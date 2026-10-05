"""Candidate-specific library hints must not equate unrelated versions or query results."""
import pytest
from test_main import backend, signin


def records():
    return [
        {"id": "studio", "artist": "Vulfpeck", "title": "Sauna", "album": "Schvitz", "format": "mp3", "bitrate": 320000},
        {"id": "live", "artist": "Vulfpeck", "title": "Sauna (Live)", "album": "MSG II", "format": "flac", "bitrate": 1000000},
        {"id": "remix", "artist": "Four Tet", "title": "Love Cry (Joy Orbison Remix)", "album": "Remixes", "format": "flac", "bitrate": 900000},
    ]


def test_exact_source_identity_normalizes_case_and_punctuation(backend):
    main, _ = backend
    result = main._search_library_matches({"kind": "track", "artist": "FOUR TET", "title": "Love Cry - Joy Orbison Remix", "album": "Remixes"}, records())
    assert result["library_match"] == "exact"
    assert [r["id"] for r in result["library_matches"]] == ["remix"]
    assert result["library_matches"][0]["bitrate"] == 900


def test_filename_hint_is_possible_and_keeps_recording_versions(backend):
    main, _ = backend
    result = main._search_library_matches({"filename": "Vulfpeck - 05 - Sauna.mp3", "title": "Vulfpeck - 05 - Sauna"}, records())
    assert result["library_match"] == "possible"
    assert [r["id"] for r in result["library_matches"]] == ["studio"]
    result = main._search_library_matches({"filename": "Vulfpeck - Sauna (Live).flac", "title": "Vulfpeck - Sauna (Live)"}, records())
    assert [r["id"] for r in result["library_matches"]] == ["live"]
    assert not main._search_library_matches({"filename": "Vulfpeck - Sauna (Acoustic).mp3"}, records())["library_matches"]


def test_requested_track_does_not_mark_unrelated_candidates(backend):
    main, _ = backend
    requested = {"requested_artist": "Vulfpeck", "requested_title": "Sauna"}
    assert not main._search_library_matches({**requested, "title": "Dean Town", "filename": "Vulfpeck - Dean Town.flac"}, records())["library_matches"]
    assert not main._search_library_matches({**requested, "artist": "Someone Else", "title": "Sauna"}, records())["library_matches"]
    hint = main._search_library_matches({**requested, "filename": "05 - Sauna.m4a"}, records())
    assert hint["library_match"] == "possible"


def test_conflicting_album_is_not_an_exact_identity(backend):
    main, _ = backend
    result = main._search_library_matches({"artist": "Vulfpeck", "title": "Sauna", "album": "MSG II"}, records())
    assert result["library_match"] == "possible"


def test_album_counts_individual_files_without_claiming_complete_album(backend):
    main, _ = backend
    result = main._search_library_matches({"kind": "album", "artist": "Vulfpeck", "album": "Schvitz", "folder_complete": False, "files": [{"title": "Sauna"}, {"title": "Dean Town"}]}, records())
    assert result["matched_track_count"] == 1
    assert result["confirmed_track_count"] == 1
    assert result["listed_track_count"] == 2
    assert result["library_match"] == "possible"


def test_search_endpoint_enriches_only_available_published_files(backend, monkeypatch):
    main, client = backend
    signin(main, client, monkeypatch)
    for id, published, available in [("visible", 1, True), ("private", 0, True), ("missing", 1, False)]:
        path = main.LIBRARY / (id + ".flac")
        if available:
            path.write_bytes(b"test")
        main.store.put("files", id, records()[0], path=str(path), job_id=None, owner="mateo", published=published)
    candidate = {"id": "candidate", "artist": "Vulfpeck", "title": "Sauna"}
    main.store.put("searches", "search", {"results": [candidate]}, owner="mateo", status="done")
    response = client.get("/api/search/search")
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["library_match"] == "exact"
    assert [m["id"] for m in result["library_matches"]] == ["visible"]
    assert "path" not in result["library_matches"][0]
    assert main.store.get("searches", "search")["results"] == [candidate]
    signin(main, client, monkeypatch, name="paulo", admin=False)
    assert client.get("/api/search/search").status_code == 404


def test_parent_folder_cannot_provide_track_title_evidence(backend):
    main, _ = backend
    result = main._search_library_matches({"filename": r"Vulfpeck\Sauna\Dean Town.flac", "title": "Dean Town"}, records())
    assert not result["library_matches"]
    actual = main._search_library_matches({"filename": r"Vulfpeck\Schvitz\05 - Sauna.flac", "title": "05 - Sauna"}, records())
    assert [r["id"] for r in actual["library_matches"]] == ["studio"]
    assert not main._search_library_matches({"filename": "Vulfpeck - Sauna (VIP).mp3"}, records())["library_matches"]
