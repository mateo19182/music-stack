"""Tag analysis and provenance for files already in the library."""
from __future__ import annotations

import json
from pathlib import Path

from mediafile import MediaFile

from .audio_models import SAMPLE_RATE
from .descriptors import fill_descriptors
from .ingestion import BPM_KEY_MAX_SECONDS, MAX_ANALYSIS_SECONDS, _analyze, _decode, _hash, _json, estimate_source
from .keys import camelot
from .tags import FIELDS, read_tags, write_tags  # noqa: F401  re-exported for the API


def update_sidecar(path, tags, analysis_source):
    """Keep a published file's provenance in step with its rewritten tags."""
    sidecar = Path(path).with_name(Path(path).name + '.provenance.json')
    if not sidecar.is_file():
        return
    record = json.loads(sidecar.read_text())
    record.update({field: tags.get(field) for field in FIELDS}, sha256=_hash(path),
                  analysis_source=analysis_source)
    _json(sidecar, record)


def needs_analysis(record, moods=True):
    """Something missing, or a key or genre written in a form that should be rewritten."""
    tag = record.get('key_tag')
    return (not record.get('bpm') or not (record.get('key') or tag) or bool(tag and camelot(tag))
            or not record.get('genres') or bool(record.get('genre_tag')) or not record.get('year')
            or (moods and not record.get('mood')))


def analyze_file(path, catalog=None, models=None, cancelled=lambda: False):
    """Fill missing BPM, key, genre, year and mood, and rewrite key and genre spellings.

    Values already present are never replaced, and a key tag that is not a key
    is left for manual review. Returns (changes, sources, note).
    """
    media = MediaFile(str(path))
    raw = (media.initial_key or '').strip()
    changes, sources = {}, {}
    if raw and camelot(raw) and camelot(raw) != raw:
        changes['key'] = media.initial_key = camelot(raw)
    need_bpm, need_key = not media.bpm_precise, not raw
    long_recording = (media.length or 0) > MAX_ANALYSIS_SECONDS
    note = None
    if (need_bpm or need_key) and (media.length or 0) > BPM_KEY_MAX_SECONDS:
        note = 'long-recording'
    elif need_bpm or need_key:
        bpm, key = _analyze(path, need_bpm, need_key, cancelled)
        if bpm:
            changes['bpm'] = media.bpm_precise = bpm
            sources['bpm'] = estimate_source('bpm')
        if key:
            changes['key'], sources['key'] = key, estimate_source('key')
            media.initial_key = key
        if not bpm and not key:
            note = 'no-confident-estimate'
    found, found_sources = fill_descriptors(media, catalog, models, lambda: _decode(path, SAMPLE_RATE, cancelled),
                                            analyze_audio=not long_recording)
    changes.update(found)
    sources.update(found_sources)
    if changes:
        media.save()
    return changes, sources, note
