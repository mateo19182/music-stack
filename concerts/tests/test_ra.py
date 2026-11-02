import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from concert_alerts.events import select_ra_events
from concert_alerts.library import Artist, ArtistIndex
from concert_alerts.ra import ResidentAdvisor
from concert_alerts.state import State
from concert_alerts.ticketmaster import ProviderError


START = datetime(2026, 11, 2, tzinfo=timezone.utc)


@pytest.fixture
def ra_event():
    return {"id": "10", "title": "Four Tet", "date": "2027-03-19T00:00:00.000",
            "startTime": "2027-03-19T21:30:00.000", "artists": [{"id": "a", "name": "Four Tet"}],
            "area": {"ianaTimeZone": "Europe/Madrid"},
            "venue": {"id": "v", "name": "Sala Maré", "address": "Rúa, 1; 15001 A Coruña; Spain",
                      "area": {"id": "170", "name": "North", "country": {"urlCode": "ES"}}}}


def reader(handler, path=None, budget=300):
    return ResidentAdvisor(cache_path=path, client=httpx.Client(transport=httpx.MockTransport(handler)),
                           sleep=lambda _: None, request_budget=budget)


def area_response():
    return {"data": {"countries": [{"urlCode": "ES", "areas": [{"id": "20", "name": "Barcelona"},
                                                                  {"id": "170", "name": "North"}]}]}}


def test_pagination_multi_day_listing_and_cache(tmp_path, ra_event):
    calls = []
    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if 'countries' in payload['query']:
            return httpx.Response(200, json=area_response())
        assert payload['variables']['filters']['areas'] == {'any': [20, 170]}
        page = payload['variables']['page']
        return httpx.Response(200, json={'data': {'eventListings': {'totalResults': 2, 'data': [
            {'listingDate': f'2027-03-{18 + page}T00:00:00.000', 'event': ra_event}]}}})
    path = tmp_path / 'cache.json'
    p = reader(handle, path)
    assert p.events(START, START + timedelta(days=365)) == [ra_event]
    assert len(calls) == 3
    p = reader(lambda _: pytest.fail('fresh cache should not request RA'), path)
    assert p.events(START, START + timedelta(days=365)) == [ra_event]
    assert p.cache_used and p.requests == 0


@pytest.mark.parametrize('response', [
    {'errors': [{'message': 'schema changed'}]},
    {'data': {'eventListings': {'totalResults': 3, 'data': []}}},
])
def test_bad_or_incomplete_result_does_not_replace_cache(tmp_path, response):
    path = tmp_path / 'cache.json'
    path.write_text('{"old":true}')
    def handle(request):
        return httpx.Response(200, json=area_response() if 'countries' in json.loads(request.content)['query'] else response)
    with pytest.raises(ProviderError):
        reader(handle, path).events(START, START + timedelta(days=365))
    assert json.loads(path.read_text()) == {'old': True}


def test_repeated_page_stops_instead_of_looping(ra_event):
    def handle(request):
        if 'countries' in json.loads(request.content)['query']:
            return httpx.Response(200, json=area_response())
        return httpx.Response(200, json={'data': {'eventListings': {'totalResults': 5, 'data': [
            {'event': ra_event, 'listingDate': ra_event['date']}]}}})
    with pytest.raises(ProviderError, match='no progress'):
        reader(handle).events(START, START + timedelta(days=365))


def test_ra_errors_are_bounded():
    p = reader(lambda _: httpx.Response(503), budget=2)
    with pytest.raises(ProviderError, match='budget'):
        p.events(START, START + timedelta(days=365))
    assert p.requests == 2


def test_normalization_country_postcode_and_artist_ids(ra_event):
    index = ArtistIndex([Artist('four', 'Four Tet')])
    events, unresolved, locations = select_ra_events([ra_event], index)
    event = events[0]
    assert event['id'] == 'ra:10' and event['city'] == 'A Coruña'
    assert event['highlight'] == 'A Coruña · Galicia'
    assert event['time'] == '21:30:00' and event['status'] == 'listed'
    assert not unresolved and not locations
    ra_event['venue']['area']['country']['urlCode'] = 'PT'
    assert not select_ra_events([ra_event], index)[0]


