import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from concert_alerts.__main__ import DEFAULT_CONFIG, ROOT, main, sync
from concert_alerts.events import select_events
from concert_alerts.library import Artist, ArtistIndex, library_catalogue, read_library
from concert_alerts.notify import batches, deliver, select_digest_dates
from concert_alerts.state import State, run_lock
from concert_alerts.ticketmaster import ProviderError, Ticketmaster


@pytest.fixture
def library(tmp_path):
    path = tmp_path / "navidrome.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE artist(id TEXT, name TEXT, mbz_artist_id TEXT, missing INTEGER)")
    db.executemany("INSERT INTO artist VALUES(?,?,?,?)", [
        ("four-tet", "Four Tet", "mb-four-tet", 0),
        ("vulfpeck", "Vulfpeck", "", 0),
        ("collab", "Earth, Wind & Fire", "", 0),
        ("various", "Various Artists", "", 0),
        ("removed", "Removed artist", "", 1),
    ])
    db.commit()
    db.close()
    return path


@pytest.fixture
def event():
    return {
        "id": "festival-1", "name": "Festival", "url": "https://example.com/tickets",
        "dates": {"start": {"localDate": "2027-07-10", "localTime": "20:00:00"},
                  "timezone": "Europe/Madrid", "status": {"code": "onsale"}},
        "_embedded": {
            "attractions": [{"id": "tm-four-tet", "name": "FOUR TET"},
                            {"id": "tm-vulfpeck", "name": "Vulfpeck"}],
            "venues": [{"id": "venue-1", "name": "Venue", "city": {"name": "Sevilla"},
                        "country": {"countryCode": "ES"}}],
        },
    }


def test_library_is_read_only_and_preserves_combined_names(library):
    before = library.read_bytes()
    artists = read_library(library)
    assert [a.name for a in artists] == ["Four Tet", "Vulfpeck", "Earth, Wind & Fire"]
    assert library.read_bytes() == before
    assert ArtistIndex(artists).match({"name": "Earth"})[0] is None


def test_url_artist_tags_are_reported_without_editing_library(library):
    db = sqlite3.connect(library)
    db.execute("INSERT INTO artist VALUES('url','https:','',0)")
    db.commit()
    db.close()
    before = library.read_bytes()
    artists, issues = library_catalogue(library)
    assert len(artists) == 3
    assert issues == [{"id": "url", "name": "https:", "reason": "URL-like artist tag"}]
    assert library.read_bytes() == before


def test_ambiguous_names_need_override_and_mbid_conflicts_do_not_match():
    index = ArtistIndex([Artist("a", "Same", "a-mbid"), Artist("b", "same", "b-mbid")])
    assert index.match({"id": "tm", "name": "SAME"})[1]["reason"] == "ambiguous"
    assert index.match({"name": "Same", "externalLinks": {"musicbrainz": [{"id": "a-mbid"}]}})[0].id == "a"
    assert index.match({"name": "Same", "externalLinks": {"musicbrainz": [{"id": "other"}]}})[0] is None
    fixed = ArtistIndex(list(index.artists.values()), {"tm": "b"})
    assert fixed.match({"id": "tm", "name": "Same"})[0].id == "b"
    with pytest.raises(ValueError):
        ArtistIndex(list(index.artists.values()), {"tm": "missing"})


def test_all_spain_and_festival_grouping_without_star_filter(library, event):
    events, unresolved, unknown = select_events([event, event], ArtistIndex(read_library(library)))
    assert len(events) == 1
    assert list(events[0]["artists"].values()) == ["Four Tet", "Vulfpeck"]
    assert events[0]["city"] == "Sevilla"
    assert not unresolved and not unknown
    event["id"] = "festival-day-2"
    event["dates"]["start"]["localDate"] = "2027-07-11"
    assert select_events([event], ArtistIndex(read_library(library)))[0][0]["date"] == "2027-07-11"


