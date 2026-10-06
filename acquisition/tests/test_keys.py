import pytest
from app.keys import camelot, key_fields, sort_key


@pytest.mark.parametrize('value,expected', [
    ('Am', '8A'), ('A minor', '8A'), ('C', '8B'), ('C major', '8B'), ('Ebm', '2A'), ('E♭', '5B'),
    ('D#m', '2A'), ('C#m', '12A'), ('Dbm', '12A'), ('Abm', '1A'), ('B', '1B'), ('F#', '2B'), ('Gb', '2B'),
    ('Cm', '5A'), ('Dm', '7A'), ('8a', '8A'), ('08A', '8A'), ('12B', '12B'), ('1m', '8A'), ('1d', '8B'), ('6m', '1A'),
])
def test_notations_convert_to_camelot(value, expected):
    assert camelot(value) == expected


@pytest.mark.parametrize('value', ['EBM', 'sn3h1t87', 'o', '', None, '13A', 'ebm', 'House'])
def test_non_keys_are_rejected(value):
    assert camelot(value) is None


def test_key_fields_keep_original_text_only_when_it_differs():
    assert key_fields('8A') == {'key': '8A', 'key_tag': None}
    assert key_fields('Am') == {'key': '8A', 'key_tag': 'Am'}
    assert key_fields('EBM') == {'key': None, 'key_tag': 'EBM'}
    assert key_fields('') == {'key': None, 'key_tag': None}


def test_keys_sort_around_the_wheel():
    assert sorted(['10A', '2B', '1B', '2A', '1A'], key=sort_key) == ['1A', '1B', '2A', '2B', '10A']


def test_genre_spellings_are_unified():
    from app.tags import genre_name, genres
    assert [genre_name(g) for g in ['Hip-Hop', 'hip hop', 'Experimental Hip-Hop', 'rnb', 'lo-fi', 'UK Garage', 'Rap', 'Hip - Hop']] == [
        'Hip Hop', 'Hip Hop', 'Experimental Hip Hop', 'R&B', 'Lo-Fi', 'UK Garage', 'Hip Hop', 'Hip Hop']
    assert genres('Hip-Hop; Rap, hip hop') == ['Hip Hop']


def test_combined_genre_tags_are_split_and_junk_dropped():
    from app.tags import genres
    assert genres(['Hip Hop;Boom Bap;West Coast Hip Hop']) == ['Hip Hop', 'Boom Bap', 'West Coast Hip Hop']
    assert genres(['Rap/Hip Hop / French Rap']) == ['Hip Hop', 'French Rap']
    assert genres(['Hip Hop - Rap Français']) == ['Hip Hop', 'French Rap']
    assert genres(['Pop / Pop Internationale / Variété Internationale / R&B']) == ['Pop', 'R&B']
    assert genres(['Funk / Soul', 'Synthpop', 'Jazz-Funk']) == ['Funk', 'Soul', 'Synth-Pop', 'Jazz Funk']
    assert genres(['Music', 'Other', '🔫', '2018/02', 'Dancedj.Club', '15.) Electronic W/O Vocals', 'Spain', 'Top 100']) == []
    assert genres(['Electro', 'Milf House', 'Lo-Fi']) == ['Electro', 'Milf House', 'Lo-Fi']
