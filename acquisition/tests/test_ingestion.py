from pathlib import Path
import hashlib
import subprocess

import pytest
from mediafile import MediaFile
from app.ingestion import Ingestor, IngestionCancelled, _audio_hash


def audio(tmp_path, name='track.mp3', title='Song (Club Remix)', bpm=123, key='Am'):
    path = tmp_path / name
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-codec:a', 'libmp3lame', '-q:a', '2', str(path)], check=True)
    tags = MediaFile(str(path))
    tags.title, tags.artist, tags.album = title, 'Artist', 'Album'
    tags.bpm, tags.initial_key = bpm, key
    tags.save()
    return path


def ingestor(tmp_path):
    return Ingestor({'state_root': str(tmp_path / 'state'), 'library_root': str(tmp_path / 'library'), 'catalog_matching': False})


def test_publish_preserves_sources_tags_and_retry(tmp_path):
    source = audio(tmp_path)
    original = source.read_bytes()
    pipeline = ingestor(tmp_path)
    result = pipeline.process([source], {'title': 'Song'}, 'job')[0]
    published = Path(result['path'])
    assert published.is_file()
    assert source.read_bytes() == original
    assert _audio_hash(source) == _audio_hash(published)
    assert result['original_sha256'] == hashlib.sha256(original).hexdigest()
    assert result['title'] == 'Song (Club Remix)'
    assert result['bpm'] == 123 and result['key'] == '8A'
    assert result['analysis_source'] == {}
    assert result['format'] == 'mp3' and result['duration'] > 2
    assert published.with_name(published.name + '.provenance.json').is_file()
    assert pipeline.process([source], {}, 'job')[0]['duplicate']
    assert pipeline.process([source], {}, 'different-job')[0]['duplicate']
    assert len(list((tmp_path / 'library').rglob('*.mp3'))) == 1


def test_missing_analysis_tags_are_written(tmp_path):
    source = audio(tmp_path, bpm=None, key=None)
    result = ingestor(tmp_path).process([source], {}, 'job')[0]
    assert result['key']
    assert result['analysis_source']['key'] in {'chroma-profile-correlation-v1', 'essentia-edma-key-estimate'}
    assert MediaFile(result['path']).initial_key == result['key']


def test_remix_collision_keeps_both_files_and_safe_paths(tmp_path):
    source = audio(tmp_path, title='../Song')
    other = audio(tmp_path, name='other.mp3', title='../Song', bpm=124)
    pipeline = ingestor(tmp_path)
    a = pipeline.process([source], {}, 'a')[0]
    b = pipeline.process([other], {}, 'b')[0]
    assert a['path'] != b['path']
    assert Path(a['path']).is_relative_to(tmp_path / 'library')
    assert Path(b['path']).is_file()


