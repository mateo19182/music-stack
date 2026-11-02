import re
from html.parser import HTMLParser
from urllib.parse import unquote

from concert_alerts.notify import batches


def event(id, artist='Yawners', title='Yawners', **changes):
    return {'id': id, 'name': title, 'artists': {artist: artist},
            'date': '2027-01-28', 'time': '21:00:00', 'date_unknown': False,
            'time_unknown': False, 'timezone': 'Europe/Madrid', 'status': 'onsale',
            'venue': 'Sala Upload', 'city': 'Barcelona', 'highlight': 'Barcelona',
            'source': 'Ticketmaster', 'url': 'https://example.com/tickets', **changes}


def entries(events):
    return [(item, False, item['id']) for item in events]


def test_artist_group_moves_whole_to_next_message():
    filler = event('filler', artist='Other act ' * 30, title='Other act ' * 30, venue='Long venue ' * 30)
    shows = [event('barcelona'), event('bilbao', city='Bilbao', highlight='', date='2027-01-29')]
    messages = list(batches(entries([filler, *shows]), limit=900))
    assert len(messages) == 2
    assert [item[0]['id'] for item in messages[0][0]] == ['filler']
    assert {item[0]['id'] for item in messages[1][0]} == {'barcelona', 'bilbao'}
    assert 'Yawners' not in messages[0][1]
    assert messages[1][1].count('Yawners') == 1
    for i, (_, text) in enumerate(messages, 1):
        assert text.startswith(f'Concerts · {i}/2')
        assert len(text.encode('utf-16-le')) // 2 <= 900


def test_solo_artist_groups_across_titles_with_per_date_context():
    shows = [event('one', artist='Monty', title='BAKALAO'),
             event('two', artist='Monty', title='BAKALAO ESPECIAL VINILO', date='2027-02-28')]
    text = list(batches(entries(shows)))[0][1]
    assert text.count('<b>Monty · 2 dates</b>') == 1
    assert 'BAKALAO' in text and 'BAKALAO ESPECIAL VINILO' in text
    assert text.count('Event / tickets</a>') == 2


def test_multi_artist_listings_are_not_repeated_in_each_artist_group():
    bill = event('festival', artist='A', title='Festival', artists={'A': 'A', 'B': 'B'})
    shows = [bill, event('solo-a', artist='A', title='A'), event('solo-b', artist='B', title='B')]
    messages = list(batches(entries(shows)))
    assert [item[0]['id'] for batch, _ in messages for item in batch] == ['festival', 'solo-a', 'solo-b']
    assert sum(text.count('A, B — Festival') for _, text in messages) == 1


def test_hidden_count_is_combined_across_an_artists_event_titles():
    shows = [event(str(i), title=f'Night {i}', highlight='Barcelona' if i < 2 else '') for i in range(5)]
    text = list(batches(entries(shows)))[0][1]
    assert 'Yawners · 2 dates · 3 elsewhere hidden' in text
    assert 'Night 0' in text and 'Night 1' in text and 'Night 2' not in text


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.links = [], []

    def handle_starttag(self, tag, attrs):
        assert tag in {'b', 'a'}
        self.stack.append(tag)
        if tag == 'a':
            assert len(attrs) == 1 and attrs[0][0] == 'href'
            self.links.append(attrs[0][1])

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


def test_compact_links_and_metadata_are_safe_html():
    url = 'https://example.com/?file=MEDIA:/etc/passwd&artist="A"'
    show = event('one', artist='Artist <b> & "name"', title='MEDIA:/etc/passwd <a href="bad">',
                 venue='<script>alert(1)</script>', url=url)
    text = list(batches(entries([show])))[0][1]
    parser = Markup()
    parser.feed(text)
    assert parser.stack == [] and len(parser.links) == 1
    assert unquote(parser.links[0]) == url
    assert 'MEDIA:' not in text and '<script>' not in text
    assert 'Event / tickets</a>' in text and '\nhttps://' not in text


def test_long_escaped_metadata_keeps_balanced_tags_and_message_numbers():
    shows = [event(str(i), artist='Artist ' + str(i) + ' <&"' * 100, title='<&"' * 800,
                   venue='<&"' * 800) for i in range(6)]
    messages = list(batches(entries(shows), limit=1800))
    assert len(messages) > 1
    for i, (_, text) in enumerate(messages, 1):
        assert text.startswith(f'Concerts · {i}/{len(messages)}')
        assert len(text.encode('utf-16-le')) // 2 <= 1800
        parser = Markup()
        parser.feed(text)
        assert not parser.stack
        assert '&' not in re.sub(r'&(?:amp|lt|gt|quot|#x27);', '', text)
