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
    assert (
        client.post(
            "/api/jobs/job/approve",
            json={"files": [{"id": "file", "metadata": {"bpm": 500}}]},
        ).status_code
        == 400
    )
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
    assert job["label"] == "Import 1 files"
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

        def prepare(self, *args):
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
