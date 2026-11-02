import importlib, json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import concerts

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    report = tmp_path / "concerts/latest.json"
    config = {
        "state_root": str(tmp_path / "state"), "library_root": str(tmp_path / "library"),
        "staging_root": str(tmp_path / "staging"), "cookie_secure": False, "catalog_matching": False,
        "navidrome_db": str(tmp_path / "absent.db"), "concerts_report": str(report),
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("ACQUISITION_CONFIG", str(path))
    from app import main

    main = importlib.reload(main)
    main.login_attempts.clear()
    return main, TestClient(main.app), report


def signin(main, client, monkeypatch, admin=True):
    upstream = Mock(status_code=200)
    upstream.json.return_value = {"username": "mateo", "isAdmin": admin}
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: upstream)
    assert client.post("/api/login", json={"username": "mateo", "password": "private-test"}).status_code == 200
    # Concert views must never reach a provider or another service.
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: pytest.fail("network call"))
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: pytest.fail("network call"))


def event(id, artists, date, city="Sevilla", highlight="", **extra):
    return {"id": id, "name": extra.pop("name", "Show"), "date": date, "time": "21:00:00",
            "date_unknown": False, "time_unknown": False, "timezone": "Europe/Madrid", "status": "onsale",
            "venue": "Sala", "venue_id": "v", "city": city, "country": "ES", "highlight": highlight,
            "artists": artists, "url": f"https://tickets.example/{id}", "source": "Ticketmaster",
            "lineup_complete": False, **extra}


def report(events, checked=NOW, **extra):
    return {"checked_at": checked.isoformat(), "mode": "send", "events": events,
            "sources": {"Ticketmaster": {"status": "ok", "events": 10},
                        "Resident Advisor": {"status": "unavailable", "error": "RA HTTP 503; source unavailable"}},
            "settings": {"lookahead_days": 365, "max_nationwide_dates": 3}, "duplicates": [],
            "coverage": "Only successful sources are included.", **extra}


TOUR = {"tour": "Long Tour"}
RARE = {"rare": "Rare Artist"}


def catalogue():
    return [
        event("t1", TOUR, "2026-11-10", city="A Coruña", highlight="A Coruña · Galicia"),
        *[event(f"t{i}", TOUR, f"2026-11-1{i}") for i in range(2, 6)],
        event("r1", RARE, "2027-03-01", city="Málaga"),
        event("fest", {**TOUR, **RARE}, "2027-06-01", name="Big Festival"),
        event("tour-only-fest", TOUR, "2027-07-01", name="Other Festival"),
        event("past", RARE, "2026-10-01"),
    ]


