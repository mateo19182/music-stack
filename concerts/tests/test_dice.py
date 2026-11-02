import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from concert_alerts.__main__ import sync
from concert_alerts.dice import Dice, parse_event_page, sitemap_entries, spanish_slug
from concert_alerts.events import select_dice_events
from concert_alerts.library import Artist, ArtistIndex
from concert_alerts.ticketmaster import ProviderError

START = datetime(2026, 11, 2, tzinfo=timezone.utc)
MADRID = "https://dice.fm/event/xe8o2l-four-tet-23rd-sep-sala-but-madrid-tickets"
LONDON = "https://dice.fm/event/k63wr9-four-tet-18th-dec-the-clapham-grand-london-tickets"
CORUNA = "https://dice.fm/event/ab12cd-festival-1st-jul-sala-mare-a-coruna-tickets"


def page(name="Four Tet", performers=("Four Tet",), status="EventScheduled", postal="28004", locality="Madrid",
         start="2027-09-23T20:00:00+02:00", country="ES"):
    item = {"@context": "https://schema.org", "@type": "MusicEvent", "name": name, "startDate": start,
            "eventStatus": f"https://schema.org/{status}", "url": MADRID,
            "location": {"@type": "Place", "name": "Sala But &amp; Co", "address": {
                "@type": "PostalAddress", "streetAddress": f"Calle Barceló, 11, {postal} {locality}, Spain",
                "addressLocality": locality, "addressCountry": country}},
            "performer": [{"@type": "PerformingGroup", "name": p,
                           "url": f"https://dice.fm/artist/{p.lower().replace(' ', '-')}-x1"} for p in performers]}
    return f'<html><script type="application/ld+json">{json.dumps(item)}</script></html>'


def sitemap(urls):
    rows = "".join(f"<url><loc>{u}</loc><lastmod>{m}</lastmod></url>" for u, m in urls.items())
    return f"<urlset>{rows}</urlset>"


def index_xml():
    return "<sitemapindex><sitemap><loc>https://dice.fm/sitemaps/sitemap1.xml</loc></sitemap></sitemapindex>"


def reader(handler, path=None, budget=200):
    return Dice(cache_path=path, client=httpx.Client(transport=httpx.MockTransport(handler)),
                sleep=lambda _: None, page_budget=budget)


def site(listing, pages, calls=None):
    def handle(request):
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if url.endswith("/sitemaps/sitemap.xml"):
            return httpx.Response(200, text=index_xml())
        if url.endswith("/sitemap1.xml"):
            return httpx.Response(200, text=sitemap(listing))
        if url in pages:
            return httpx.Response(*pages[url]) if isinstance(pages[url], tuple) else httpx.Response(200, text=pages[url])
        return httpx.Response(404)
    return handle


def test_sitemap_parsing_and_spanish_prefilter():
    entries = sitemap_entries(sitemap({MADRID: "2026-10-29T03:34:09Z", LONDON: ""}))
    assert entries == {MADRID: "2026-10-29T03:34:09Z", LONDON: ""}
    assert spanish_slug(MADRID) and spanish_slug(CORUNA) and not spanish_slug(LONDON)


def test_json_ld_event_is_parsed_without_trusting_markup():
    event = parse_event_page(page(status="EventCancelled"), MADRID)
    assert event["artists"] == [{"id": "four-tet-x1", "name": "Four Tet"}]
    assert event["venue"] == "Sala But & Co" and event["status"] == "cancelled" and event["country"] == "ES"
    assert parse_event_page(page(status="EventRescheduled"), MADRID)["status"] == "listed"
    moved = page(status="EventRescheduled").replace('"startDate"', '"previousStartDate": "2027-09-01", "startDate"')
    assert parse_event_page(moved, MADRID)["status"] == "rescheduled"
    assert parse_event_page("<html>no data</html>", MADRID) is None
    assert parse_event_page('<script type="application/ld+json">{broken</script>', MADRID) is None


def test_only_new_or_changed_pages_are_fetched(tmp_path):
    path = tmp_path / "dice-cache.json"
    calls = []
    listing = {MADRID: "1", LONDON: "1", CORUNA: "1"}
    p = reader(site(listing, {MADRID: page(), CORUNA: (404,)}, calls), path)
    events = p.events()
    assert [e["url"] for e in events] == [MADRID]
    assert LONDON not in calls and p.pages_fetched == 2 and p.pending == 0
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    calls.clear()
    p = reader(site(listing, {}, calls), path)
    assert len(p.events()) == 1 and p.pages_fetched == 0 and len(calls) == 2  # sitemaps only
    listing[MADRID] = "2"
    p = reader(site(listing, {MADRID: page(name="Moved")}, calls), path)
    assert p.events()[0]["name"] == "Moved" and p.pages_fetched == 1
    # Events gone from the sitemap leave the cache.
    p = reader(site({CORUNA: "1"}, {}), path)
    assert p.events() == [] and list(json.loads(path.read_text())["pages"]) == [CORUNA]