def test_country_required_but_region_optional(library, event):
    index = ArtistIndex(read_library(library))
    venue = event["_embedded"]["venues"][0]
    venue["country"]["countryCode"] = "PT"
    assert not select_events([event], index)[0]
    venue["country"] = {}
    assert select_events([event], index)[2] == ["festival-1"]
    venue["country"]["countryCode"] = "ES"
    venue["city"]["name"] = "A Coruña"
    assert select_events([event], index)[0][0]["highlight"] == "A Coruña · Galicia"
    venue["city"]["name"] = "Santiago de Compostela"
    assert select_events([event], index)[0][0]["highlight"] == "Galicia"


def test_event_titles_are_not_artist_matches(library, event):
    event["name"] = "Four Tet tribute"
    event["_embedded"]["attractions"] = [{"id": "tribute", "name": "Tribute Band"}]
    selected, unresolved, _ = select_events([event], ArtistIndex(read_library(library)))
    assert selected == []
    assert unresolved[0]["name"] == "Tribute Band"


def test_notification_retry_change_and_missing_event(tmp_path, library, event):
    state = State(tmp_path / "events.db")
    events = select_events([event], ArtistIndex(read_library(library)))[0]
    pending = state.observe(events)
    assert len(state.observe(events)) == 1  # Dry runs do not consume delivery.
    def fail(text):
        raise RuntimeError("offline")
    with pytest.raises(RuntimeError):
        deliver(pending, state, send=fail)
    assert len(state.observe(events)) == 1
    sent = []
    assert deliver(state.observe(events), state, send=sent.append) == 1
    assert "Four Tet" in sent[0] and "Vulfpeck" in sent[0]
    assert state.observe(events) == []
    assert state.observe([]) == []  # Disappearance is not cancellation.
    events[0]["status"] = "cancelled"
    changed = state.observe(events)
    assert changed[0][1] is True
    deliver(changed, state, send=sent.append)
    assert "Updated" in sent[-1] and "CANCELLED" in sent[-1]
    state.close()


def test_each_successful_batch_is_retained_after_later_failure(tmp_path, library, event):
    base = select_events([event], ArtistIndex(read_library(library)))[0][0]
    events = []
    for i in range(24):
        item = copy.deepcopy(base)
        item.update(id=str(i), name="X" * 800, venue="Y" * 800, highlight="Galicia")
        events.append(item)
    state = State(tmp_path / "events.db")
    sent = []
    def send(text):
        if sent:
            raise RuntimeError("offline")
        sent.append(text)
    with pytest.raises(RuntimeError):
        deliver(state.observe(events), state, send=send)
    assert 0 < len(state.observe(events)) < len(events)
    state.close()


def test_lock_blocks_overlapping_runs(tmp_path):
    path = tmp_path / "events.db"
    with run_lock(path):
        with pytest.raises(RuntimeError, match="Another"):
            with run_lock(path):
                pass


def test_notification_metadata_cannot_attach_local_files(library, event):
    event['name'] = 'MEDIA:/etc/passwd'
    event['url'] = 'javascript:alert(1)'
    items = select_events([event], ArtistIndex(read_library(library)))[0]
    messages = list(batches([(items[0], False, 'digest')]))
    assert 'MEDIA:' not in messages[0][1]
    assert 'javascript:' not in messages[0][1]


def test_unicode_messages_fit_telegram_budget(library, event):
    event['name'] = '🎸' * 800
    event['_embedded']['venues'][0]['name'] = '🎸' * 800
    item = select_events([event], ArtistIndex(read_library(library)))[0][0]
    for _, text in batches([(item, False, 'digest')] * 3):
        assert len(text.encode('utf-16-le')) // 2 <= 3800


def test_tour_dates_are_grouped_and_sorted(library, event):
    base = select_events([event], ArtistIndex(read_library(library)))[0][0]
    base.update(name='Four Tet', artists={'four-tet': 'Four Tet'})
    entries = []
    for id, day, city in [('later', '2027-07-12', 'A Coruña'), ('earlier', '2027-07-10', 'Oviedo')]:
        item = {**base, 'id': id, 'date': day, 'city': city,
                'highlight': 'Galicia' if city == 'A Coruña' else ''}
        entries.append((item, False, id))
    messages = list(batches(entries, lookahead_days=90))
    assert len(messages) == 1
    text = messages[0][1]
    assert text.count('Four Tet') == 1 and '2 dates' in text
    assert text.index('10 Jul 2027') < text.index('12 Jul 2027')
    assert '★ A Coruña' in text and 'Next 90 days' in text
    assert text.count('Source: Ticketmaster') == 1
    assert 'Matches your library' not in text and 'incomplete' not in text
    assert 'onsale' not in text


