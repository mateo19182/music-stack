"""Copy-based, restart-safe ingestion. Source downloads are never changed."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import sys
from pathlib import Path

import numpy as np
from beets.library import Item, Library
from mediafile import MediaFile

from .keys import camelot, key_fields
from .audio_models import SAMPLE_RATE, models_at
from .descriptors import fill_descriptors
from .lookup import Catalog
from .tags import set_tag, split_values


class IngestionCancelled(RuntimeError):
    pass


# A full album, live set or mix in one file has no single tempo or key.
MAX_ANALYSIS_SECONDS = 20 * 60
_audio_index_lock = threading.Lock()


def _hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path, data):
    # Unique per thread: parallel processing workers share the audio index.
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(data, indent=2))
    os.replace(temporary, path)


def _audio_hash(path, cancelled=lambda: False):
    result = _run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
                   '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-'], 300, cancelled)
    return result.stdout.decode().strip().split('=', 1)[1]


def _failure_reason(exc):
    if isinstance(exc, subprocess.TimeoutExpired):
        return 'Decoding timed out'
    if isinstance(exc, subprocess.CalledProcessError):
        error = exc.stderr.decode(errors='replace') if isinstance(exc.stderr, bytes) else str(exc.stderr or '')
        lines = [line.split('] ', 1)[-1].strip() for line in error.splitlines() if line.strip()]
        return ('Audio does not decode cleanly: ' + lines[-1])[:200] if lines else 'Audio does not decode cleanly'
    return str(exc)[:200] or 'Unreadable audio file'


def _component(value):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', str(value or 'Unknown'))
    return value.strip(' .')[:120] or 'Unknown'


def _run(args, timeout=300, cancelled=lambda: False):
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        deadline = time.monotonic() + timeout
        while True:
            if cancelled():
                child.kill()
                child.communicate()
                raise IngestionCancelled('Ingestion cancelled; source downloads retained')
            if time.monotonic() >= deadline:
                child.kill()
                child.communicate()
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                output, error = child.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, args, output, error)
        return subprocess.CompletedProcess(args, child.returncode, output, error)


def _essentia():
    try:
        import essentia.standard as es
        return es
    except ImportError:
        return None


def _decode(path, rate, cancelled=lambda: False):
    """Mono float32 audio from at most the first three minutes."""
    raw = _run(['ffmpeg', '-v', 'error', '-i', str(path), '-t', '180',
                '-ac', '1', '-ar', str(rate), '-f', 'f32le', '-'], 240, cancelled).stdout
    return np.frombuffer(raw, dtype='<f4').copy()


def _genre_text(media):
    return '; '.join(media.genres or []) or None


def estimate_source(field):
    if _essentia():
        return {'bpm': 'essentia-rhythm-multifeature-estimate', 'key': 'essentia-edma-key-estimate'}[field]
    return {'bpm': 'spectral-flux-autocorrelation-v1', 'key': 'chroma-profile-correlation-v1'}[field]


def _analyze(path, need_bpm, need_key, cancelled=lambda: False):
    """Estimate from at most three minutes. Existing tags always take precedence."""
    if not need_bpm and not need_key:
        return None, None
    engine = _essentia()
    if engine is not None:
        raw = _run(['ffmpeg', '-v', 'error', '-i', str(path), '-t', '180',
                    '-ac', '1', '-ar', '44100', '-f', 'f32le', '-'], 240, cancelled).stdout
        audio = np.frombuffer(raw, dtype='<f4').copy()
        if len(audio) < 44100 or np.max(np.abs(audio)) < 1e-6:
            return None, None
        bpm = key = None
        if need_bpm:
            estimate, _, confidence, _, _ = engine.RhythmExtractor2013(method='multifeature')(audio)
            if confidence > 0 and estimate > 0:
                bpm = round(float(estimate), 2)
        if need_key:
            pitch, scale, strength = engine.KeyExtractor(sampleRate=44100, profileType='edma')(audio)
            if strength >= 0.4:
                key = camelot(pitch + ('m' if scale == 'minor' else ''))
        return bpm, key
    raw = _run(['ffmpeg', '-v', 'error', '-i', str(path), '-t', '180',
                '-ac', '1', '-ar', '11025', '-f', 'f32le', '-'], 240, cancelled).stdout
    audio = np.frombuffer(raw, dtype='<f4')
    if len(audio) < 4096 or float(np.max(np.abs(audio))) < 1e-6:
        return None, None
    window = 2048
    hop = 256
    frames = np.lib.stride_tricks.sliding_window_view(audio, window)[::hop]
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(window), axis=1))
    bpm = key = None
    if need_bpm:
        flux = np.maximum(np.diff(spectrum, axis=0), 0).sum(axis=1)
        flux -= flux.mean()
        n = 1 << (2 * len(flux) - 1).bit_length()
        transformed = np.fft.rfft(flux, n=n)
        correlation = np.fft.irfft(transformed * transformed.conj(), n=n)[:len(flux)]
        low, high = int(11025 / hop * 60 / 180), int(11025 / hop * 60 / 60)
        if len(correlation) > high and np.max(correlation[low:high + 1]) > 0:
            lag = low + int(np.argmax(correlation[low:high + 1]))
            bpm = round(60 * 11025 / hop / lag)
    if need_key:
        frequencies = np.fft.rfftfreq(window, 1 / 11025)
        valid = (frequencies >= 65) & (frequencies <= 4000)
        pitches = np.rint(69 + 12 * np.log2(frequencies[valid] / 440)).astype(int) % 12
        chroma = np.bincount(pitches, weights=spectrum[:, valid].sum(axis=0), minlength=12)
        major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
        minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
        scores = [(float(np.corrcoef(chroma, np.roll(profile, root))[0, 1]), root, mode)
                  for mode, profile in [('major', major), ('minor', minor)] for root in range(12)]
        _, root, mode = max(scores)
        key = camelot(['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'][root] + ('m' if mode == 'minor' else ''))
    return bpm, key


_CATALOG_SCRIPT = r"""
import json, sys
from beets import config, plugins
from beets.library import Item
from beets.autotag import Source, tag_item
config['plugins'] = ['musicbrainz']
config['musicbrainz']['searchlimit'] = 3
plugins.load_plugins()
proposal = tag_item(Source.from_item(Item.from_path(sys.argv[1])))
out = []
for rank, match in enumerate(proposal.candidates[:3]):
    info = match.info
    out.append(dict(title=info.title, artist=info.artist, track_id=info.track_id,
                    distance=float(match.distance), recommendation=proposal.recommendation.name if rank == 0 else 'alternative',
                    length=info.length))