def test_invalid_source_and_cancel_do_not_publish(tmp_path):
    source = tmp_path / 'bad.mp3'
    source.write_text('not audio')
    pipeline = ingestor(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.process([source], {}, 'bad')
    with pytest.raises(IngestionCancelled):
        pipeline.process([source], {}, 'cancel', cancelled=lambda: True)
    assert not list((tmp_path / 'library').rglob('*.mp3'))


def test_prepare_keeps_library_private_until_publish(tmp_path):
    source = audio(tmp_path)
    pipeline = ingestor(tmp_path)
    prepared = pipeline.prepare([source], {}, 'review')
    assert not list((tmp_path / 'library').rglob('*.mp3'))
    assert prepared[0]['existing_tags']['title'] == 'Song (Club Remix)'
    assert Path(prepared[0]['prepared_path']).is_file()
    published = pipeline.publish(prepared, {prepared[0]['prepared_path']: {'title': 'Explicit edit'}}, 'review')
    assert published[0]['title'] == 'Explicit edit'
    assert Path(published[0]['path']).is_file()
    assert MediaFile(str(source)).title == 'Song (Club Remix)'
    assert published[0]['original_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_wrong_extension_uses_actual_audio_container(tmp_path):
    source = audio(tmp_path).rename(tmp_path / 'download.bin')
    prepared = ingestor(tmp_path).prepare([source], {}, 'wrong-extension')
    assert Path(prepared[0]['path']).suffix == '.mp3'
    assert prepared[0]['format'] == 'mp3'


def test_legacy_duplicate_ignores_tags_without_changing_existing(tmp_path):
    source = audio(tmp_path)
    legacy = tmp_path / 'library' / 'existing.mp3'
    legacy.parent.mkdir()
    legacy.write_bytes(source.read_bytes())
    metadata = MediaFile(str(legacy))
    metadata.title = 'Protected legacy title'
    metadata.save()
    before = legacy.read_bytes()
    pipeline = ingestor(tmp_path)
    prepared = pipeline.prepare([source], {}, 'legacy')
    assert prepared[0]['duplicate']
    assert prepared[0]['duplicate_path'] == str(legacy)
    result = pipeline.publish(prepared, {}, 'legacy')[0]
    assert result['duplicate']
    assert legacy.read_bytes() == before
    assert len(list((tmp_path / 'library').rglob('*.mp3'))) == 1


def test_publish_recovers_crash_after_audio_rename(tmp_path, monkeypatch):
    source = audio(tmp_path)
    pipeline = ingestor(tmp_path)
    prepared = pipeline.prepare([source], {}, 'crash')
    register = pipeline._register
    monkeypatch.setattr(pipeline, '_register', lambda *_: (_ for _ in ()).throw(RuntimeError('simulated crash')))
    with pytest.raises(RuntimeError, match='simulated crash'):
        pipeline.publish(prepared, {}, 'crash')
    monkeypatch.setattr(pipeline, '_register', register)
    published = pipeline.publish(prepared, {}, 'crash')[0]
    assert Path(published['path']).is_file()
    assert len(list((tmp_path / 'library').rglob('*.mp3'))) == 1


def test_catalog_proposals_preserve_remix_and_warn_on_request_conflict(tmp_path, monkeypatch):
    source = audio(tmp_path)
    pipeline = Ingestor({'state_root': str(tmp_path / 'state'), 'library_root': str(tmp_path / 'library'), 'catalog_matching': True})
    monkeypatch.setattr('app.ingestion._catalog', lambda *_: ([{'title': 'Song', 'artist': 'Other artist', 'recommendation': 'strong', 'distance': 0.01}], None))
    prepared = pipeline.prepare([source], {'artist': 'Other artist', 'title': 'Song'}, 'catalog')[0]
    assert prepared['title'] == 'Song (Club Remix)'
    assert prepared['proposed_tags']['title'] == 'Song (Club Remix)'
    assert prepared['catalog_matches'][0]['title'] == 'Song'
    assert any('Requested title differs' in warning for warning in prepared['warnings'])
    assert MediaFile(str(source)).title == 'Song (Club Remix)'


def test_explicit_keep_existing_does_not_refill_cleared_tags(tmp_path):
    source = audio(tmp_path)
    pipeline = ingestor(tmp_path)
    prepared = pipeline.prepare([source], {}, 'clear')
    result = pipeline.publish(prepared, {prepared[0]['prepared_path']: {'bpm': 0, 'key': ''}}, 'clear')[0]
    assert result['bpm'] is None and result['key'] is None
    assert not MediaFile(result['path']).bpm
    assert not MediaFile(result['path']).initial_key


def test_possible_duplicate_quality_stays_in_review(tmp_path):
    source = audio(tmp_path)
    legacy = tmp_path / 'library' / 'different-quality.mp3'
    legacy.parent.mkdir()
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(source), '-codec:a', 'libmp3lame', '-b:a', '64k', str(legacy)], check=True)
    tags = MediaFile(str(legacy))
    tags.title, tags.artist = 'Song (Club Remix)', 'Artist'
    tags.save()
    pipeline = ingestor(tmp_path)
    prepared = pipeline.prepare([source], {}, 'possible')[0]
    assert not prepared['duplicate']
    assert prepared['possible_duplicates']
    assert any('not automatically merged' in warning for warning in prepared['warnings'])
    assert len(list((tmp_path / 'library').rglob('*.mp3'))) == 1