def test_namespaced_ra_override_and_missing_linked_artists(ra_event):
    index = ArtistIndex([Artist('four', 'Four Tet')], {'ra:a': 'four'})
    ra_event['artists'][0]['name'] = 'Alias'
    assert select_ra_events([ra_event], index)[0][0]['artists'] == {'four': 'Four Tet'}
    ra_event['artists'] = []
    events, issues, _ = select_ra_events([ra_event], index)
    assert not events and issues[0]['reason'] == 'no linked RA artists'


def test_duplicate_keeps_delivery_identity_across_outages_and_restart(tmp_path, ra_event):
    event = select_ra_events([ra_event], ArtistIndex([Artist('four', 'Four Tet')]))[0][0]
    ticketmaster = {**event, 'id': 'tm-original', 'source': 'Ticketmaster', 'status': 'onsale',
                   'url': 'https://example.com/tickets', 'name': 'FOUR TET: LIVE'}
    path = tmp_path / 'events.db'
    state = State(path)
    state.delivered(state.observe([ticketmaster]))
    merged, duplicates = state.reconcile([ticketmaster, event])
    assert len(merged) == 1 and duplicates[0]['canonical_id'] == 'tm-original'
    assert state.observe(merged) == []
    state.close()
    state = State(path)
    # Only RA succeeds next time: source/title/URL changes are not another alert.
    only_ra, _ = state.reconcile([event])
    assert only_ra[0]['id'] == 'tm-original' and state.observe(only_ra) == []
    event['date'] = '2027-03-20'
    changed, _ = state.reconcile([event])
    assert state.observe(changed)[0][1] is True
    state.close()


def test_ra_seen_first_does_not_repeat_when_ticketmaster_arrives(tmp_path, ra_event):
    event = select_ra_events([ra_event], ArtistIndex([Artist('four', 'Four Tet')]))[0][0]
    state = State(tmp_path / 'events.db')
    state.delivered(state.observe([event]))
    tm = {**event, 'id': 'tm', 'source': 'Ticketmaster', 'status': 'onsale'}
    merged, _ = state.reconcile([tm, event])
    assert len(merged) == 1 and merged[0]['id'] == 'ra:10'
    assert state.observe(merged) == []
    state.close()


def test_uncertain_duplicates_remain_separate(tmp_path, ra_event):
    event = select_ra_events([ra_event], ArtistIndex([Artist('four', 'Four Tet')]))[0][0]
    state = State(tmp_path / 'events.db')
    other = {**event, 'id': 'tm', 'source': 'Ticketmaster', 'time': '23:00:00'}
    assert len(state.reconcile([other, event])[0]) == 2
    other['time'] = event['time']
    other['time_unknown'] = True
    assert len(state.reconcile([other, event])[0]) == 2
    state.close()


def test_past_events_are_excluded_and_window_survives_multiple_records(ra_event):
    index = ArtistIndex([Artist('four', 'Four Tet')])
    events = [ra_event, {**ra_event, 'id': 'second'},
              {**ra_event, 'id': 'past', 'startTime': '2026-11-01T21:00:00.000'}]
    selected, _, _ = select_ra_events(events, index, START, START + timedelta(days=365))
    assert {event['id'] for event in selected} == {'ra:10', 'ra:second'}


def test_ra_listing_cannot_undo_known_cancellation(tmp_path, ra_event):
    event = select_ra_events([ra_event], ArtistIndex([Artist('four', 'Four Tet')]))[0][0]
    cancelled = {**event, 'id': 'tm', 'source': 'Ticketmaster', 'status': 'cancelled'}
    state = State(tmp_path / 'events.db')
    state.delivered(state.observe([cancelled]))
    state.reconcile([cancelled, event])
    fallback, _ = state.reconcile([event])
    assert fallback[0]['status'] == 'cancelled'
    assert state.observe(fallback) == []
    state.close()


def test_non_performance_and_unlocated_listings_stay_out_of_alerts(ra_event):
    index = ArtistIndex([Artist('four', 'Four Tet')])
    listening = {**ra_event, 'title': 'Listening Session — Four Tet'}
    events, issues, _ = select_ra_events([listening], index)
    assert not events and 'performance unconfirmed' in issues[0]['reason']
    unlocated = copy.deepcopy(ra_event)
    unlocated['venue'].update(name='TBA', address=None)
    events, _, locations = select_ra_events([unlocated], index)
    assert not events and locations == ['ra:10']
