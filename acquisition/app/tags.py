"""Tag fields shared by ingestion, library edits and analysis."""
from __future__ import annotations

import copy
import re
from pathlib import Path

from mediafile import (ListMediaField, ListStorageStyle, MediaField, MediaFile, MP3ListStorageStyle,
                       MP4ListStorageStyle, MP4StorageStyle)

from .keys import key_fields

# ID3/Vorbis/RIFF BPM is textual and can retain fractions. MP4's native
# tmpo is integer-only, so preserve exact BPM in the common freeform tag too.
if not hasattr(MediaFile, 'bpm_precise'):
    bpm_styles = copy.deepcopy(MediaFile.__dict__['bpm']._styles)
    for bpm_style in bpm_styles:
        bpm_style.float_places = 8
    MediaFile.add_field('bpm_precise', MediaField(
        MP4StorageStyle('----:com.apple.iTunes:BPM', as_type=str, float_places=8),
        *bpm_styles, out_type=float))

# Navidrome reads mood from TMOO (ID3v2.4), MOOD (Vorbis/APE) and the iTunes freeform atom.
if not hasattr(MediaFile, 'mood'):
    MediaFile.add_field('mood', ListMediaField(
        MP3ListStorageStyle('TMOO'),
        MP4ListStorageStyle('----:com.apple.iTunes:MOOD'),
        ListStorageStyle('MOOD'),
    ))

FIELDS = ('artist', 'title', 'album', 'genre', 'year', 'mood', 'bpm', 'key')
_ATTRIBUTES = {'bpm': 'bpm_precise', 'key': 'initial_key', 'genre': 'genres'}
_UPPER = {'edm': 'EDM', 'idm': 'IDM', 'uk': 'UK', 'us': 'US', 'dj': 'DJ', 'dnb': 'DnB', 'rnb': 'R&B', 'mpb': 'MPB'}


def genre_name(value):
    """One spelling per genre: "hip-hop", "Hip-Hop" and "hip hop" all become "Hip Hop"."""
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not text:
        return None
    text = re.sub(r'\bhip[\s-]?hop\b', 'hip hop', text, flags=re.I)
    return re.sub(r"[^\W\d_]+", lambda word: _UPPER.get(word[0].casefold()) or word[0][:1].upper() + word[0][1:].lower(), text)


def split_values(value):
    """Tag lists from an edit form ("House; Disco" or "House, Disco") or a list."""
    items = value if isinstance(value, (list, tuple)) else re.split(r'\s*[;,]\s*', str(value or ''))
    return [str(item).strip() for item in items if str(item).strip()]


def unique(values):
    seen, result = set(), []
    for value in values:
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result


def genres(values):
    return unique(genre_name(value) for value in split_values(values))


def read_tags(path):
    media = MediaFile(str(path))
    raw = [g for g in (media.genres or []) if g]
    names = genres(raw)
    return {'artist': media.artist, 'title': media.title, 'album': media.album,
            'genre': names[0] if names else None, 'genres': names,
            'genre_tag': raw if raw != names else None, 'year': media.year or None,
            'mood': unique(split_values(media.mood or [])), 'bpm': media.bpm_precise or None,
            **key_fields(media.initial_key), 'mtime_ns': Path(path).stat().st_mtime_ns}


def set_tag(media, field, value):
    """Set one validated field. Empty values remove the tag."""
    if field == 'genre':
        value = genres(value)
    elif field == 'mood':
        value = unique(split_values(value))
    setattr(media, _ATTRIBUTES.get(field, field), value or None)


def write_tags(path, changes):
    media = MediaFile(str(path))
    for field, value in changes.items():
        set_tag(media, field, value)
    media.save()