def test_budget_leaves_pending_pages_for_the_next_run(tmp_path):
    path = tmp_path / "dice-cache.json"
    listing = {MADRID: "1", CORUNA: "1"}
    pages = {MADRID: page(), CORUNA: page(locality="A Coruña", postal="15001")}
    p = reader(site(listing, pages), path, budget=1)
    assert len(p.events()) == 1 and p.pending == 1
    p = reader(site(listing, pages), path, budget=1)
    assert len(p.events()) == 2 and p.pending == 0


def test_failures_are_reported_not_hidden(tmp_path):
    def down(request):
        return httpx.Response(503)
    with pytest.raises(ProviderError, match="DICE HTTP 503"):
        reader(down).events()
    urls = {f"https://dice.fm/event/id{i}-show-sala-madrid-tickets": "1" for i in range(6)}
    path = tmp_path / "dice-cache.json"
    with pytest.raises(ProviderError, match="keep failing"):
        reader(site(urls, {u: (403,) for u in urls}), path).events()
    assert path.exists()  # progress is kept even when the run fails
    with pytest.raises(ProviderError, match="no Spanish events"):
        reader(site({LONDON: "1"}, {})).events()


@pytest.fixture
def index():
    return ArtistIndex([Artist("four", "Four Tet"), Artist("vulf", "Vulfpeck")])


def test_matching_location_window_and_status(index):
    events = [parse_event_page(page(), MADRID),
              parse_event_page(page(performers=("Unknown DJ",)), MADRID),
              {**parse_event_page(page(locality="A Coruña", postal="15001"), CORUNA), "url": CORUNA},
              parse_event_page(page(country="GB"), MADRID),
              parse_event_page(page(start="2026-10-01T20:00:00+02:00"), MADRID),
              parse_event_page(page(status="EventPostponed", start="2027-09-24T21:00:00+02:00"), MADRID)]
    selected, unresolved, unknown = select_dice_events(events, index, START, START + timedelta(days=365))
    assert [e["id"] for e in selected] == ["dice:xe8o2l", "dice:ab12cd", "dice:xe8o2l"]
    first = selected[0]
    assert first["artists"] == {"four": "Four Tet"} and first["source"] == "DICE"
    assert (first["date"], first["time"], first["city"], first["highlight"]) == ("2027-09-23", "20:00:00", "Madrid", "Madrid")
    assert selected[1]["highlight"] == "A Coruña · Galicia"
    assert selected[2]["status"] == "postponed"
    assert unresolved[0]["provider_id"] == "dice:unknown-dj-x1"


def test_dice_dates_are_held_until_notifications_are_enabled(tmp_path, monkeypatch):
    library = tmp_path / "navidrome.db"
    db = sqlite3.connect(library)
    db.execute("CREATE TABLE artist(id TEXT, name TEXT, mbz_artist_id TEXT, missing INTEGER)")
    db.execute("INSERT INTO artist VALUES('four','Four Tet','',0)")
    db.commit()
    db.close()

    class NoTicketmaster:
        requests = 0
        def __init__(self, key): pass
        def events(self, start, end): return []
        def close(self): pass

    class FakeDice:
        requests, pages_fetched, pending = 3, 1, 0
        def __init__(self, cache_path, page_budget): pass
        def events(self, start, end): return [parse_event_page(page(), MADRID)]
        def close(self): pass

    delivered = []
    monkeypatch.setattr("concert_alerts.__main__.deliver", lambda pending, *a, **k: delivered.append(pending) or 0)
    config = {"navidrome_db": library, "state_db": tmp_path / "events.db", "report": tmp_path / "latest.json",
              "dice_enabled": True}
    report = sync(config, send=True, provider_factory=NoTicketmaster, dice_factory=FakeDice)
    assert report["matching_events"] == 1 and report["provider_events"] == 1 and report["held_notifications"] == 1 and delivered == [[]]
    assert report["sources"]["DICE"] == {"status": "ok", "events": 1, "pages_fetched": 1, "pending": 0}
    report = sync({**config, "dice_notify": True}, send=True, provider_factory=NoTicketmaster, dice_factory=FakeDice)
    assert report["held_notifications"] == 0 and len(delivered[-1]) == 1