def test_support_slots_and_festivals_keep_event_context(library, event):
    base = select_events([event], ArtistIndex(read_library(library)))[0][0]
    festival = {**base, 'id': 'festival', 'name': 'Festival'}
    support = {**base, 'id': 'support', 'name': 'Other Headliner', 'artists': {'four-tet': 'Four Tet'}}
    solo = {**support, 'id': 'solo', 'name': 'Four Tet'}
    text = '\n'.join(text for _, text in batches([(item, False, item['id']) for item in (festival, support, solo)]))
    assert 'Four Tet, Vulfpeck — Festival' in text
    assert '<b>Four Tet · 2 dates</b>' in text and 'Other Headliner' in text
    assert text.count('https://example.com/tickets') == 3


def test_long_tour_splits_without_losing_dates_or_repeating_footer(library, event):
    base = select_events([event], ArtistIndex(read_library(library)))[0][0]
    base.update(name='Four Tet', artists={'four-tet': 'Four Tet'}, venue='Long venue ' * 20, highlight='Madrid')
    entries = [({**base, 'id': str(i)}, i == 0, str(i)) for i in range(24)]
    messages = list(batches(entries, limit=1800))
    assert len(messages) > 1
    ids = [entry[0]['id'] for batch, _ in messages for entry in batch]
    assert len(ids) == len(set(ids)) == 24
    for _, text in messages:
        assert 'Four Tet' in text
        assert len(text.encode('utf-16-le')) // 2 <= 1800
    text = '\n'.join(text for _, text in messages)
    assert '23 new dates' in text and '1 updated date' in text
    assert text.count('Source: Ticketmaster') == 1
    assert 'continued' in text


def test_grouped_rendering_preserves_delivery_history(tmp_path, library, event):
    state = State(tmp_path / 'events.db')
    events = select_events([event], ArtistIndex(read_library(library)))[0]
    pending = state.observe(events)
    state.delivered(pending)
    list(batches(pending))
    assert state.observe(events) == []
    state.close()


def test_sync_passes_configured_horizon_to_digest(tmp_path, library, event, monkeypatch):
    config = {'navidrome_db': library, 'state_db': tmp_path / 'events.db',
              'report': tmp_path / 'report.json', 'lookahead_days': 90}
    horizons = []
    def fake_deliver(pending, state, *, lookahead_days, catalogue, max_nationwide_dates):
        horizons.append(lookahead_days)
        assert len(catalogue) == 1 and max_nationwide_dates == 3
        return 0
    monkeypatch.setattr('concert_alerts.__main__.deliver', fake_deliver)
    sync(config, [event], send=True)
    assert horizons == [90]


def test_long_tour_filter_uses_all_known_dates_not_just_pending(library, event):
    base = select_events([event], ArtistIndex(read_library(library)))[0][0]
    base.update(artists={'four-tet': 'Four Tet'}, name='Four Tet')
    catalogue = [{**base, 'id': str(i), 'highlight': 'Galicia' if i == 0 else ''} for i in range(5)]
    # A different show title must not let an artist's sixth date escape the filter.
    new = {**base, 'id': 'new', 'name': 'Special show', 'highlight': ''}
    visible, hidden = select_digest_dates([(new, False, 'hash')], catalogue + [new])
    assert visible == [] and hidden[0][0]['id'] == 'new'
    preferred = [(catalogue[0], False, 'hash')]
    assert select_digest_dates(preferred, catalogue)[0] == preferred


