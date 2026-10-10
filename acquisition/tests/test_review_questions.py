from app.review_questions import plan


def track(**kw):
    return {'artist': 'Vulfpeck', 'title': 'Sauna', 'duration': 192, 'format': 'flac', 'bitrate': 900_000,
            'candidate': {'requested_artist': 'Vulfpeck', 'requested_title': 'Sauna'}, **kw}


def library(**kw):
    return {'title': 'Sauna', 'duration': 191, 'format': 'MP3', 'bitrate': 320, 'replaceable': True, **kw}


def test_clean_track_is_added_and_identical_copy_skipped():
    assert plan(track()) == {'action': 'add', 'questions': []}
    assert plan(track(duplicate_path='/library/x.flac'))['action'] == 'skip'


def test_requests_that_name_another_song_are_asked_but_remasters_are_not():
    assert plan(track(title='Sauna (2025 Remaster)'))['action'] == 'add'
    asked = plan(track(title='Zzz'))
    assert asked['action'] == 'ask' and asked['questions'] == [
        {'kind': 'mismatch', 'field': 'title', 'requested': 'Sauna', 'found': 'Zzz'}]
    # In a multi-track download the request names the release, not each track.
    assert plan(track(title='Zzz'), track_count=12)['action'] == 'add'


def test_missing_names_and_long_single_files_are_asked():
    assert plan(track(artist='', proposed_tags={'artist': ''}))['questions'][0] == {'kind': 'missing', 'fields': ['artist']}
    assert plan(track(duration=45 * 60))['questions'][0]['kind'] == 'long'


def test_versions_replace_when_clearly_better_and_skip_when_not():
    assert plan(track(possible_duplicates=[library()]))['action'] == 'replace'
    assert plan(track(format='mp3', bitrate=320_000, possible_duplicates=[library()]))['action'] == 'skip'
    assert plan(track(format='mp3', bitrate=320_000, possible_duplicates=[library(bitrate=128)]))['action'] == 'replace'
    assert plan(track(possible_duplicates=[library(format='FLAC', bitrate=1000)]))['action'] == 'skip'


def test_unclear_versions_are_asked():
    # A different length is probably another recording, such as a remix or live take.
    assert plan(track(possible_duplicates=[library(duration=240)]))['questions'][0]['kind'] == 'version'
    # Files outside the managed library are never replaced.
    assert plan(track(possible_duplicates=[library(replaceable=False)]))['action'] == 'ask'



def test_album_decides_between_another_copy_and_another_recording():
    on_album = library(album='Hill Climber', duration=180)
    # The same album track a few seconds longer, e.g. from a video with an intro.
    decided = plan(track(album='Hill Climber (Remastered)', possible_duplicates=[on_album]))
    assert decided['action'] == 'replace' and decided['replace'] == [on_album]
    # A same-titled track on another album with another length is added alongside.
    other = library(album='Live at Madison Square Garden', duration=260)
    assert plan(track(album='Hill Climber', possible_duplicates=[other])) == {'action': 'add', 'questions': []}
    # Only the copy on this album is replaced.
    assert plan(track(album='Hill Climber', possible_duplicates=[on_album, other]))['replace'] == [on_album]
    # Too far apart even on the same album, or no album to compare, is still asked.
    assert plan(track(album='Hill Climber', possible_duplicates=[library(album='Hill Climber', duration=240)]))['action'] == 'ask'
    assert plan(track(possible_duplicates=[library(album='Hill Climber', duration=260)]))['action'] == 'ask'