print(json.dumps(out))
"""


def _catalog(path, cancelled):
    # Isolate plugin/network work so a failed lookup cannot hold the worker forever.
    try:
        response = _run([sys.executable, '-c', _CATALOG_SCRIPT, str(path)], 12, cancelled)
        matches = [m for m in json.loads(response.stdout) if m.get("recommendation") in {"strong", "medium", "alternative"} and m.get("distance", 1) <= 0.25]
        return matches, None if matches else "No confident catalog match; check the source metadata before publishing."
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError, OSError):
        return [], 'Catalog lookup unavailable; existing metadata is preserved.'


class Ingestor:
    def __init__(self, config):
        self.config = dict(config)
        self.state = Path(config.get('state_root', '/state')) / 'ingestion'
        self.library = Path(config.get('library_root', '/library')).resolve()
        self.beets_path = Path(config.get('state_root', '/state')) / 'beets' / 'library.db'
        self.state.mkdir(parents=True, exist_ok=True)
        self.library.mkdir(parents=True, exist_ok=True)
        self.beets_path.parent.mkdir(parents=True, exist_ok=True)
        # Fail startup immediately if the runtime cannot initialize beets.
        Library(str(self.beets_path), directory=str(self.library))
        self.catalog = Catalog(self.config)
        self.models = models_at(self.config.get('models_root') or str(Path(self.config.get('state_root', '/state')) / 'models'))

    def process(self, paths, candidate, job_id, progress=lambda *args: None, cancelled=lambda: False, _preserve_tags=False, skipped=None):
        def check():
            if cancelled():
                raise IngestionCancelled('Ingestion cancelled; source downloads retained')
        job = self.state / hashlib.sha256(str(job_id).encode()).hexdigest()
        job.mkdir(exist_ok=True)
        manifest_path = job / 'manifest.json'
        with open(self.state / 'publish.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            known = {}
            for sidecar in self.library.rglob('*.provenance.json'):
                try:
                    record = json.loads(sidecar.read_text())
                    if Path(record['path']).is_file():
                        known[record['original_sha256']] = record
                except (ValueError, KeyError, OSError):
                    continue
            results = []
            database = Library(str(self.beets_path), directory=str(self.library))
            for number, source in enumerate(paths):
                check()
                source = Path(source).resolve(strict=True)
                if not source.is_file():
                    raise ValueError('Downloaded source must be a regular file')
                original = _hash(source)
                progress(f'Processing {number + 1}/{len(paths)}: {source.name}')
                record = manifest.get(original) or known.get(original)
                if record and Path(record['path']).is_file():
                    self._register(database, Path(record['path']))
                    duplicate = dict(record, duplicate=True, source_path=str(source))
                    results.append(duplicate)
                    manifest[original] = record
                    _json(manifest_path, manifest)
                    continue
                try:
                    probe = json.loads(_run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)], cancelled=cancelled).stdout)
                    streams = [s for s in probe['streams'] if s.get('codec_type') == 'audio']
                    if len(streams) != 1:
                        raise ValueError('Expected exactly one audio stream')
                    stream = streams[0]
                    # Decode the entire source before admitting it to the library.
                    _run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(source), '-map', '0:a:0', '-f', 'null', '-'], 1800, cancelled)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
                    # With a skipped list, one unreadable file must not fail the rest of the job.
                    if skipped is None:
                        raise
                    skipped.append({'path': str(source), 'name': source.name, 'reason': _failure_reason(exc)})
                    continue
                check()
                codec = stream.get('codec_name')
                container = probe['format'].get('format_name', '').split(',')
                suffix = {"mp3": '.mp3', 'flac': '.flac', 'ogg': '.ogg', 'opus': '.opus', 'wav': '.wav', 'aac': '.aac', 'aiff': '.aiff'}.get(container[0])
                if 'mov' in container or 'm4a' in container:
                    suffix = '.m4a'
                if not suffix:
                    raise ValueError(f'Unsupported audio container: {container[0]}')
                work = job / ('working' + suffix)
                shutil.copy2(source, work)
                media = MediaFile(str(work))
                if not media.title and not _preserve_tags:
                    # A request often names the original song, so prefer the downloaded filename.
                    media.title = source.stem
                if not media.artist and len(paths) == 1 and not _preserve_tags:
                    media.artist = candidate.get('artist') or candidate.get('requested_artist') or ''
                if candidate.get('source') == 'youtube' and len(paths) == 1 and not media.album and not _preserve_tags:
                    # Uploads rarely declare an album; without one Navidrome lists the file
                    # under Unknown Album, so treat it as a single named after its title.
                    media.album = media.title
                duration = float(stream.get('duration') or probe['format'].get('duration') or 0)
                skipped = None
                if not _preserve_tags:
                    # Valid keys are rewritten in Camelot; anything else is not a key and gets estimated.
                    media.initial_key = camelot(media.initial_key)
                if _preserve_tags:
                    bpm = key = None
                elif duration > MAX_ANALYSIS_SECONDS and (not media.bpm_precise or not media.initial_key):
                    bpm = key = None
                    skipped = 'long-recording'
                else:
                    bpm, key = _analyze(work, not media.bpm_precise, not media.initial_key, cancelled)
                estimates = {}
                if bpm and not media.bpm_precise:
                    media.bpm_precise = bpm
                    estimates['bpm'] = estimate_source('bpm')
                if key and not media.initial_key:
                    media.initial_key = key
                    estimates['key'] = estimate_source('key')
                if not _preserve_tags:
                    progress(f'Looking up genre, year and mood {number + 1}/{len(paths)}')
                    _, found = fill_descriptors(media, self.catalog, self.models,
                                                lambda: _decode(work, SAMPLE_RATE, cancelled),
                                                analyze_audio=duration <= MAX_ANALYSIS_SECONDS)
                    estimates.update(found)
                media.save()
                check()
                item = Item.from_path(str(work))
                prefix = f'{item.track:02d} - ' if item.track else ''
                destination = self.library / _component(item.albumartist or item.artist) / _component(item.album or 'Singles') / (prefix + _component(item.title) + work.suffix)
                if destination.exists():
                    destination = destination.with_name(destination.stem + ' [' + original[:12] + ']' + destination.suffix)
                if destination.exists():
                    raise RuntimeError('Publication destination already exists without matching provenance')
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.parent.resolve().is_relative_to(self.library):
                    raise ValueError('Publication directory escapes the library')
                temporary = destination.with_name('.' + destination.name + '.publishing')
                shutil.copy2(work, temporary)
                record = dict(path=str(destination), source_path=str(source), sha256=_hash(temporary),
                              original_sha256=original, audio_sha256=_audio_hash(source, cancelled), title=item.title, artist=item.artist,
                              album=item.album, album_artist=item.albumartist, track_number=item.track,
                              track_total=media.tracktotal, disc_number=media.disc, disc_total=media.disctotal,
                              format=stream.get('codec_name'), bitrate=int(stream.get('bit_rate') or probe['format'].get('bit_rate') or 0),
                              bit_depth=int(stream.get('bits_per_raw_sample') or stream.get('bits_per_sample') or 0),
                              duration=duration,
                              bpm=media.bpm_precise or None, key=media.initial_key or None, genre=_genre_text(media),
                              year=media.year or None, mood=split_values(media.mood or []) or None,
                              analysis_source=estimates, analysis_skipped=skipped, size=temporary.stat().st_size, duplicate=False,
                              candidate=candidate, job_id=job_id)
                sidecar = destination.with_name(destination.name + '.provenance.json')
                # Provenance first allows a retry to recover after atomic audio publication.
                _json(sidecar, record)
                os.replace(temporary, destination)
                self._register(database, destination)
                manifest[original] = record
                known[original] = record
                _json(manifest_path, manifest)
                results.append(record)
            return results

    def prepare(self, paths, candidate, job_id, progress=lambda *args: None, cancelled=lambda: False, skipped=None):
        """Prepare tagged copies privately; publication requires publish()."""
        private = self.state / 'prepared' / hashlib.sha256(str(job_id).encode()).hexdigest()
        worker = Ingestor({**self.config, 'state_root': str(private / 'state'), 'library_root': str(private / 'files'),
                           'models_root': str(self.models.root)})
        records = worker.process(paths, candidate, job_id, progress, cancelled, skipped=skipped)
        fields = ('artist', 'title', 'album', 'genre', 'year', 'mood', 'bpm', 'key')
        catalog_enabled = self.config.get('catalog_matching', True) and len(paths) == 1
        for record in records:
            source = MediaFile(record['source_path'])
            record['existing_tags'] = {
                **{field: getattr(source, field) for field in ('artist', 'title', 'album')},
                'genre': _genre_text(source), 'year': source.year or None, 'mood': split_values(source.mood or []) or None,
                'bpm': source.bpm_precise, 'key': source.initial_key}
            record.update(track_total=source.tracktotal, disc_number=source.disc, disc_total=source.disctotal)
            record['proposed_tags'] = {field: record.get(field) for field in fields}
            record['warnings'] = []
            if len(paths) == 1:
                for field in ('artist', 'title'):
                    requested = candidate.get('requested_' + field) or candidate.get(field)
                    embedded = record['existing_tags'].get(field)
                    if requested and embedded and requested.casefold().strip() != embedded.casefold().strip():
                        record['warnings'].append(f'Requested {field} differs from embedded metadata: {requested} / {embedded}. Existing metadata retained.')
            if record.get('analysis_skipped') == 'long-recording':
                record['warnings'].append('Longer than 20 minutes, likely a full album or set: BPM and key were not estimated.')
            embedded_key = record['existing_tags'].get('key')
            if embedded_key and not camelot(embedded_key):
                record['warnings'].append(f'Embedded key "{embedded_key}" is not a musical key and was not kept.')
            if not record.get('album'):
                record['warnings'].append('No album tag. Navidrome will list this under Unknown Album.')
            if not MediaFile(record['path']).art:
                record['warnings'].append('No embedded cover art. Navidrome will show no cover.')
            record['catalog_matches'] = []
            if catalog_enabled and record.get('artist') and record.get('title'):
                progress('Checking catalog metadata')
                matches, warning = _catalog(Path(record['path']), cancelled)
                record['catalog_matches'] = matches
                if warning:
                    record['warnings'].append(warning)
                if matches:
                    best = matches[0]
                    record['warnings'].append(f"Catalog suggests {best.get('artist')} / {best.get('title')} ({best.get('recommendation')} match). Confirm version before changing existing tags.")
            record['prepared_path'] = record['path']
            record['duplicate'] = False
            record['audio_sha256'] = record.get('audio_sha256') or _audio_hash(Path(record['source_path']), cancelled)
            for sidecar in self.library.rglob('*.provenance.json'):
                try:
                    existing = json.loads(sidecar.read_text())
                    if existing.get('original_sha256') == record['original_sha256'] and Path(existing['path']).is_file():
                        record['duplicate'] = True
                        record['duplicate_path'] = existing['path']
                        break
                except (OSError, ValueError, KeyError):
                    continue
            legacy = self._find_existing_audio(record['audio_sha256'], cancelled, progress)
            if legacy:
                record['duplicate'] = True
                record['duplicate_path'] = str(legacy)
                record['warnings'].append('Exact encoded audio already exists. Approval keeps the existing library file and its tags; duplicate import edits do not overwrite it.')
            record['possible_duplicates'] = [] if legacy else self._possible_duplicates(record)
            if record['possible_duplicates']:
                record['warnings'].append('Other library files have the same artist and title but different encoded audio. They may be another version or quality. Review before publishing; these files are not automatically merged.')
        _json(private / 'review.json', records)
        return records

    def publish(self, prepared, edits, job_id, progress=lambda *args: None, cancelled=lambda: False):
        """Publish prepared records. Edits map prepared paths to explicit tag changes."""
        results = []
        for record in prepared:
            path = Path(record.get('prepared_path') or record['path']).resolve(strict=True)
            if not path.is_relative_to((self.state / 'prepared').resolve()):
                raise ValueError('Approval must reference a privately prepared file')
            changes = edits.get(str(path), {}) if isinstance(edits, dict) else {}
            allowed = {'artist', 'title', 'album', 'genre', 'year', 'mood', 'bpm', 'key'}
            if set(changes) - allowed:
                raise ValueError('Unsupported metadata edit')
            if changes:
                media = MediaFile(str(path))
                for field, value in changes.items():
                    set_tag(media, field, value)
                media.save()
            legacy = self._find_existing_audio(record.get('audio_sha256') or _audio_hash(path, cancelled), cancelled, progress)
            if legacy:
                media = MediaFile(str(legacy))
                existing_record = dict(record, path=str(legacy), duplicate=True, size=legacy.stat().st_size, sha256=_hash(legacy),
                                       title=media.title, artist=media.artist, album=media.album, genre=_genre_text(media),
                                       year=media.year or None, mood=split_values(media.mood or []) or None, bpm=media.bpm_precise or None, **key_fields(media.initial_key), analysis_source={})
                if legacy.resolve().is_relative_to(self.library):
                    self._register(Library(str(self.beets_path), directory=str(self.library)), legacy)
                    sidecar = legacy.with_name(legacy.name + '.provenance.json')
                    if sidecar.exists():
                        persisted = json.loads(sidecar.read_text())
                        existing_record['analysis_source'] = dict(persisted.get('analysis_source', {}))
                        if persisted.get('job_id') == str(job_id) + ':approved:' + record['original_sha256']:
                            analysis = {field: engine for field, engine in record.get('analysis_source', {}).items()
                                        if field not in changes or changes[field] == record.get(field)}
                            persisted.update(source_path=record['source_path'], original_sha256=record['original_sha256'],
                                             analysis_source=analysis)
                            existing_record['analysis_source'] = dict(analysis)
                            _json(sidecar, persisted)
                results.append(existing_record)
                continue
            existing_record = None
            if not changes:
                for sidecar in self.library.rglob('*.provenance.json'):
                    try:
                        existing = json.loads(sidecar.read_text())
                        if existing.get('original_sha256') == record['original_sha256'] and Path(existing['path']).is_file():
                            existing_record = dict(existing, duplicate=True)
                            break
                    except (ValueError, KeyError, OSError):
                        continue
            if existing_record:
                results.append(existing_record)
                continue
            published = self.process([path], record.get('candidate', {}), str(job_id) + ':approved:' + record['original_sha256'], progress, cancelled, _preserve_tags=True)[0]
            published['source_path'] = record['source_path']
            published['original_sha256'] = record['original_sha256']
            published['analysis_source'] = dict(record.get('analysis_source', {}))
            for field in changes:
                if changes[field] != record.get(field):
                    published['analysis_source'].pop(field, None)
            _json(Path(published['path']).with_name(Path(published['path']).name + '.provenance.json'), published)
            results.append(published)
        return results

    def _find_existing_audio(self, digest, cancelled, progress=lambda *args: None):
        # Workers share one index file; serialize so their cache updates are not lost.
        with _audio_index_lock:
            return self._scan_existing_audio(digest, cancelled, progress)

    def _scan_existing_audio(self, digest, cancelled, progress):
        progress('Checking existing library for duplicates')
        checked = 0
        reported = time.monotonic()
        # Cache by path, size and modification time; never change a legacy file.
        index_path = self.state / 'audio-index.json'
        try:
            index = json.loads(index_path.read_text())
        except (OSError, ValueError):
            index = {}
        supported = {'.mp3', '.flac', '.m4a', '.ogg', '.opus', '.wav', '.aac', '.aiff', '.aif'}
        match = None
        for root in dict.fromkeys([self.library, *[Path(p) for p in self.config.get('legacy_roots', [])]]):
            for path in root.rglob('*'):
                if path.suffix.lower() not in supported or not path.is_file() or path.is_symlink():
                    continue
                if cancelled():
                    raise IngestionCancelled('Ingestion cancelled; source downloads retained')
                checked += 1
                if time.monotonic() - reported >= 5:
                    progress(f'Checking existing library for duplicates: {checked} files checked')
                    reported = time.monotonic()
                stat = path.stat()
                stamp = [stat.st_size, stat.st_mtime_ns]
                stored = index.get(str(path))
                if not stored or stored['stamp'] != stamp:
                    try:
                        stored = {'stamp': stamp, 'digest': _audio_hash(path, cancelled)}
                        index[str(path)] = stored
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                        continue
                if stored['digest'] == digest:
                    match = path
                    break
            if match:
                break
        _json(index_path, index)
        return match

    def _possible_duplicates(self, record):
        index_path = self.state / 'audio-index.json'
        try:
            index = json.loads(index_path.read_text())
        except (OSError, ValueError):
            return []
        matches = []
        def normalized(value):
            return re.sub(r'[^\w]', '', str(value or '').casefold())
        artist, title = normalized(record.get('artist')), normalized(record.get('title'))
        if not artist or not title:
            return matches
        for filename, entry in index.items():
            if not Path(filename).is_file():
                continue
            if 'metadata' not in entry or 'album' not in entry['metadata']:
                try:
                    media = MediaFile(filename)
                    entry['metadata'] = dict(artist=media.artist, title=media.title, album=media.album, format=media.format, duration=media.length, bitrate=round(media.bitrate / 1000), path=filename)
                except Exception:
                    continue
            metadata = entry['metadata']
            if normalized(metadata.get('artist')) == artist and normalized(metadata.get('title')) == title:
                matches.append(metadata)
                if len(matches) >= 5:
                    break
        _json(index_path, index)
        return matches

    @staticmethod
    def _register(database, path):
        encoded = os.fsencode(path)
        if not any(item.path == encoded for item in database.items()):
            item = Item.from_path(str(path))
            item['bpm_precise'] = MediaFile(str(path)).bpm_precise
            database.add(item)
            item.store()