@pytest.mark.parametrize('extension,codec', [('flac', 'flac'), ('m4a', 'aac'), ('opus', 'libopus'), ('wav', 'pcm_s16le')])
def test_actual_download_formats_prepare_without_transcoding(tmp_path, extension, codec):
    source = tmp_path / ('track.' + extension)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-codec:a', codec, str(source)], check=True)
    prepared = ingestor(tmp_path).prepare([source], {'artist': 'Artist'}, extension)[0]
    assert _audio_hash(source) == _audio_hash(Path(prepared['path']))
    assert prepared['duration'] > 1
    assert not list((tmp_path / 'library').rglob('*.*'))


def test_duplicate_does_not_claim_unpublished_analysis(tmp_path):
    source = audio(tmp_path, bpm=None, key=None)
    legacy = tmp_path / 'library' / 'existing.mp3'
    legacy.parent.mkdir()
    legacy.write_bytes(source.read_bytes())
    pipeline = ingestor(tmp_path)
    messages = []
    prepared = pipeline.prepare([source], {}, 'analysis-duplicate', progress=messages.append)
    assert prepared[0]['analysis_source']
    result = pipeline.publish(prepared, {}, 'analysis-duplicate')[0]
    assert result['duplicate'] and result['analysis_source'] == {}
    assert result['key'] is None
    assert 'Checking existing library for duplicates' in messages


@pytest.mark.parametrize('extension,codec', [('mp3', 'libmp3lame'), ('flac', 'flac'), ('m4a', 'aac'), ('opus', 'libopus'), ('wav', 'pcm_s16le')])
def test_fractional_bpm_preserved_and_explicit_edits_precise(tmp_path, extension, codec):
    source = tmp_path / ('fractional.' + extension)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-codec:a', codec, str(source)], check=True)
    tags = MediaFile(str(source))
    tags.bpm_precise, tags.initial_key, tags.title, tags.artist = 65.95, 'Am', 'Song (Remix)', 'Artist'
    tags.save()
    source_bytes = source.read_bytes()
    pipeline = ingestor(tmp_path)
    prepared = pipeline.prepare([source], {}, 'fractional')
    assert prepared[0]['existing_tags']['bpm'] == pytest.approx(65.95)
    assert prepared[0]['proposed_tags']['bpm'] == pytest.approx(65.95)
    assert MediaFile(prepared[0]['path']).bpm_precise == pytest.approx(65.95)
    result = pipeline.publish(prepared, {prepared[0]['path']: {'bpm': 123.45}}, 'fractional')[0]
    assert result['bpm'] == pytest.approx(123.45)
    assert MediaFile(result['path']).bpm_precise == pytest.approx(123.45)
    assert source.read_bytes() == source_bytes
    assert _audio_hash(source) == _audio_hash(Path(result['path']))


def test_fractional_bpm_raw_id3_text_retained_without_edits(tmp_path):
    from mutagen.id3 import ID3, TBPM
    source = audio(tmp_path)
    tags = ID3(source)
    tags.add(TBPM(encoding=3, text=['117.79']))
    tags.save(source)
    pipeline = ingestor(tmp_path)
    prepared = pipeline.prepare([source], {}, 'raw-fraction')
    published = pipeline.publish(prepared, {}, 'raw-fraction')[0]
    assert published['bpm'] == pytest.approx(117.79)
    assert str(ID3(published['path'])['TBPM']) == '117.79'


def test_youtube_single_keeps_title_and_gets_album_fallback(tmp_path):
    source = audio(tmp_path, title='Artist - Live at the Studio')
    tags = MediaFile(str(source))
    tags.album = None
    tags.save()
    prepared = ingestor(tmp_path).prepare([source], {'source': 'youtube'}, 'yt')[0]
    assert prepared['title'] == 'Artist - Live at the Studio'
    assert prepared['album'] == 'Artist - Live at the Studio'
    assert 'No embedded cover art. Navidrome will show no cover.' in prepared['warnings']
    assert not any('Unknown Album' in w for w in prepared['warnings'])


