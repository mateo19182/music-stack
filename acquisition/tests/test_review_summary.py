from app.review_summary import summarize


def track(n, **kw):
    return dict(id=str(n), artist='Artist', title=f'Track {n}', album='Album', track_number=n, **kw)


def album(files, **candidate):
    return summarize({'candidate': {'kind': 'album', **candidate}}, files)


def test_numbering_is_not_catalog_completeness():
    summary = album([track(1), track(2)])
    assert summary['status'] == 'ready'
    assert 'unverified' in summary['completeness']
    summary = album([track(1, track_total=2), track(2, track_total=2)])
    assert 'matches embedded totals' in summary['completeness']
    assert 'edition is unverified' in summary['completeness']


def test_gaps_and_repeated_numbers():
    summary = album([track(1, track_total=4), track(3, track_total=4), track(3, track_total=4)])
    assert summary['status'] == 'check'
    assert any('missing track numbers 2, 4' in c for c in summary['concerns'])
    assert any('repeated track numbers 3' in c for c in summary['concerns'])


def test_multiple_discs_are_checked_separately():
    summary = album([track(1, disc_number=1, track_total=1), track(1, disc_number=2, track_total=1)])
    assert summary['status'] == 'ready'


def test_source_counts_and_mixed_editions():
    summary = album([track(1), dict(track(2), album='Album (Live)')], file_count=3)
    assert summary['status'] == 'check'
    assert any('3 source files' in c for c in summary['concerns'])
    assert any('Inconsistent album names' in c for c in summary['concerns'])
    assert any('Mixed recording-version' in c for c in summary['concerns'])


def test_ai_cannot_hide_duplicate_or_album_gaps():
    job = {'candidate': {'kind': 'album'}, 'advice': {'status': 'complete', 'result': {
        'status': 'looks_fine', 'files': [{'id': '1', 'innocuous_warnings': ['Minor note']}]}}}
    summary = summarize(job, [track(1, duplicate=True, warnings=['Minor note']), track(3)])
    assert summary['status'] == 'check'
    assert any('identical' in c for c in summary['concerns'])
    assert any('missing track numbers 2' in c for c in summary['concerns'])
    assert not any('metadata concern' in c for c in summary['concerns'])


def test_track_review_missing_identity_and_possible_duplicate():
    summary = summarize({}, [{'id': 'x', 'title': '', 'artist': '', 'possible_duplicates': [{'id': 'existing'}]}])
    assert summary['status'] == 'check'
    assert any('Missing artist' in c for c in summary['concerns'])
    assert any('another library version' in c for c in summary['concerns'])


def test_missing_disc_and_unknown_metadata_are_not_complete():
    summary = album([track(1, disc_number=1, disc_total=2, track_total=1)])
    assert summary['status'] == 'check'
    assert any('Missing disc numbers 2' in c for c in summary['concerns'])
    assert 'unverified' in summary['completeness']
    assert summarize({}, [])['status'] == 'check'


def test_single_concert_requires_explicit_attention():
    summary = summarize({}, [track(1, duration=5937)])
    assert summary['status'] == 'check'
    assert any('single long track' in c for c in summary['concerns'])


def test_album_request_with_one_middle_track_is_partial():
    summary = album([track(3, track_total=10)])
    assert summary['status'] == 'check'
    assert any('missing track numbers' in c for c in summary['concerns'])


def test_mixed_import_is_not_an_album():
    files = [dict(id='1', artist='Jul', title='A', album='La machine'),
             dict(id='2', artist='Young Dolph', title='B', album='')]
    summary = summarize({'source': 'existing', 'candidate': {'source': 'existing'}}, files)
    assert summary['mixed'] and not summary['album']
    assert summary['status'] == 'ready' and summary['completeness'] is None
    same = [dict(id=str(n), artist='Jul', title=f'T{n}', album='La machine', track_number=n) for n in (1, 2)]
    summary = summarize({'source': 'existing', 'candidate': {'source': 'existing'}}, same)
    assert summary['album'] and not summary['mixed']
