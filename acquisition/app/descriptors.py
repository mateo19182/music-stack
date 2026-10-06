"""Fill missing genre, year and mood: catalogs first, then the audio models."""
from __future__ import annotations

from .tags import genres, set_tag, split_values, unique

AUDIO_ESTIMATE = 'essentia-discogs-effnet-estimate'


def fill_descriptors(media, catalog, models, decode, analyze_audio=True):
    """Fill missing values on an open MediaFile and rewrite genre spellings.

    Existing values are kept. Returns (changes, sources); a change without a
    source only normalized the spelling of an existing genre.
    """
    changes, sources = {}, {}
    raw = [g for g in (media.genres or []) if g]
    current = genres(raw)
    if current and current != raw:
        changes['genre'] = current
    need_genre, need_year = not current, not media.year
    need_mood = not unique(split_values(media.mood or []))
    if (need_genre or need_year) and catalog is not None:
        found = catalog.lookup(media.artist, media.title, need_genre, need_year)
        for field in ('genre', 'year'):
            if found.get(field):
                changes[field], sources[field] = found[field], found['sources'][field]
    want_genre = need_genre and 'genre' not in sources
    if (need_mood or want_genre) and analyze_audio and models is not None and models.available:
        described = models.describe(decode())
        if want_genre and described['genre']:
            changes['genre'], sources['genre'] = genres(described['genre']), AUDIO_ESTIMATE
        if need_mood and described['mood']:
            changes['mood'], sources['mood'] = described['mood'], AUDIO_ESTIMATE
    for field, value in changes.items():
        set_tag(media, field, value)
    return changes, sources
