import hashlib, importlib, json, threading, time
from pathlib import Path
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def backend(tmp_path, monkeypatch):
    config = {
        "state_root": str(tmp_path / "state"),
        "library_root": str(tmp_path / "library"),
        "staging_root": str(tmp_path / "staging"),
        "cookie_secure": False,
        "catalog_matching": False,
        "navidrome_db": str(tmp_path / "absent.db"),
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("ACQUISITION_CONFIG", str(path))
    from app import main

    main = importlib.reload(main)
    main.login_attempts.clear()
    return main, TestClient(main.app)


def signin(main, client, monkeypatch, name="mateo", admin=True):
    upstream = Mock()
    upstream.json.return_value = {"username": name, "isAdmin": admin}
    upstream.status_code = 200
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: upstream)
    assert (
        client.post(
            "/api/login", json={"username": name, "password": "private-test"}
        ).status_code
        == 200
    )


def seed_review(main, id="job", owner="mateo"):
    main.store.put(
        "jobs",
        id,
        {
            "candidate": {"source": "existing"},
            "source": "existing",
            "label": "Test",
            "prepared": [],
        },
        owner=owner,
        stage="review",
        created_at="2026-01-01",
    )
    path = main.STATE / "prepared.mp3"
    path.write_bytes(b"audio")
    main.store.put(
        "files",
        "file",
        {"existing_tags": {"title": "Original remix", "bpm": 120}, "title": "Proposed"},
        job_id=id,
        owner=owner,
        published=0,
        path=str(path),
    )


