"""Evidence-based review guidance. Never makes publication decisions."""
import re
from collections import Counter, defaultdict


def positive(value):
    try:
        number = int(value)
        return number if 0 < number <= 1000 else None
    except (TypeError, ValueError):
        return None


def summarize(job, files):
    concerns, notes = [], []
    if not files:
        concerns.append('No prepared tracks are available.')
    skipped = job.get('skipped_files') or []
    if skipped:
        names = ', '.join(entry.get('name') or 'a file' for entry in skipped[:5])
        more = f' and {len(skipped) - 5} more' if len(skipped) > 5 else ''
        concerns.append(f'{len(skipped)} source file(s) did not decode cleanly and were left out: {names}{more}. Repair or replace them and import again.')
    candidate = job.get('candidate') or {}
    album = candidate.get('kind') == 'album' or len(files) > 1
    advice = job.get('advice') or {}
    result = advice.get('result') if advice.get('status') == 'complete' else {}
    result = result or {}
    by_id = {entry.get('id'): entry for entry in result.get('files', [])}
    duplicate_count = sum(bool(f.get('duplicate') or f.get('duplicate_of')) for f in files)
    if duplicate_count:
        concerns.append(f'{duplicate_count} identical track(s) already in the library. Adding them reuses the existing audio and tags.')
    possible = sum(bool(f.get('possible_duplicates')) for f in files)
    if possible:
        concerns.append(f'{possible} track(s) have another library version. Compare the recordings.')
    warnings = []
    for file in files:
        innocuous = set(by_id.get(file.get('id'), {}).get('innocuous_warnings', []))
        warnings.extend(w for w in file.get('warnings', []) if w not in innocuous)
        for field in ('artist', 'title'):
            if not str((file.get('proposed') or {}).get(field) or file.get(field) or '').strip():
                concerns.append(f'Missing {field} on {file.get("title") or file.get("filename") or "a track"}.')
    if warnings:
        concerns.append(f'{len(warnings)} metadata concern(s). Check the affected tracks below.')
    if result.get('status') == 'check':
        concerns.append(result.get('summary') or 'AI advice suggests checking the metadata.')
    completeness = None
    if len(files) == 1 and (files[0].get('duration') or 0) > 1200:
        concerns.append('This is one recording longer than 20 minutes. Confirm you want a single long track, rather than separate album tracks.')
    if album:
        if any(not str(f.get('album') or '').strip() for f in files):
            concerns.append('Some tracks have no album name. Set the album tags before approving.')
        if any(not positive(f.get('track_number')) for f in files):
            notes.append('Some tracks have no track number; numbering checks are limited.')
        for field, label in [('album', 'album names'), ('album_artist', 'album artists')]:
            values = {str(f.get(field) or '').strip().casefold() for f in files}
            if len(values) > 1:
                concerns.append(f'Inconsistent {label}, including empty tags. Check the album tags.')
        formats = {f.get('format') for f in files if f.get('format')}
        if len(formats) > 1:
            notes.append('Mixed file formats: ' + ', '.join(sorted(formats)) + '.')
        source_files = candidate.get('files') or candidate.get('source_files') or []
        expected = len(source_files) if source_files else positive(candidate.get('file_count'))
        if expected and expected != len(files):
            concerns.append(f'{len(files)} prepared tracks from {expected} source files. Check for missing tracks or duplicate source files.')
        discs = defaultdict(list)
        for file in files:
            discs[positive(file.get('disc_number')) or 1].append(file)
        disc_totals = {positive(f.get('disc_total')) for f in files} - {None}
        missing_discs = sorted(set(range(1, max(disc_totals, default=max(discs, default=1)) + 1)) - set(discs))
        if missing_discs:
            concerns.append('Missing disc numbers ' + ', '.join(map(str, missing_discs)) + '.')
        if len(disc_totals) > 1:
            concerns.append('Conflicting embedded disc totals.')
        verified = not missing_discs and len(disc_totals) <= 1
        for disc, tracks in sorted(discs.items()):
            numbers = [positive(f.get('track_number')) for f in tracks]
            totals = {positive(f.get('track_total')) for f in tracks} - {None}
            known = [n for n in numbers if n]
            repeated = sorted(n for n, count in Counter(known).items() if count > 1)
            if repeated:
                concerns.append(f'Disc {disc}: repeated track numbers ' + ', '.join(map(str, repeated)) + '.')
            upper = max(totals) if totals else max(known, default=0)
            missing = sorted(set(range(1, upper + 1)) - set(known))
            if missing:
                concerns.append(f'Disc {disc}: missing track numbers ' + ', '.join(map(str, missing[:30])) + ('…' if len(missing) > 30 else '') + '.')
            if len(totals) > 1:
                concerns.append(f'Disc {disc}: conflicting embedded track totals.')
            if not known or None in numbers or len(totals) != 1 or repeated or missing or len(tracks) != upper:
                verified = False
        completeness = 'Track numbering matches embedded totals. Album edition is unverified.' if verified else 'Album completeness is unverified. Check the source track list and edition.'
        versions = defaultdict(list)
        for f in files:
            text = ' '.join(str(f.get(k) or '') for k in ('title', 'album'))
            labels = [label for label, pattern in [('live', r'\blive\b'), ('remix', r'\bremix\b'), ('demo', r'\bdemo\b'), ('acoustic', r'\bacoustic\b'), ('instrumental', r'\binstrumental\b'), ('remaster', r'\bremaster(?:ed)?\b')] if re.search(pattern, text, re.I)]
            versions[', '.join(labels) or 'no version label'].append(f.get('title') or 'Untitled')
        if len(versions) > 1:
            concerns.append('Mixed recording-version labels. These may be intentional bonus tracks; check the edition.')
        if any(label != 'no version label' for label in versions):
            notes.extend(label + ': ' + ', '.join(titles[:6]) + ('…' if len(titles) > 6 else '') for label, titles in versions.items())
    return {'status': 'check' if concerns else 'ready', 'label': 'Check before approving' if concerns else 'Ready to approve',
            'concerns': list(dict.fromkeys(concerns)), 'notes': notes, 'completeness': completeness,
            'track_count': len(files), 'album': album}
