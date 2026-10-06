"""Genre and year from public catalogs. Results are suggestions for missing tags only."""
from __future__ import annotations

import logging
import re
import threading
import time

import httpx

from .tags import genres

log = logging.getLogger('acquisition')
USER_AGENT = 'music-stack-acquisition/0.1 (self-hosted)'
# Last.fm artist tags that describe place or listening habits rather than genre.
_NOT_GENRES = {'seen live', 'favorites', 'favourite', 'usa', 'us', 'uk', 'british', 'american', 'english',
               'canadian', 'french', 'german', 'spanish', 'female vocalists', 'male vocalists',
               'singer-songwriter', 'all', 'under 2000 listeners', 'beautiful', 'awesome'}


class _Pace:
    """Serialize requests to one service at its published rate limit."""

    def __init__(self, seconds):
        self.seconds, self.last, self.lock = seconds, 0.0, threading.Lock()

    def __enter__(self):
        self.lock.acquire()
        time.sleep(max(0.0, self.last + self.seconds - time.monotonic()))

    def __exit__(self, *exc):
        self.last = time.monotonic()
        self.lock.release()


_musicbrainz, _discogs, _lastfm = _Pace(1.1), _Pace(1.1), _Pace(0.25)


def _identity(value):
    return ' '.join(re.findall(r'[^\W_]+', str(value or '').casefold()))


def _artist_matches(wanted, found):
    wanted, found = _identity(wanted), _identity(found)
    return bool(wanted and found and (wanted == found or f' {wanted} ' in f' {found} '))


def _year(value):
    match = re.match(r'(\d{4})', str(value or ''))
    return int(match[1]) if match and 1900 <= int(match[1]) <= 2100 else None


def _top_genres(entity):
    ranked = sorted(entity.get('genres') or [], key=lambda g: -int(g.get('count') or 0))
    return genres([g['name'] for g in ranked if int(g.get('count') or 0) > 0][:2])


def recordings(client, artist, title):
    """MusicBrainz recordings whose title and artist match exactly."""
    with _musicbrainz:
        response = client.get('https://musicbrainz.org/ws/2/recording', params={
            'query': f'artist:"{artist}" AND recording:"{title}"', 'fmt': 'json', 'limit': 5})
    response.raise_for_status()
    return [recording for recording in response.json().get('recordings', [])
            if recording.get('score', 0) >= 95
            and _identity(recording.get('title')) == _identity(title)
            and any(_artist_matches(artist, credit.get('name')) for credit in recording.get('artist-credit', []))]


def release(matches, album=None):
    """The release a recording belongs to: the named album if given, else its earliest official album, EP or single."""
    rank = {'Album': 0, 'EP': 1, 'Single': 2}
    found = []
    for recording in matches:
        for entry in recording.get('releases') or []:
            group = entry.get('release-group') or {}
            if album:
                if _identity(entry.get('title')) == _identity(album) and group.get('id'):
                    found.append((0, entry.get('date') or '9999', entry))
            elif (entry.get('status') == 'Official' and not group.get('secondary-types')
                  and group.get('primary-type') in rank and group.get('id')):
                found.append((rank[group['primary-type']], entry.get('date') or '9999', entry))
    if not found:
        return None
    entry = min(found, key=lambda item: item[:2])[2]
    return {'album': entry.get('title'), 'release_group': entry['release-group']['id']}


def cover_art(client, release_group):
    """Front cover from the Cover Art Archive, or None."""
    response = client.get(f'https://coverartarchive.org/release-group/{release_group}/front-500', follow_redirects=True)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    kind = response.headers.get('content-type', '')
    if not kind.startswith('image/') or len(response.content) > 5_000_000:
        return None
    return response.content


def musicbrainz(client, artist, title, need_genre=True, matches=None):
    """Original release year and genres from MusicBrainz.

    Only exact title and artist matches count. Genres come from the recording,
    or from the artist when nobody has voted on the recording.
    """
    if matches is None:
        matches = recordings(client, artist, title)
    year = min((y for y in (_year(r.get('first-release-date')) for r in matches) if y), default=None)
    found = []
    if need_genre and matches:
        with _musicbrainz:
            response = client.get(f'https://musicbrainz.org/ws/2/recording/{matches[0]["id"]}',
                                  params={'inc': 'genres+artists', 'fmt': 'json'})
        response.raise_for_status()
        recording = response.json()
        found = _top_genres(recording)
        credited = [c['artist']['id'] for c in recording.get('artist-credit', [])
                    if c.get('artist', {}).get('id') and _artist_matches(artist, c.get('name'))]
        if not found and credited:
            with _musicbrainz:
                response = client.get(f'https://musicbrainz.org/ws/2/artist/{credited[0]}',
                                      params={'inc': 'genres', 'fmt': 'json'})
            response.raise_for_status()
            found = _top_genres(response.json())
    return year, found


