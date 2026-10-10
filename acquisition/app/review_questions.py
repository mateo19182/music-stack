"""What happens to each prepared track, and the decisions that still need a person.

A track with no questions can be added without review when its owner turned
automatic adding on. Identical copies and equal or worse copies of a recording
already in the library are skipped; clearly better copies replace it.
"""
import re

LONG_SECONDS = 20 * 60
SAME_RECORDING_SECONDS = 3
LOSSLESS = {'flac', 'alac', 'wav', 'aiff', 'ape', 'wavpack', 'pcm_s16le', 'pcm_s24le', 'pcm_s16be', 'pcm_s24be'}


def _identity(value):
    return ' '.join(re.findall(r'[^\W_]+', str(value or '').casefold()))


def _same(a, b):
    a, b = _identity(a), _identity(b)
    # "Sauna" and "Sauna (2025 Remaster)" are the same request; "Sauna" and "Zzz" are not.
    return bool(a and b and (a == b or f' {a} ' in f' {b} ' or f' {b} ' in f' {a} '))


def _quality(fmt, kbps):
    return str(fmt or '').casefold() in LOSSLESS, float(kbps or 0)


def better(new, old):
    """Lossless beats lossy; between lossy copies only a clearly higher bitrate counts."""
    (new_lossless, new_rate), (old_lossless, old_rate) = new, old
    if new_lossless != old_lossless:
        return new_lossless
    return not new_lossless and new_rate >= old_rate * 1.25 and new_rate > 0


def same_recording(record, versions):
    """The library copies that are this recording, or None when that is unclear.

    The same title on the same album is the same recording, whatever the lengths.
    On another album it is the same recording only when the lengths match; otherwise
    it is another release of the song and the new track is added alongside it.
    """
    album = (record.get('proposed_tags') or record.get('proposed') or {}).get('album') or record.get('album')
    duration = record.get('duration') or 0
    same = []
    for version in versions:
        if album and _same(album, version.get('album')):
            same.append(version)
        elif duration and abs((version.get('duration') or 0) - duration) <= SAME_RECORDING_SECONDS:
            same.append(version)
        elif not (album and version.get('album')):
            return None
    return same


def version_plan(record, versions):
    """'replace' or 'skip' when library copies are the same recording, 'ask' when unclear."""
    same = same_recording(record, versions)
    if same is None:
        return 'ask'
    if not same:
        return None
    bitrate = record.get('bitrate') or 0
    new = _quality(record.get('format'), bitrate / 1000 if bitrate > 10000 else bitrate)
    if all(better(new, _quality(v.get('format'), v.get('bitrate'))) for v in same):
        return 'replace' if all(v.get('replaceable') for v in same) else 'ask'
    return 'skip'


def plan(record, track_count=1):
    """{'action': 'add'|'skip'|'replace', 'questions': [...]} for one prepared record."""
    if record.get('duplicate_path') or record.get('duplicate_file_id') or record.get('duplicate_of'):
        return {'action': 'skip', 'reason': 'identical', 'questions': []}
    proposed = record.get('proposed_tags') or record.get('proposed') or {}
    current = {field: proposed.get(field) or record.get(field) for field in ('artist', 'title')}
    found = []
    missing = [field for field in ('artist', 'title') if not str(current[field] or '').strip()]
    if missing:
        found.append({'kind': 'missing', 'fields': missing})
    candidate = record.get('candidate') or {}
    if track_count == 1:
        for field in ('artist', 'title'):
            requested = candidate.get('requested_' + field)
            if requested and current[field] and not _same(requested, current[field]):
                found.append({'kind': 'mismatch', 'field': field, 'requested': requested, 'found': current[field]})
        if (record.get('duration') or 0) > LONG_SECONDS:
            found.append({'kind': 'long', 'duration': record['duration']})
    versions = record.get('possible_duplicates') or []
    action = version_plan(record, versions)
    if action == 'ask':
        found.append({'kind': 'version', 'library': versions})
    if found:
        return {'action': 'ask', 'questions': found}
    if action == 'skip':
        return {'action': 'skip', 'reason': 'not-better', 'questions': []}
    if action == 'replace':
        return {'action': 'replace', 'replace': same_recording(record, versions), 'questions': []}
    return {'action': action or 'add', 'questions': []}