def test_small_tours_remain_spain_wide_and_filter_can_be_disabled(library, event):
    base = select_events([event], ArtistIndex(read_library(library)))[0][0]
    dates = [{**base, 'id': str(i)} for i in range(4)]
    entries = [(item, False, item['id']) for item in dates]
    assert len(select_digest_dates(entries[:3], dates[:3])[0]) == 3
    assert select_digest_dates(entries, dates)[0] == []
    assert len(select_digest_dates(entries, dates, max_nationwide_dates=0)[0]) == 4


def test_festival_with_a_rare_artist_still_qualifies_nationwide(library, event):
    base = select_events([event], ArtistIndex(read_library(library)))[0][0]
    tour = [{**base, 'id': str(i), 'artists': {'four-tet': 'Four Tet'}} for i in range(4)]
    festival = {**base, 'id': 'festival'}
    entry = (festival, False, 'hash')
    assert select_digest_dates([entry], tour + [festival])[0] == [entry]


def test_hidden_dates_are_counted_but_never_marked_as_delivered(tmp_path, library, event):
    base = select_events([event], ArtistIndex(read_library(library)))[0][0]
    dates = [{**base, 'id': str(i), 'highlight': 'Galicia' if i == 0 else ''} for i in range(5)]
    state = State(tmp_path / 'events.db')
    sent = []
    assert deliver(state.observe(dates), state, send=sent.append, catalogue=dates) == 1
    assert len(sent) == 1 and '4 elsewhere hidden' in sent[0]
    assert '1 new date' in sent[0]
    pending = state.observe(dates)
    assert {entry[0]['id'] for entry in pending} == {'1', '2', '3', '4'}
    assert deliver(pending, state, send=sent.append, catalogue=dates) == 0
    assert len(sent) == 1
    state.close()


def test_galicia_province_codes_qualify_outside_major_cities(library, event):
    venue = event['_embedded']['venues'][0]
    venue['city']['name'] = 'Boiro'
    venue['state'] = {'stateCode': 'ES-C'}
    assert select_events([event], ArtistIndex(read_library(library)))[0][0]['highlight'] == 'Galicia'


@pytest.mark.parametrize('failed_source', ['ra', 'ticketmaster'])
def test_sources_fail_independently(tmp_path, library, event, failed_source):
    config = {'navidrome_db': library, 'state_db': tmp_path / 'events.db',
              'report': tmp_path / 'report.json', 'ra_enabled': True}
    class TicketmasterSource:
        requests = 1
        def __init__(self, key): pass
        def events(self, start, end):
            if failed_source == 'ticketmaster': raise ProviderError('Ticketmaster unavailable')
            return [event]
        def close(self): pass
    class RASource:
        requests, cache_used, areas = 1, False, []
        def __init__(self, cache_path): pass
        def events(self, start, end):
            if failed_source == 'ra': raise ProviderError('RA unavailable')
            return [{'id': '1', 'title': 'Four Tet', 'date': '2027-07-10T00:00:00',
                     'startTime': '2027-07-10T20:00:00', 'artists': [{'id': 'four', 'name': 'Four Tet'}],
                     'venue': {'id': 'v', 'name': 'RA venue', 'address': '28001 Madrid',
                               'area': {'name': 'Madrid', 'country': {'urlCode': 'ES'}}}}]
        def close(self): pass
    report = sync(config, provider_factory=TicketmasterSource, ra_factory=RASource)
    assert report['matching_events'] == 1
    failed = 'Resident Advisor' if failed_source == 'ra' else 'Ticketmaster'
    assert report['sources'][failed]['status'] == 'unavailable'
    assert report['sent_events'] == 0


def test_offline_fixture_never_calls_ra(tmp_path, library, event):
    config = {'navidrome_db': library, 'state_db': tmp_path / 'events.db',
              'report': tmp_path / 'report.json', 'ra_enabled': True}
    def unexpected(**kwargs): pytest.fail('offline fixture called RA')
    assert sync(config, [event], ra_factory=unexpected)['matching_events'] == 1


def provider(handler, budget=1000):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Ticketmaster("private-test-key", client=client, request_budget=budget, sleep=lambda _: None)


START = datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_paging_and_duplicate_provider_ids(event):
    pages = []
    events = [{**event, "id": str(i)} for i in range(201)]
    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.params["countryCode"] == "ES"
        return httpx.Response(200, json={"page": {"totalElements": 201}, "_embedded": {"events": events[page * 200:(page + 1) * 200]}})
    assert provider(handler).events(START, START + timedelta(days=10)) == events
    assert pages == [0, 1]


def test_large_windows_split_without_crossing_paging_ceiling(event):
    windows = []
    end = START + timedelta(seconds=10)
    def handler(request):
        params = request.url.params
        windows.append((params["startDateTime"], params["endDateTime"]))
        count = 1001 if len(windows) == 1 else 1
        return httpx.Response(200, json={"page": {"totalElements": count}, "_embedded": {"events": [event]}})
    assert provider(handler).events(START, end) == [event]
    assert len(windows) == 3
    assert windows[1][1] == "2027-01-01T00:00:05Z"
    assert windows[2][0] == "2027-01-01T00:00:06Z"


@pytest.mark.parametrize("status", [401, 429, 503])
def test_provider_errors_are_bounded_and_do_not_expose_credentials(status):
    p = provider(lambda _: httpx.Response(status))
    with pytest.raises(ProviderError) as error:
        p.events(START, START + timedelta(days=1))
    assert "private-test-key" not in str(error.value)
    assert p.requests <= 3


def test_malformed_page_and_budget_fail_closed():
    with pytest.raises(ProviderError):
        provider(lambda _: httpx.Response(200, json={})).events(START, START)
    with pytest.raises(ProviderError, match="budget"):
        provider(lambda _: httpx.Response(503), budget=1).events(START, START)


def test_dry_run_writes_report_without_delivery(tmp_path, library, event, monkeypatch):
    config = {"navidrome_db": library, "state_db": tmp_path / "state/events.db", "report": tmp_path / "report.json",
              "ticketmaster_api_key": "private-test-key"}
    monkeypatch.setattr("concert_alerts.__main__.deliver", lambda *_: pytest.fail("dry-run sent a notification"))
    report = sync(config, [event])
    assert report["matching_events"] == 1
    assert report["sent_events"] == 0
    saved = json.loads(config["report"].read_text())
    assert saved["pending_notifications"] == 1
    assert saved["settings"] == {"lookahead_days": 365, "max_nationwide_dates": 3}
    assert "private-test-key" not in config["report"].read_text()
    assert sync(config, [event])["pending_notifications"] == 1


def test_missing_page_results_fail_closed(event):
    data = {"page": {"totalElements": 201}, "_embedded": {"events": [event]}}
    with pytest.raises(ProviderError, match="incomplete pages"):
        provider(lambda _: httpx.Response(200, json=data)).events(START, START)


def test_incomplete_fetch_never_observes_or_delivers(tmp_path, library, monkeypatch):
    config = {"navidrome_db": library, "state_db": tmp_path / "events.db", "report": tmp_path / "report.json"}
    class Broken:
        def __init__(self, key): pass
        def events(self, start, end): raise ProviderError("incomplete")
        def close(self): pass
    monkeypatch.setattr("concert_alerts.__main__.deliver", lambda *_: pytest.fail("incomplete fetch notified"))
    with pytest.raises(ProviderError):
        sync(config, send=True, provider_factory=Broken)
    assert not config["state_db"].exists() and not config["report"].exists()


def test_offline_fixtures_cannot_be_sent():
    with pytest.raises(SystemExit):
        main(["sync", "--events-file", "fixture.json", "--send"])


def test_cli_configuration_and_library(tmp_path, library, capsys):
    assert DEFAULT_CONFIG == ROOT / "config/concerts/config.json"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"navidrome_db": str(library)}))
    config.chmod(0o600)
    assert main(["--config", str(config), "library"]) == 0
    assert json.loads(capsys.readouterr().out)["library_artists"] == 3
    config.chmod(0o644)
    assert main(["--config", str(config), "library"]) == 1