def test_soulseek_tags_are_not_rewritten(tmp_path):
    source = audio(tmp_path, title='Artist - Kept')
    tags = MediaFile(str(source))
    tags.album = None
    tags.save()
    prepared = ingestor(tmp_path).prepare([source], {'source': 'soulseek'}, 'slsk')[0]
    assert prepared['title'] == 'Artist - Kept' and not prepared['album']
    assert 'No album tag. Navidrome will list this under Unknown Album.' in prepared['warnings']


def test_existing_keys_become_camelot_and_non_keys_are_estimated(tmp_path):
    valid = audio(tmp_path, key='Ebm')
    junk = audio(tmp_path, name='junk.mp3', title='Other', key='EBM')
    pipeline = ingestor(tmp_path)
    normalized = pipeline.prepare([valid], {}, 'valid')[0]
    assert normalized['key'] == '2A' and 'key' not in normalized['analysis_source']
    assert MediaFile(normalized['path']).initial_key == '2A'
    estimated = pipeline.prepare([junk], {}, 'junk')[0]
    assert estimated['existing_tags']['key'] == 'EBM'
    assert estimated['key'] != 'EBM' and estimated['analysis_source'].get('key')
    assert any('"EBM" is not a musical key' in warning for warning in estimated['warnings'])
    assert MediaFile(str(junk)).initial_key == 'EBM'


def test_long_recordings_are_not_analyzed(tmp_path, monkeypatch):
    monkeypatch.setattr('app.ingestion.BPM_KEY_MAX_SECONDS', 1)
    monkeypatch.setattr('app.ingestion._analyze', lambda *_: pytest.fail('long recordings must not be analyzed'))
    source = audio(tmp_path, bpm=None, key=None)
    prepared = ingestor(tmp_path).prepare([source], {}, 'album-in-one-file')[0]
    assert prepared['bpm'] is None and prepared['key'] is None
    assert prepared['analysis_skipped'] == 'long-recording'
    assert any('Longer than 10 minutes' in warning for warning in prepared['warnings'])


def test_json_writes_from_parallel_workers_do_not_collide(tmp_path):
    import json
    import threading
    from app.ingestion import _json
    target = tmp_path / 'audio-index.json'
    errors = []

    def write(n):
        try:
            for i in range(50):
                _json(target, {'worker': n, 'i': i})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert json.loads(target.read_text())['i'] == 49
    assert [p.name for p in tmp_path.iterdir()] == ['audio-index.json']


def test_prepare_skips_undecodable_file_and_keeps_the_rest(tmp_path):
    first = audio(tmp_path, name='first.mp3', title='First')
    last = audio(tmp_path, name='last.mp3', title='Last')
    bad = tmp_path / 'broken.mp3'
    bad.write_text('not audio')
    skipped = []
    # A bad file after a good one must still be skipped, not fail the job.
    prepared = ingestor(tmp_path).prepare([first, bad, last], {}, 'mixed', skipped=skipped)
    assert [Path(r['source_path']).name for r in prepared] == ['first.mp3', 'last.mp3']
    assert [entry['name'] for entry in skipped] == ['broken.mp3']
    assert skipped[0]['reason']
    assert not list((tmp_path / 'library').rglob('*.mp3'))


def test_skipping_never_swallows_cancellation(tmp_path):
    bad = tmp_path / 'broken.mp3'
    bad.write_text('not audio')
    with pytest.raises(IngestionCancelled):
        ingestor(tmp_path).prepare([bad], {}, 'cancel', cancelled=lambda: True, skipped=[])