def discogs_release(client, token, artist, title):
    with _discogs:
        response = client.get('https://api.discogs.com/database/search', headers={'Authorization': 'Discogs token=' + token},
                              params={'artist': artist, 'track': title, 'type': 'release', 'per_page': 5})
    response.raise_for_status()
    # Discogs titles read "Artist - Release"; compilations credit someone else.
    releases = [r for r in response.json().get('results', []) if _artist_matches(artist, str(r.get('title', '')).split(' - ')[0])]
    if not releases:
        return [], None
    styled = next((r['style'] for r in releases if r.get('style')), None)
    return genres(styled or releases[0].get('genre') or []), min(
        (year for year in (_year(r.get('year')) for r in releases) if year), default=None)


def lastfm_genres(client, key, artist):
    with _lastfm:
        response = client.get('https://ws.audioscrobbler.com/2.0/', params={
            'method': 'artist.gettoptags', 'artist': artist, 'api_key': key, 'format': 'json', 'autocorrect': 1})
    response.raise_for_status()
    tags = response.json().get('toptags', {}).get('tag', [])
    return genres([tag['name'] for tag in tags if int(tag.get('count') or 0) >= 50
                   and tag.get('name', '').casefold() not in _NOT_GENRES][:2])


class Catalog:
    """MusicBrainz is authoritative; Discogs, then Last.fm artist tags, fill what it lacks."""

    def __init__(self, config):
        self.discogs_token = config.get('discogs_token') or ''
        self.lastfm_key = config.get('lastfm_api_key') or ''
        self.enabled = bool(config.get('catalog_matching', True))
        self.cache = {}
        self.searches = {}

    def _recordings(self, client, artist, title, attempt):
        key = (_identity(artist), _identity(title))
        if key not in self.searches:
            self.searches[key] = attempt('MusicBrainz', lambda: recordings(client, artist, title)) or []
        return self.searches[key]

    def release(self, artist, title, album=None, need_cover=False):
        """{'album', 'release_group', 'cover'} for the track's release, or {} when nothing matches.

        With an album the cover must come from a release of that name; without one,
        the earliest official album, EP or single is used.
        """
        if not self.enabled or not artist or not title:
            return {}
        with httpx.Client(headers={'User-Agent': USER_AGENT}, timeout=10) as client:
            def attempt(name, call):
                try:
                    return call()
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    log.info('%s lookup failed for %s - %s: %s', name, artist, title, exc)
                    return None

            found = release(self._recordings(client, artist, title, attempt), album) or {}
            if found and need_cover:
                found['cover'] = attempt('Cover Art Archive', lambda: cover_art(client, found['release_group']))
        return found

    def lookup(self, artist, title, need_genre=True, need_year=True):
        """Return {'genre': [...], 'year': int, 'sources': {...}} with whatever was found."""
        if not self.enabled or not artist or not title or not (need_genre or need_year):
            return {'sources': {}}
        cache_key = (_identity(artist), _identity(title), need_genre, need_year)
        if cache_key in self.cache:
            return self.cache[cache_key]
        result = {'sources': {}}

        def found(field, value, source):
            if value and field not in result:
                result[field], result['sources'][field] = value, source

        with httpx.Client(headers={'User-Agent': USER_AGENT}, timeout=10) as client:
            def attempt(name, call):
                try:
                    return call()
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    log.info('%s lookup failed for %s - %s: %s', name, artist, title, exc)
                    return None

            matches = self._recordings(client, artist, title, attempt)
            year, names = attempt('MusicBrainz', lambda: musicbrainz(client, artist, title, need_genre, matches)) or (None, [])
            if need_year:
                found('year', year, 'musicbrainz-first-release')
            if need_genre:
                found('genre', names, 'musicbrainz')
            if self.discogs_token and ((need_genre and 'genre' not in result) or (need_year and 'year' not in result)):
                styles, discogs_year = attempt('Discogs', lambda: discogs_release(client, self.discogs_token, artist, title)) or ([], None)
                if need_genre:
                    found('genre', styles, 'discogs')
                if need_year:
                    found('year', discogs_year, 'discogs-earliest-release')
            if need_genre and 'genre' not in result and self.lastfm_key:
                found('genre', attempt('Last.fm', lambda: lastfm_genres(client, self.lastfm_key, artist)), 'lastfm-artist-tags')
        self.cache[cache_key] = result
        return result
