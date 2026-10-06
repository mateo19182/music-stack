import httpx
import pytest
from app import lookup


@pytest.fixture
def respond(monkeypatch):
    for pace in (lookup._musicbrainz, lookup._discogs, lookup._lastfm):
        monkeypatch.setattr(pace, 'seconds', 0)
    routes = {}
    seen = []

    def handler(request):
        seen.append(request)
        body = routes.get(request.url.path, routes.get(request.url.host, {}))
        return httpx.Response(routes.get('status', 200), json=body)

    real = httpx.Client
    monkeypatch.setattr(lookup.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return routes, seen


def test_musicbrainz_year_then_discogs_styles_when_it_has_no_genres(respond):
    routes, seen = respond
    routes['api.discogs.com'] = {'results': [
        {'title': 'Various - Best Of', 'year': '1999', 'style': ['Pop']},
        {'title': 'Artist - Album', 'year': '2017', 'genre': ['Funk / Soul'], 'style': []},
        {'title': 'Artist - Album', 'year': '2013', 'style': ['jazz-funk', 'Soul']}]}
    routes['musicbrainz.org'] = {'recordings': [
        {'id': 'rec', 'score': 100, 'title': 'Song', 'first-release-date': '2013-08-06', 'artist-credit': [{'name': 'Artist'}]},
        {'score': 100, 'title': 'Song', 'first-release-date': '2001', 'artist-credit': [{'name': 'Someone Else'}]},
        {'score': 60, 'title': 'Song', 'first-release-date': '1990', 'artist-credit': [{'name': 'Artist'}]}]}
    found = lookup.Catalog({'discogs_token': 'token', 'lastfm_api_key': 'key'}).lookup('Artist', 'Song')
    assert found == {'genre': ['Jazz Funk', 'Soul'], 'year': 2013,
                     'sources': {'genre': 'discogs', 'year': 'musicbrainz-first-release'}}
    assert all(r.url.host != 'ws.audioscrobbler.com' for r in seen)
    assert all(r.headers['user-agent'].startswith('music-stack') for r in seen)


def test_lastfm_artist_genres_when_discogs_has_nothing(respond):
    routes, _ = respond
    routes['ws.audioscrobbler.com'] = {'toptags': {'tag': [
        {'name': 'seen live', 'count': 100}, {'name': 'Hip-Hop', 'count': 90},
        {'name': 'glitch hop', 'count': 60}, {'name': 'jazz', 'count': 10}]}}
    found = lookup.Catalog({'discogs_token': 'token', 'lastfm_api_key': 'key'}).lookup('Artist', 'Song', need_year=False)
    assert found == {'genre': ['Hip Hop', 'Glitch Hop'], 'sources': {'genre': 'lastfm-artist-tags'}}


def test_failures_and_disabled_lookups_return_nothing(respond):
    routes, _ = respond
    routes['status'] = 503
    assert lookup.Catalog({'discogs_token': 't', 'lastfm_api_key': 'k'}).lookup('Artist', 'Song') == {'sources': {}}
    assert lookup.Catalog({'catalog_matching': False}).lookup('Artist', 'Song') == {'sources': {}}


def test_musicbrainz_genres_win_over_discogs(respond):
    routes, seen = respond
    routes['musicbrainz.org'] = {'recordings': [
        {'id': 'rec', 'score': 100, 'title': 'Song', 'first-release-date': '1994', 'artist-credit': [{'name': 'Artist'}]}]}
    routes['/ws/2/recording/rec'] = {'genres': [], 'artist-credit': [{'name': 'Artist', 'artist': {'id': 'art'}}]}
    routes['/ws/2/artist/art'] = {'genres': [{'name': 'boom bap', 'count': 2}, {'name': 'hip-hop', 'count': 5},
                                             {'name': 'jazz', 'count': 0}]}
    routes['api.discogs.com'] = {'results': [{'title': 'Artist - Album', 'year': '1990', 'style': ['Pop']}]}
    found = lookup.Catalog({'discogs_token': 'token', 'lastfm_api_key': 'key'}).lookup('Artist', 'Song')
    assert found == {'year': 1994, 'genre': ['Hip Hop', 'Boom Bap'],
                     'sources': {'year': 'musicbrainz-first-release', 'genre': 'musicbrainz'}}
    assert all(r.url.host == 'musicbrainz.org' for r in seen)
