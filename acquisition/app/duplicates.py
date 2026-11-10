"""Copies of the same recording: within one download, and across the library.

Two tracks are the same recording when their lead artist and title (credits aside, version
words such as "Remix" kept) match, their lengths are within a few seconds, and neither sits on a
live record the other is not on. The best copy is lossless before lossy, then the higher bitrate,
then the one in the fuller folder (the complete album), then the plainer file name.
"""
import collections
import re
from pathlib import Path

from .matching import clean_artist, key, track_key

SAME_RECORDING_SECONDS = 3
LOSSLESS = {'flac', 'alac', 'wav', 'aiff', 'ape', 'wavpack', 'pcm_s16le', 'pcm_s24le', 'pcm_s16be', 'pcm_s24be'}
LIVE = re.compile(r'\b(live|session|sessions|unplugged)\b', re.I)


def lead(artist):
    """The first credited artist: "JPEGMAFIA x Danny Brown" and "The Roots" are "JPEGMAFIA" and "Roots"."""
    return key(re.sub(r'^the\s+', '', clean_artist(artist), flags=re.I))


def _tag(record, field):
    proposed = record.get('proposed_tags') or record.get('proposed') or {}
    return proposed.get(field) or record.get(field)


def _kbps(record):
    rate = record.get('bitrate') or 0
    return rate / 1000 if rate > 10000 else rate


def _folder(record):
    return str(Path(record.get('path') or '').parent)


def groups(records):
    """Lists of two or more records that are one recording, best copy first."""
    folder_size = collections.Counter(_folder(r) for r in records)

    def rank(r):
        return (str(r.get('format') or '').casefold() in LOSSLESS, _kbps(r), folder_size[_folder(r)],
                -len(Path(r.get('path') or '').name))

    songs = collections.defaultdict(list)
    for r in records:
        artist, title = lead(_tag(r, 'artist')), track_key(_tag(r, 'title'))
        if artist and title and r.get('duration'):
            songs[artist, title, bool(LIVE.search(str(_tag(r, 'album') or '')))].append(r)
    found = []
    for copies in songs.values():
        copies.sort(key=lambda r: r.get('duration') or 0)
        cluster = []
        for r in copies + [None]:
            if r is not None and cluster and (r.get('duration') or 0) - (cluster[0].get('duration') or 0) <= SAME_RECORDING_SECONDS:
                cluster.append(r)
                continue
            if len(cluster) > 1:
                found.append(sorted(cluster, key=rank, reverse=True))
            cluster = [r]
    return found