def test_requires_signed_in_web_session(backend, monkeypatch):
    main, client, path = backend
    path.parent.mkdir()
    path.write_text(json.dumps(report(catalogue())))
    assert client.get("/api/concerts").status_code == 401
    signin(main, client, monkeypatch, admin=False)
    assert client.get("/api/concerts").json()["counts"]["listings"] > 0
    token = main.mint_agent("mateo", "helper", True, "owner")["token"]
    agent = TestClient(main.app)
    assert agent.get("/api/concerts", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_read_only_route_and_no_private_files(backend, monkeypatch):
    main, client, path = backend
    path.parent.mkdir()
    secret = "tm-secret-key-123"
    raw = report([{**catalogue()[0], "api_key": secret}], ticketmaster_api_key=secret)
    path.write_text(json.dumps(raw))
    (path.parent / "config.json").write_text(json.dumps({"ticketmaster_api_key": secret}))
    before = path.read_bytes(), path.stat().st_mtime_ns
    signin(main, client, monkeypatch)
    response = client.get("/api/concerts")
    assert response.status_code == 200 and secret not in response.text
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert client.post("/api/concerts", json={}).status_code == 405
    for url in ("/api/concerts/latest.json", "/api/concerts/config.json", "/api/concerts/", "/concerts/latest.json"):
        assert secret not in client.get(url).text
    assert client.get("/api/concerts?days=999").status_code == 422
    assert client.get("/api/concerts?area=elsewhere").status_code == 422
    assert client.get("/api/concerts?q=" + "x" * 101).status_code == 422


def test_missing_invalid_and_stale_reports(backend, monkeypatch):
    main, client, path = backend
    signin(main, client, monkeypatch)
    data = client.get("/api/concerts").json()
    assert data["status"] == "missing" and data["groups"] == [] and "18:00" in data["next_check"]
    path.parent.mkdir()
    path.write_text("{not json")
    assert client.get("/api/concerts").json()["status"] == "invalid"
    path.write_text(json.dumps({"events": "nope"}))
    assert client.get("/api/concerts").json()["status"] == "invalid"
    view = concerts.build_view(report(catalogue(), checked=NOW - timedelta(hours=30)), now=NOW)
    assert view["status"] == "stale" and view["message"] and view["groups"]
    assert concerts.build_view(report(catalogue()), now=NOW)["status"] == "ok"


def test_long_tour_restriction_and_shared_bills():
    view = concerts.build_view(report(catalogue()), now=NOW)
    groups = {g["artists"]: g for g in view["groups"]}
    tour = groups["Long Tour"]
    # Only the preferred-area date of a long tour shows; the rest stays one click away.
    assert [e["id"] for e in tour["dates"]] == ["t1"]
    assert {e["id"] for e in tour["hidden"]} == {"t2", "t3", "t4", "t5", "tour-only-fest"}
    assert [e["id"] for e in groups["Rare Artist"]["dates"]] == ["r1"]  # past date dropped
    bill = groups["Long Tour, Rare Artist"]
    assert bill["multi"] and bill["title"] == "Big Festival" and [e["id"] for e in bill["dates"]] == ["fest"]
    assert view["counts"] == {"matching": 9, "listings": 8, "shown": 3, "hidden": 5, "groups": 3}
    assert [s["name"] for s in view["sources"]] == ["Resident Advisor", "Ticketmaster"]
    assert view["sources"][0] == {"name": "Resident Advisor", "status": "unavailable", "events": None,
                                  "cached": False, "error": "RA HTTP 503; source unavailable"}


def test_filters_area_text_and_window():
    raw = report(catalogue())
    preferred = concerts.build_view(raw, now=NOW, area="preferred")
    assert [e["id"] for g in preferred["groups"] for e in g["dates"] + g["hidden"]] == ["t1"]
    found = concerts.build_view(raw, now=NOW, q="rare  artist")
    assert {g["artists"] for g in found["groups"]} == {"Rare Artist", "Long Tour, Rare Artist"}
    assert concerts.build_view(raw, now=NOW, q="big festival")["counts"]["listings"] == 1
    accents = concerts.build_view(report([event("x", {"a": "Pépe"}, "2026-12-01")]), now=NOW, q="PEPE")
    assert accents["counts"]["shown"] == 1
    month = concerts.build_view(raw, now=NOW, days=30)
    assert {e["id"] for g in month["groups"] for e in g["dates"] + g["hidden"]} == {"t1", "t2", "t3", "t4", "t5"}
    # The restriction still uses every known date, not only those in the window.
    assert month["counts"]["hidden"] == 4


def test_unsafe_links_and_malformed_events_are_dropped():
    events = [event("a", {"a": "A"}, "2026-12-01", url="javascript:alert(1)"),
              event("b", {"b": "B"}, "2026-12-02", url="http://insecure.example/x"),
              event("c", {"c": "C"}, "not-a-date"), {"id": "broken"}, "junk"]
    raw = report(events, duplicates=[{"source_id": "ra:9", "canonical_id": "a", "url": "https://ra.co/events/9"},
                                     {"source_id": "ra:8", "canonical_id": "a", "url": "data:text/html,x"}])
    view = concerts.build_view(raw, now=NOW)
    dates = {e["id"]: e for g in view["groups"] for e in g["dates"]}
    assert set(dates) == {"a", "b", "c"}
    assert dates["a"]["url"] == "" and dates["b"]["url"] == ""
    assert dates["a"]["alternates"] == [{"source": "Resident Advisor", "url": "https://ra.co/events/9"}]
    assert dates["c"]["date_unknown"] and view["groups"][-1]["dates"][0]["id"] == "c"


def test_next_check_follows_madrid_local_time():
    assert concerts.next_check(datetime(2026, 11, 2, 16, 59, tzinfo=timezone.utc)).isoformat() == "2026-11-02T18:00:00+01:00"
    assert concerts.next_check(datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)).isoformat() == "2026-11-03T18:00:00+01:00"
    assert concerts.next_check(datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)).isoformat() == "2026-07-01T18:00:00+02:00"