def test_auth_origin_and_logout(backend, monkeypatch):
    main, client = backend
    assert client.get("/api/library").status_code == 401
    assert (
        client.post(
            "/api/login",
            json={"username": "a", "password": "b"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    signin(main, client, monkeypatch)
    cookie = client.cookies.get("acquire_session")
    assert main.store.get("sessions", hashlib.sha256(cookie.encode()).hexdigest())
    assert client.get("/api/me").json() == {
        "username": "mateo",
        "isAdmin": True,
        "navidromeUrl": "http://localhost:4533",
    }
    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_review_approval_and_stale_action(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    assert not main.store.list("files", "published=1")
    assert (
        client.post(
            "/api/jobs/job/approve", json={"files": [{"id": "bad", "metadata": {}}]}
        ).status_code
        == 400
    )
    response = client.post(
        "/api/jobs/job/approve",
        json={"files": [{"id": "file", "metadata": {"title": "Odd one", "bpm": 500}}]},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == {
        "message": "Odd one: BPM must be between 0 and 400", "file_id": "file", "field": "bpm"}
    assert (
        client.post(
            "/api/jobs/job/approve",
            json={
                "files": [
                    {"id": "file", "metadata": {"title": "Confirmed remix", "bpm": 123}}
                ]
            },
        ).status_code
        == 200
    )
    job = main.store.get("jobs", "job")
    assert job["stage"] == "publish_queued"
    assert list(job["edits"].values())[0]["title"] == "Confirmed remix"
    assert client.post("/api/jobs/job/reject", json={}).status_code == 409
    assert not main.store.list("files", "published=1")


def test_private_review_and_shared_published_files(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch, "paulo", False)
    seed_review(main)
    assert client.get("/api/review").json()["jobs"] == []
    assert client.post("/api/jobs/job/approve", json={}).status_code == 404
    assert client.get("/api/files/file/download").status_code == 404
    assert client.get("/api/inbox").status_code == 403
    path = main.LIBRARY / "public.mp3"
    path.write_bytes(b"original-quality")
    main.store.put(
        "files",
        "public",
        {"title": "Track", "album_id": "album"},
        job_id=None,
        owner="mateo",
        published=1,
        path=str(path),
    )
    assert client.get("/api/files/public/download").content == b"original-quality"
    assert client.get("/api/albums/album/download").status_code == 200


def test_retry_processing_uses_retained_download(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    main.store.put(
        "jobs",
        "failed",
        {
            "candidate": {},
            "source": "existing",
            "paths": ["retained.flac"],
            "resume_stage": "processing",
        },
        owner="mateo",
        stage="failed",
        created_at="2026-01-01",
    )
    assert (
        client.post("/api/jobs/failed/retry", json={"stage": "processing"}).status_code
        == 200
    )
    job = main.store.get("jobs", "failed")
    assert job["stage"] == "process_queued"
    assert job["paths"] == ["retained.flac"]


def test_selected_approval_retains_unselected_tracks(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    second = main.STATE / "second.mp3"
    second.write_bytes(b"private second track")
    main.store.put("files", "second", {"title": "Second"}, job_id="job", owner="mateo", published=0, path=str(second))
    assert client.post("/api/jobs/job/approve", json={"selected_file_ids": []}).status_code == 400
    assert client.post("/api/jobs/job/approve", json={"selected_file_ids": ["foreign"]}).status_code == 400
    response = client.post("/api/jobs/job/approve", json={"selected_file_ids": ["file"], "keep_existing": True})
    assert response.status_code == 200
    job = main.store.get("jobs", "job")
    assert job["selected_paths"] == [str(main.STATE / "prepared.mp3")]
    assert job["skipped_count"] == 1
    assert str(second) not in job["edits"]
    assert second.read_bytes() == b"private second track"


def test_import_label_is_not_requested_track_title(backend, monkeypatch, tmp_path):
    main, client = backend
    signin(main, client, monkeypatch)
    root = tmp_path / "downloads"
    root.mkdir()
    (root / "track.mp3").write_bytes(b"audio")
    monkeypatch.setitem(main.config, "slskd_download_root", str(root))
    response = client.post("/api/import", json={"paths": ["track.mp3"]})
    assert response.status_code == 200
    job = main.store.get("jobs", response.json()["id"])
    assert job["label"] == "track"
    assert "title" not in job["candidate"]
    assert "requested_title" not in job["candidate"]


def test_publication_retry_preserves_selection_and_edits(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    main.store.put("jobs", "failed", {"source": "youtube", "resume_stage": "publishing", "selected_paths": ["chosen"], "edits": {"chosen": {"title": "Corrected"}}, "error": "Disk full", "progress": 50}, owner="mateo", stage="failed", created_at="2026-01-01")
    assert client.post("/api/jobs/failed/retry", json={"stage": "invalid"}).status_code == 400
    assert client.post("/api/jobs/failed/retry", json={"stage": "publishing"}).status_code == 200
    job = main.store.get("jobs", "failed")
    assert job["stage"] == "publish_queued"
    assert job["selected_paths"] == ["chosen"]
    assert job["edits"]["chosen"]["title"] == "Corrected"
    assert job["progress"] == 0 and job["error"] is None and job["retry_count"] == 1


def test_worker_publishes_only_selected_tracks(backend, monkeypatch):
    main, _ = backend
    chosen = main.STATE / "chosen.mp3"
    skipped = main.STATE / "skipped.mp3"
    chosen.write_bytes(b"chosen")
    skipped.write_bytes(b"private")
    main.store.put("jobs", "album", {"prepared": [{"path": str(chosen)}, {"path": str(skipped)}], "selected_paths": [str(chosen)], "edits": {}, "source": "existing"}, owner="mateo", stage="publish_queued", created_at="2026-01-01")
    seen = []
    class FakeIngestor:
        def __init__(self, config):
            pass
        def publish(self, records, edits, id, progress, cancelled):
            seen.extend(r["path"] for r in records)
            main.stop.set()
            return []
    monkeypatch.setattr(main, "Ingestor", FakeIngestor)
    monkeypatch.setattr(main, "request_scan", lambda: "Published")
    main.stop.clear()
    try:
        main.worker(("publish_queued",))
    finally:
        main.stop.clear()
    assert seen == [str(chosen)]
    assert main.store.get("jobs", "album")["stage"] == "published"
    assert skipped.read_bytes() == b"private"


def test_recovery_and_atomic_claim(backend):
    main, _ = backend
    for id, stage in [
        ("a", "downloading"),
        ("b", "processing"),
        ("c", "publishing"),
        ("d", "review"),
    ]:
        main.store.put(
            "jobs",
            id,
            {"candidate": {}, "paths": ["saved"]},
            owner="mateo",
            stage=stage,
            created_at=id,
        )
    main.store.recover()
    assert main.store.get("jobs", "d")["stage"] == "review"
    claims = []

    def claim():
        claims.append(main.store.claim()["id"])

    threads = [threading.Thread(target=claim) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claims) == ["a", "b", "c"]


def test_library_filters_and_path_escape(backend, monkeypatch, tmp_path):
    main, client = backend
    signin(main, client, monkeypatch)
    for id, bpm, key in [("a", 110, "8A"), ("b", 130, "8B")]:
        p = main.LIBRARY / (id + ".mp3")
        p.write_bytes(b"audio")
        main.store.put(
            "files",
            id,
            {"title": id, "bpm": bpm, "key": key, "genre": "House", "bitrate": 320000},
            published=1,
            path=str(p),
            owner=None,
            job_id=None,
        )
    result = client.get("/api/library?bpm_min=120&key=8B").json()
    assert result["total"] == 1
    assert result["files"][0]["bitrate"] == 320
    assert client.get("/api/library?bpm_min=not-a-number").status_code == 400
    outside = tmp_path / "private"
    outside.write_bytes(b"secret")
    main.store.put(
        "files",
        "escape",
        {"title": "escape"},
        published=1,
        path=str(outside),
        owner=None,
        job_id=None,
    )
    assert client.get("/api/files/escape/download").status_code == 404


def test_worker_lanes_do_not_block_processing(backend):
    main, _ = backend
    for id, stage in [("download", "queued"), ("process", "process_queued")]:
        main.store.put(
            "jobs", id, {"candidate": {}}, owner="mateo", stage=stage, created_at=id
        )
    assert main.store.claim(("queued",))["id"] == "download"
    assert main.store.claim(("queued",)) is None
    assert main.store.claim(("process_queued", "publish_queued"))["id"] == "process"


def test_ingestion_lanes_keep_one_publisher(backend):
    main, _ = backend
    assert main.ingestion_lanes(None) == [("ingestion-worker", ("process_queued", "publish_queued"))]
    lanes = main.ingestion_lanes(4)
    assert [name for name, _ in lanes] == ["ingestion-worker", "processing-worker-2", "processing-worker-3", "processing-worker-4"]
    assert sum("publish_queued" in stages for _, stages in lanes) == 1
    assert len(main.ingestion_lanes(50)) == 8


def test_cancel_during_prepare_is_not_overwritten_by_review(backend, monkeypatch):
    main, _ = backend
    main.store.put(
        "jobs",
        "cancel-test",
        {"candidate": {}, "paths": ["saved-source"], "source": "existing"},
        owner="mateo",
        stage="process_queued",
        created_at="now",
    )

    class FakeIngestor:
        def __init__(self, config):
            pass

        def prepare(self, *args, **kwargs):
            main.store.update_job("cancel-test", cancel_requested=True)
            return []

    monkeypatch.setattr(main, "Ingestor", FakeIngestor)
    thread = threading.Thread(target=main.worker, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if main.store.get("jobs", "cancel-test")["stage"] == "cancelled":
                break
            time.sleep(0.01)
        assert main.store.get("jobs", "cancel-test")["stage"] == "cancelled"
        assert not main.store.list("files")
    finally:
        main.stop.set()
        thread.join(timeout=2)


def test_sharing_admin_only_and_private_review_not_shared(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    assert not client.get('/api/review').json()['jobs'][0]['files'][0]['shared']
    assert client.post('/api/files/file/sharing', json={'shared': True}).status_code == 404
    audio = main.LIBRARY / 'track.mp3'
    audio.write_bytes(b'original-audio')
    main.store.put('files', 'accepted', {'title':'Accepted'}, path=str(audio), published=1)
    main.sharing.reconcile()
    assert client.get('/api/library').json()['files'][0]['shared']
    assert client.post('/api/files/accepted/sharing', json={'shared': False}).status_code == 200
    assert not (main.sharing.root / 'track.mp3').exists()
    assert audio.read_bytes() == b'original-audio'
    assert client.post('/api/sharing', json={'enabled': False}).json()['enabled'] is False
    signin(main, client, monkeypatch, name='paulo', admin=False)
    assert client.get('/api/sharing').status_code == 200
    assert client.post('/api/sharing', json={'enabled': True}).status_code == 403
    assert client.post('/api/files/accepted/sharing', json={'shared': True}).status_code == 403


def test_review_exposes_guidance_without_changing_decision(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    job = client.get('/api/review').json()['jobs'][0]
    assert job['review_summary']['label'] == 'Check before approving'
    assert any('Missing artist' in c for c in job['review_summary']['concerns'])
    assert main.store.get('jobs', 'job')['stage'] == 'review'
    assert not main.store.get('files', 'file')['published']


def library_track(main, name, key=None, bpm=None, genre=None, year=None, mood=None):
    import sqlite3, subprocess
    from mediafile import MediaFile

    path = main.LIBRARY / name
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-codec:a', 'libmp3lame', str(path)], check=True)
    tags = MediaFile(str(path))
    tags.title, tags.artist, tags.initial_key, tags.bpm_precise = name, 'Artist', key, bpm
    tags.genres, tags.year, tags.mood = genre, year, mood
    tags.save()
    db = sqlite3.connect(main.config['navidrome_db'])
    db.execute("CREATE TABLE IF NOT EXISTS media_file (path, title, artist, album, genre, tags, codec, suffix, bit_rate, duration, size, album_id, missing, library_id)")
    db.execute("INSERT INTO media_file VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,1)",
               (name, name, 'Artist', 'Album', 'House', json.dumps({'key': [{'value': key}]} if key else {}), 'mp3', 'mp3', 128, 2.0, path.stat().st_size, 'nd-album'))
    db.commit()
    db.close()
    return path, hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32]


def test_index_normalizes_keys_and_picks_up_external_tag_edits(backend, monkeypatch):
    from mediafile import MediaFile

    main, client = backend
    signin(main, client, monkeypatch)
    path, id = library_track(main, 'a.mp3', key='Ebm')
    _, junk = library_track(main, 'b.mp3', key='EBM')
    main.index_library()
    assert main.store.get('files', id)['key'] == '2A'
    library = client.get('/api/library?key=Ebm').json()
    assert [f['id'] for f in library['files']] == [id]
    assert library['keys'] == ['2A'] and library['invalid_key_count'] == 1
    flagged = client.get('/api/library?key=invalid').json()['files']
    assert [(f['id'], f['key_invalid'], f['key_tag']) for f in flagged] == [(junk, True, 'EBM')]
    tags = MediaFile(str(path))
    tags.initial_key, tags.bpm_precise = '9A', 126.5
    tags.save()
    main.index_library()
    record = main.store.get('files', id)
    assert record['key'] == '9A' and record['bpm'] == 126.5


def test_admin_edits_library_tags_in_the_file(backend, monkeypatch):
    from mediafile import MediaFile

    main, client = backend
    signin(main, client, monkeypatch)
    path, id = library_track(main, 'a.mp3', key='EBM')
    main.index_library()
    main.store.put('files', id, {**{k: v for k, v in main.store.get('files', id).items() if k not in {'id', 'path', 'job_id', 'owner', 'published'}}, 'analysis_source': {'bpm': 'estimate'}}, path=str(path.resolve()), published=1, job_id=None, owner=None)
    assert client.post(f'/api/files/{id}/tags', json={'key': 'EBM'}).status_code == 400
    assert client.post(f'/api/files/{id}/tags', json={'path': '/etc'}).status_code == 400
    result = client.post(f'/api/files/{id}/tags', json={'key': 'Gm', 'bpm': 98.5, 'genre': 'Techno'})
    assert result.status_code == 200
    assert result.json()['file']['key'] == '6A' and not result.json()['file']['key_invalid']
    media = MediaFile(str(path))
    assert media.initial_key == '6A' and media.bpm_precise == 98.5 and media.genre == 'Techno'
    assert main.store.get('files', id)['analysis_source'] == {}
    assert client.post(f'/api/files/{id}/tags', json={'key': '', 'bpm': None}).status_code == 200
    assert not MediaFile(str(path)).initial_key and not MediaFile(str(path)).bpm_precise
    signin(main, client, monkeypatch, name='guest', admin=False)
    assert client.post(f'/api/files/{id}/tags', json={'key': '8A'}).status_code == 403


def test_library_analysis_fills_missing_and_normalizes_without_replacing(backend, monkeypatch):
    from mediafile import MediaFile

    main, client = backend
    signin(main, client, monkeypatch)
    calls = []
    monkeypatch.setattr('app.library_tags._analyze', lambda path, need_bpm, need_key, cancelled: calls.append((need_bpm, need_key)) or (124.0 if need_bpm else None, '8A' if need_key else None))
    done = dict(genre=['House'], year=2001, mood=['Party'])
    missing, missing_id = library_track(main, 'missing.mp3', **done)
    standard, standard_id = library_track(main, 'standard.mp3', key='Ebm', bpm=120, **done)
    junk, junk_id = library_track(main, 'junk.mp3', key='EBM', bpm=120, **done)
    complete, _ = library_track(main, 'complete.mp3', key='5A', bpm=120, **done)
    untouched = complete.read_bytes()
    main.index_library()
    main.analyze_library('mateo')
    status = client.get('/api/library/analysis').json()
    assert status['status'] == 'complete' and status['total'] == 2
    assert (status['bpm_added'], status['key_added'], status['key_normalized']) == (1, 1, 1)
    assert calls == [(True, True)]
    assert MediaFile(str(missing)).initial_key == '8A' and MediaFile(str(missing)).bpm_precise == 124.0
    assert main.store.get('files', missing_id)['analysis_source'].keys() == {'bpm', 'key'}
    assert MediaFile(str(standard)).initial_key == '2A'
    assert MediaFile(str(junk)).initial_key == 'EBM'
    assert complete.read_bytes() == untouched
    main.analyze_library('mateo')
    assert client.get('/api/library/analysis').json()['total'] == 0
    signin(main, client, monkeypatch, name='guest', admin=False)
    assert client.post('/api/library/analysis', json={}).status_code == 403


class FakeCatalog:
    def __init__(self):
        self.calls = []

    def lookup(self, artist, title, need_genre=True, need_year=True):
        self.calls.append((title, need_genre, need_year))
        found = {'sources': {}}
        if need_genre and title == 'listed.mp3':
            found['genre'], found['sources']['genre'] = ['Boom Bap'], 'discogs'
        if need_year:
            found['year'], found['sources']['year'] = 1994, 'musicbrainz-first-release'
        return found


class FakeModels:
    available = True

    def describe(self, audio):
        assert len(audio) > 16000
        return {'genre': ['Hip-Hop'], 'mood': ['Relaxed', 'Danceable']}


def test_library_analysis_fills_genre_year_and_mood_from_catalog_then_audio(backend, monkeypatch):
    from mediafile import MediaFile

    main, client = backend
    signin(main, client, monkeypatch)
    catalog = FakeCatalog()
    monkeypatch.setattr(main, 'Catalog', lambda config: catalog)
    monkeypatch.setattr(main, 'models_at', lambda root: FakeModels())
    keyed = dict(key='5A', bpm=120)
    listed, listed_id = library_track(main, 'listed.mp3', **keyed)
    unlisted, _ = library_track(main, 'unlisted.mp3', **keyed)
    spelled, _ = library_track(main, 'spelled.mp3', genre=['hip-hop', 'Hip Hop', 'jazz'], year=2015, mood=['Sad'], **keyed)
    main.index_library()
    main.analyze_library('mateo')
    media = MediaFile(str(listed))
    assert media.genres == ['Boom Bap'] and media.year == 1994 and media.mood == ['Relaxed', 'Danceable']
    assert main.store.get('files', listed_id)['analysis_source'] == {
        'genre': 'discogs', 'year': 'musicbrainz-first-release', 'mood': 'essentia-discogs-effnet-estimate'}
    assert MediaFile(str(unlisted)).genres == ['Hip Hop']
    media = MediaFile(str(spelled))
    assert media.genres == ['Hip Hop', 'Jazz'] and media.year == 2015 and media.mood == ['Sad']
    assert ('spelled.mp3', False, False) not in catalog.calls
    status = client.get('/api/library/analysis').json()
    assert (status['genre_added'], status['genre_normalized'], status['year_added'], status['mood_added']) == (2, 1, 2, 2)
    library = client.get('/api/library?genre=Jazz').json()
    assert [f['title'] for f in library['files']] == ['spelled.mp3']
    assert client.post(f'/api/files/{listed_id}/tags', json={'key': 'soon'}).status_code == 400
    assert client.post(f'/api/files/{listed_id}/tags', json={'year': 1995, 'mood': 'Dark, Happy', 'genre': 'hip-hop; Jazz'}).status_code == 200
    media = MediaFile(str(listed))
    assert media.year == 1995 and media.mood == ['Dark', 'Happy'] and media.genres == ['Hip Hop', 'Jazz']


def test_analysis_start_rejects_a_second_run_and_marks_silent_runs_interrupted(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    monkeypatch.setattr(main, 'analyze_library', lambda username: None)
    assert client.post('/api/library/analysis', json={}).status_code == 200
    assert client.get('/api/library/analysis').json()['status'] == 'running'
    assert client.post('/api/library/analysis', json={}).status_code == 409
    assert client.post('/api/library/analysis/cancel', json={}).status_code == 200
    main.ANALYSIS_STATE.write_text(json.dumps({**json.loads(main.ANALYSIS_STATE.read_text()), 'heartbeat': 0}))
    assert client.get('/api/library/analysis').json()['status'] == 'interrupted'
    assert client.post('/api/library/analysis', json={}).status_code == 200


def test_review_summary_lists_skipped_files():
    from app.review_summary import summarize
    job = {'candidate': {}, 'skipped_files': [{'name': 'a.mp3', 'reason': 'Header missing'}]}
    summary = summarize(job, [{'title': 'x', 'artist': 'y'}])
    assert any('a.mp3' in concern and 'left out' in concern for concern in summary['concerns'])


def test_error_during_shutdown_requeues_instead_of_failing(backend, monkeypatch):
    main, _ = backend
    main.store.put("jobs", "shutdown-test", {"candidate": {}, "paths": ["saved-source"], "source": "existing"},
                   owner="mateo", stage="process_queued", created_at="now")

    class DyingIngestor:
        def __init__(self, config):
            pass

        def prepare(self, *args, **kwargs):
            main.stop.set()
            raise OSError("ffmpeg killed during shutdown")

    monkeypatch.setattr(main, "Ingestor", DyingIngestor)
    main.stop.clear()
    try:
        main.worker(("process_queued",))
        job = main.store.get("jobs", "shutdown-test")
        assert job["stage"] == "process_queued"
        assert "resumes after restart" in job["detail"]
    finally:
        main.stop.set()


def test_year_edits_are_lenient(backend):
    main, _ = backend
    clean = main.clean_metadata
    assert clean({"year": "2024-05-01"})["year"] == 2024
    assert clean({"year": 1994.0})["year"] == 1994
    assert clean({"year": 0})["year"] == 0
    assert clean({"year": 20240501})["year"] == 0
    assert clean({"year": "unknown"})["year"] == 0


def test_import_label_names_the_folder(backend, tmp_path):
    main, _ = backend
    root = tmp_path / "downloads"
    paths = [str(root / "DIRECTOS" / artist / "a.mp3") for artist in ("Jul", "Migos")]
    assert main.import_label(paths, root) == "DIRECTOS · 2 files"
    assert main.import_label([str(root / "a.mp3"), str(root / "b.mp3")], root) == "Inbox import · 2 files"


def test_approval_can_leave_tracks_for_later(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    seed_review(main)
    later = main.STATE / "later.mp3"
    later.write_bytes(b"audio")
    main.store.update_job("job", prepared=[{"path": str(main.STATE / "prepared.mp3")}, {"path": str(later)}])
    main.store.put("files", "later", {"title": "Later"}, job_id="job", owner="mateo", published=0, path=str(later))
    assert client.post("/api/jobs/job/approve", json={
        "selected_file_ids": ["file"], "later_file_ids": ["file"]}).status_code == 400
    response = client.post("/api/jobs/job/approve", json={
        "files": [{"id": "file", "metadata": {"title": "Now"}}],
        "selected_file_ids": ["file"], "later_file_ids": ["later"]})
    assert response.status_code == 200
    rest = main.store.get("jobs", response.json()["later_job_id"])
    assert rest["stage"] == "review" and rest["split_from"] == "job" and rest["label"] == "Test"
    assert [p["path"] for p in rest["prepared"]] == [str(later)]
    assert main.store.get("files", "later")["job_id"] == rest["id"]
    job = main.store.get("jobs", "job")
    assert job["stage"] == "publish_queued" and job["skipped_count"] == 0
    review = client.get("/api/review").json()["jobs"]
    assert [[f["id"] for f in j["files"]] for j in review] == [["later"]]