def test_killed_decoder_is_not_skipped_as_bad_file(tmp_path, monkeypatch):
    import app.ingestion as ingestion
    source = audio(tmp_path)
    real_run = ingestion._run

    def run(args, *rest, **kwargs):
        if '-xerror' in args:
            raise subprocess.CalledProcessError(-15, args, b'', b'')
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(ingestion, '_run', run)
    skipped = []
    with pytest.raises(subprocess.CalledProcessError):
        ingestor(tmp_path).prepare([source], {}, 'killed', skipped=skipped)
    assert skipped == []


def test_library_analysis_skips_bpm_and_key_over_limit(tmp_path, monkeypatch):
    import app.library_tags as library_tags
    monkeypatch.setattr(library_tags, 'BPM_KEY_MAX_SECONDS', 1)
    monkeypatch.setattr(library_tags, '_analyze', lambda *_: pytest.fail('BPM/key must not be estimated'))
    source = audio(tmp_path, bpm=None, key=None)
    changes, sources, note = library_tags.analyze_file(source)
    assert note == 'long-recording'
    assert 'bpm' not in changes and 'key' not in changes


def test_strong_catalog_match_names_the_track_only_at_the_same_length(tmp_path, monkeypatch):
    for length, title in ((3.5, 'Song'), (240.0, 'Song (Club Remix)')):
        source = audio(tmp_path, name=f'{length}.mp3')
        pipeline = Ingestor({'state_root': str(tmp_path / f'state{length}'), 'library_root': str(tmp_path / f'library{length}'), 'catalog_matching': True})
        monkeypatch.setattr('app.ingestion._catalog', lambda *_: ([{'title': 'Song', 'artist': 'Artist', 'recommendation': 'strong', 'length': length}], None))
        prepared = pipeline.prepare([source], {}, f'named{length}')[0]
        assert prepared['title'] == title and prepared['proposed_tags']['title'] == title
        assert MediaFile(prepared['path']).title == title
        assert MediaFile(str(source)).title == 'Song (Club Remix)'
        if title == 'Song':
            assert prepared['catalog_applied'] == {'title': 'Song (Club Remix)'}
            assert prepared['analysis_source']['title'] == 'musicbrainz-match'


def test_retire_and_restore_move_a_library_file_and_its_provenance(tmp_path):
    pipeline = ingestor(tmp_path)
    published = Path(pipeline.process([audio(tmp_path)], {}, 'job')[0]['path'])
    sidecar = published.with_name(published.name + '.provenance.json')
    entry = pipeline.retire(published, tmp_path / 'trash')
    assert not published.exists() and not sidecar.exists() and not published.parent.exists()
    assert Path(entry['trash']).is_file()
    assert pipeline.restore(entry) == str(published)
    assert published.is_file() and sidecar.is_file()
    try:
        pipeline.retire(tmp_path / 'outside.mp3', tmp_path / 'trash')
        raise AssertionError('retire must refuse files outside the library')
    except ValueError:
        pass


def test_album_without_artist_tags_takes_the_requested_artist(tmp_path):
    paths = []
    for n in range(2):
        path = audio(tmp_path, name=f'{n}.mp3', title=f'Song {n}')
        tags = MediaFile(str(path))
        tags.artist, tags.album = None, 'Even In Arcadia'
        tags.save()
        paths.append(path)
    request = {'source': 'soulseek', 'requested_artist': 'Sleep Token', 'requested_album': 'Even In Arcadia'}
    prepared = ingestor(tmp_path).prepare(paths, request, 'arcadia')
    assert [p['artist'] for p in prepared] == ['Sleep Token', 'Sleep Token']
    assert any('from your request' in w for w in prepared[0]['warnings'])
    other = []
    for n in range(2):   # another album's files keep their blank artist: no guessing
        path = audio(tmp_path, name=f'o{n}.mp3', title=f'Other {n}')
        tags = MediaFile(str(path))
        tags.artist, tags.album = None, 'Something Else'
        tags.save()
        other.append(path)
    assert not any(p['artist'] for p in ingestor(tmp_path).prepare(other, request, 'other'))
