"""Does a search result hold the album we want, and how good a copy is it?

Moved from the blog-favourites batch (blog_acquire.py), where these rules were tuned on
~700 real searches: a folder must carry the album's words (typos allowed) and nothing that
makes it another record (a volume number, "remixes", a chapter); quality decides between copies.
"""
import collections
import difflib
import re
import sqlite3
import unicodedata

ROMAN = {'ii': '2', 'iii': '3', 'iv': '4', 'vi': '6', 'vii': '7', 'viii': '8', 'ix': '9'}   # not i/v/x: real words
UNWANTED = ('instrumental', 'compilation', 'remix', 'live', 'karaoke', 'acapella', 'a cappella', 'sped up', 'slowed', 'reverb', 'chopped')


def key(text):
    ascii_key = re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower())
    return ascii_key or re.sub(r'\W', '', (text or '').casefold())


def clean_artist(artist):
    # "Overmono (* incredible live set)", "A + B", "A and B" → first credited artist for matching
    artist = re.sub(r'\s*\(.*?\)', '', artist or '')
    return re.split(r'\s*(?:\+|,| and | & | x )\s*', artist)[0].strip()


def album_key(name):
    """Album key with "Part II"/"Part 2" and "Vol. 3"/"Vol 3" spelled the same."""
    roman = {'i': '1', 'ii': '2', 'iii': '3', 'iv': '4', 'v': '5'}
    name = re.sub(r'\b(part|pt|vol(?:ume)?)\.?\s*(i{1,3}|iv|v)\b', lambda m: f'{m[1]} {roman[m[2].lower()]}', name or '', flags=re.I)
    # Standalone "II".."IX" are numbers too ("Pinball II" = "Pinball 2"); not I/V/X, which are words.
    name = re.sub(r'\b(ii|iii|iv|vi|vii|viii|ix)\b', lambda m: ROMAN[m[1].lower()], name, flags=re.I)
    return key(name)


def numbers(name):
    """Non-year numbers in a title: "Basspunk" is not "Basspunk 2", "Part 1" is not "Part II"."""
    return {n.lstrip('0') for n in re.findall(r'\d+', album_key(name)) if not re.fullmatch(r'(19|20)\d\d', n)}


def in_library(navidrome_db):
    """Navidrome's present tracks per album: album_id → keys of title (with and without brackets), artists, track ids."""
    db = sqlite3.connect(f'file:{navidrome_db}?mode=ro', uri=True)
    albums = {}
    for id, album_id, album, artist, album_artist in db.execute(
            'select id, album_id, album, artist, album_artist from media_file '
            'where missing = 0 order by album_id, disc_number, track_number, path'):
        a = albums.setdefault(album_id, {'keys': {album_key(album), album_key(re.sub(r'\s*[\(\[].*?[\)\]]', '', album))} - {''},
                                         'numbers': numbers(album), 'artists': set(), 'tracks': []})
        a['artists'].update({key(artist), key(album_artist)} - {''})
        a['tracks'].append(id)
    return albums


def artist_matches(wanted, credited):
    """First credited artist of the post, typos allowed ("Earl Sweatchirt", "Joy Orbinson")."""
    if not wanted:
        return False
    return any(wanted in x or len(x) >= 3 and x in wanted
               or difflib.SequenceMatcher(None, wanted, x[:len(wanted)]).ratio() >= 0.8 for x in credited)


# A listed edition ("(Director's Cut)") is a different record from the plain album: match and download only it.
EDITION = re.compile(r"[\(\[]\s*(director'?s cut|deluxe|expanded|complete)", re.I)


def album_tracks(library, item):
    """Navidrome track ids for a blog entry, in disc/track order. Title (typos allowed) and artist must both match."""
    artist = key(clean_artist(item['artist']))
    stripped = re.sub(r'\s*[\(\[].*?[\)\]]', '', item['album'])
    names = [item['album']]
    if not re.search(r'remix|live|instrumental', item['album'][len(stripped):], re.I) and not EDITION.search(item['album']):
        names.append(stripped)   # "(Soundtrack)" → the plain album; not for remix albums or a listed edition
    for name in names:
        k = album_key(name)
        if not k:
            continue
        found = [a for a in library.values() if artist_matches(artist, a['artists']) and a['numbers'] == numbers(item['album'])
                 and any(x == k or len(k) >= 5 and difflib.SequenceMatcher(None, x, k).ratio() >= 0.85 for x in a['keys'])]
        if found:
            # Navidrome splits an album whose tracks credit different album artists ("Machinedrum,
            # Kučka" / "Machinedrum, DUCKWRTH") into several albums: together they are the album.
            exact = [a for a in found if k in a['keys']] or found
            return list(dict.fromkeys(t for a in exact for t in a['tracks']))
    return []


def matches(album, text):
    """Album name inside a folder/title, tolerating small typos ("Void Dire", "Patters")."""
    a, t = key(re.sub(r'[\(\[].*?[\)\]]', '', album)) or key(album), key(text)
    if not a or not t:
        return False
    if len(a) < 5:
        # "40" must be a word of its own, not part of "400 Lux".
        spaced = ' ' + re.sub(r'[^a-z0-9]+', ' ', unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()) + ' '
        return f' {a} ' in spaced
    if a in t:
        return True
    best = max(difflib.SequenceMatcher(None, a, t[i:i + len(a)]).ratio() for i in range(max(1, len(t) - len(a) + 1)))
    return best >= 0.86


def library_has(library, item):
    # A single or two from an album is not the album: those get downloaded whole.
    return len(album_tracks(library, item)) >= 4


def superseded(item, everything):
    """An expanded edition listed elsewhere ("X (Director's Cut)") already contains album X."""
    edition = re.compile(r"^\W*(director'?s cut|deluxe|expanded|complete)", re.I)
    for other in everything:
        if other is item or key(clean_artist(other['artist'])) != key(clean_artist(item['artist'])):
            continue
        if other['album'].casefold().startswith(item['album'].casefold()):
            rest = other['album'][len(item['album']):].strip(' ([')
            if rest and edition.match(rest):
                return True
    return False


NOISE = re.compile(r"\b(?:19|20)\d\d\b|\b(?:flac|mp3|aac|m4a|alac|wav|aiff|ogg|opus|web|cd|vinyl|lp|ep|"
                   r"\d+\s*bit|\d+(?:\.\d+)?\s*khz|\d{3}\s*(?:kbps|k)|kbps|lossless|hi\s*res|24bit|16bit|deluxe edition|"
                   r"remaster(?:ed)?|album|ost|pmedia|dash)\b|[\(\[\{][^\)\]\}]*[\)\]\}]|⭐️", re.I)


def release_folder(c):
    """The folder named after the release; a bare "FLAC (24bit-48kHz)" leaf defers to its parent."""
    parts = re.split(r'[\\/]', str(c.get('directory') or c.get('title') or ''))
    for part in reversed(parts[-2:]):
        if key(NOISE.sub(' ', part)):
            return part
    return parts[-1] if parts else ''


FILLER = {'the', 'and', 'x', 'with', 'feat', 'ft', 'vs', 'a', 'de', 'la', 'el', 'y', 'le', 'les'}


def words(text):
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().lower() or str(text or '').casefold()
    return [ROMAN.get(w, w) for w in re.split(r"[^\w]+|_", text.replace("'", '')) if w]   # "Pinball II" = "Pinball 2"


def close(a, b):
    """Same word, allowing a typo in longer words ("void"/"voir", "sweatchirt"/"sweatshirt")."""
    if a == b:
        return True
    return min(len(a), len(b)) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def identity(c, album, artist):
    """Gate, not points. The release folder's words, minus noise and the artist's words, must be
    the album's words plus at most one extra; none when the artist is not on the path."""
    return extra_words(c, album, artist) is not None


def extra_words(c, album, artist):
    """The folder's words beyond the album's, or None when it cannot be the album."""
    name = release_folder(c)
    if EDITION.search(album):
        # Keep "(Director's Cut)" in the folder; NOISE drops other bracketed text such as "[24-48]".
        name = re.sub(r"[\(\[]\s*((?:director'?s cut|deluxe|expanded|complete)[^\)\]]*)[\)\]]", r' \1 ', name, flags=re.I)
    folder = words(NOISE.sub(' ', name))
    path = words(str(c.get('directory') or '') + ' ' + str(c.get('title') or ''))
    artist_words = [w for w in words(clean_artist(artist)) if w not in FILLER]
    has_artist = bool(artist_words) and all(any(close(a, p) for p in path) for a in artist_words)
    rest = [w for w in folder if not any(close(w, a) for a in artist_words)]
    plain = album if EDITION.search(album) else re.sub(r'[\(\[].*?[\)\]]', '', album)
    # The folder lost its noise words ("EP", years), so the album loses them too: "Superwave EP".
    album_words = ([w for w in words(plain if EDITION.search(album) else NOISE.sub(' ', plain)) if w not in FILLER] or
                   [w for w in words(plain) if w not in FILLER] or words(album))
    # Album words that are also artist words ("Skepta..Fred", self-titled albums) match the whole folder.
    found = lambda a: any(close(a, w) for w in (folder if any(close(a, x) for x in artist_words) else rest))
    missing = [a for a in album_words if not found(a)]
    # Long titles may differ in a word per five when the artist is on the path ("your" / "Ur").
    allowed_missing = len(album_words) // 5 if has_artist else 0
    if not album_words or len(missing) > allowed_missing or len(missing) == len(album_words):
        return None
    extra = [w for w in rest if w not in FILLER and not any(close(w, a) for a in album_words)]
    if any(w.isdigit() and not re.fullmatch(r'(19|20)\d\d', w) for w in extra):
        return None   # "Machine II" is another volume of "Machine"
    self_titled = all(any(close(a, x) for x in artist_words) for a in album_words)
    if len(extra) > (1 if has_artist and not self_titled else 0) + len(missing):
        return None   # "A Few Songs B4 40" is not "40"; "... & Nico" is another record
    lowered = release_folder(c).casefold()
    if any(re.search(r'\b' + re.escape(u) + r'(?:e?s)?\b', lowered) and u not in album.casefold() for u in UNWANTED):
        return None
    return extra


def rank(c, album, typical):
    """None when the folder cannot be the release; otherwise higher is better."""
    count = c.get('file_count') or 0
    edition = re.findall(r'[\(\[](.*?)[\)\]]', album)
    if c.get('mixed_formats'):
        return None
    if typical:
        if count < typical - 1 or (count > typical + 1 and not edition):
            return None   # partial, or a padded/deluxe folder we did not ask for
    elif count < 2:
        return None
    fmt = (c.get('format') or '').lower()
    bitrate = c.get('bitrate') or 0
    if fmt in ('flac', 'alac', 'wav', 'aiff'):
        s = 3.0
    elif bitrate >= 256:
        s = 2.0
    elif bitrate >= 192:
        s = 1.0
    else:
        return None    # no 128 kbps copies in the library
    if edition and all(key(e) in key(str(c.get('directory')) + str(c.get('title'))) for e in edition):
        s += 2         # the listed edition (Director's Cut) over the plain one
    if c.get('free_slots'):
        s += 0.5
    return s - min(c.get('queue_length') or 0, 50) / 100


def pick(results, album, artist, avoid=()):
    albums = [c for c in results if c.get('kind') == 'album' and c.get('source') == 'soulseek'
              and c.get('username') not in avoid and identity(c, album, artist)]
    # The usual track count, from folders named exactly like the album when there are any: chapter
    # or single folders ("Mid Spiral - Chaos", 6 files) can outnumber the full album (18).
    exact = [c for c in albums if not extra_words(c, album, artist)] or albums
    counts = collections.Counter(c.get('file_count') for c in exact if c.get('file_count'))
    typical = counts.most_common(1)[0][0] if counts else None
    # An exact folder beats one with an extra word ("EUSEXUA" over "EUSEXUA Afterglow", a different album).
    score = lambda c: None if rank(c, album, typical) is None else rank(c, album, typical) - 3 * len(extra_words(c, album, artist))
    scored = sorted(((score(c), c) for c in albums), key=lambda x: -1e9 if x[0] is None else x[0], reverse=True)
    scored = [x for x in scored if x[0] is not None]
    return (scored[0] if scored else (None, None)), typical


def pick_torrent(results, album, artist, avoid=()):
    """A RuTracker release of this album (or a discography holding it), best format then seeders."""
    best = None
    for c in results:
        if c.get('source') != 'torrent' or c.get('torrent_key') in avoid:
            continue
        title = c.get('title') or ''
        # "Artist - Album - 2024, FLAC (tracks)": the name part before the year and format.
        name = re.split(r'\s[-–]\s(?:19|20)\d\d\b|,\s*(?:19|20)\d\d\b', title)[0]
        name = re.sub(r'^\s*(?:[\(\[][^\)\]]*[\)\]]\s*)+', '', name)   # "(Jazz Fusion) [TR24]" genre and format tags
        name_words = set(words(name))
        artist_words = set(words(clean_artist(artist)))
        if artist and not artist_words <= name_words:
            continue
        # Only a real discography: "Collection" also names compilations and unofficial soundtrack bundles.
        discography = bool(re.search(r'discograph|дискограф', title, re.I))
        if discography and set(words(re.split(r'\s[-–/]\s', name)[0])) - {'the'} != artist_words - {'the'}:
            continue   # "Blond Viper - Discography" is another band than "Viper"; so is "Viper Recordings"
        if not discography and not set(words(album)) <= name_words:
            continue
        if not discography and name_words - artist_words - set(words(album)) - {'ep', 'lp', 'single', 'album'}:
            continue   # "Mid Spiral: Order" is a chapter, "... Remixes" another record
        lowered = name.casefold()
        if any(re.search(r'\b' + re.escape(u) + r'(?:e?s)?\b', lowered) and u not in album.casefold() for u in UNWANTED):
            continue
        fmt, bitrate = (c.get('format') or '').lower(), c.get('bitrate') or 0
        if fmt in ('flac', 'alac', 'wav'):
            s = 3.0
        elif fmt == 'mp3' and bitrate >= 256:
            s = 2.0
        else:
            continue
        s += min(c.get('seeders') or 0, 50) / 50 - (1 if discography else 0)
        if not best or s > best[0]:
            best = (s, c)
    return best or (None, None)


def pick_youtube(results, album, artist):
    """The official YouTube Music release with exactly this title, by an artist we asked for."""
    core = lambda text: set(words(re.sub(r'\s*[\(\[].*?[\)\]]', '', text or '')))
    best = None
    for c in results:
        if c.get('source') != 'youtube' or c.get('kind') != 'album' or not c.get('official'):
            continue
        if artist and not set(words(clean_artist(artist))) & set(words(c.get('artist') or '')):
            continue
        title = c.get('title') or ''
        if core(title) != core(album):
            continue   # "Mid Spiral: Order" is a chapter, not "Mid Spiral"
        if any(re.search(r'\b' + re.escape(u) + r'(?:e?s)?\b', title.casefold()) and u not in album.casefold() for u in UNWANTED):
            continue
        s = (2 if set(words(title)) == set(words(album)) else 0) + {'Album': 1, 'EP': 0.5}.get(c.get('release_type'), 0)
        if not best or s > best[0]:
            best = (s, c)
    return best or (None, None)


def quality(c):
    """3 lossless, 2 for 256 kbps and up, 1 for 192 kbps and up, 0 for YouTube (lossy, ~130-160 kbps)."""
    if c.get('source') == 'youtube':
        return 0
    fmt, bitrate = (c.get('format') or '').lower(), c.get('bitrate') or 0
    if fmt in ('flac', 'alac', 'wav', 'aiff'):
        return 3
    return 2 if bitrate >= 256 else 1 if bitrate >= 192 else None


SOURCE_ORDER = {'soulseek': 0, 'torrent': 1, 'youtube': 2}
WELL_SEEDED = 3


def source_order(c):
    """Among copies of one quality: a seeded torrent first (they rarely fail once matched, while
    Soulseek uploaders often never answer), then Soulseek, then thin torrents, then YouTube."""
    if c.get('source') == 'torrent':
        return 0 if (c.get('seeders') or 0) >= WELL_SEEDED else 2
    return {'soulseek': 1, 'youtube': 3}.get(c.get('source'), 9)


def ranked(results, album, artist, avoid=(), limit=8):
    """Every result that is this album, best first: quality, then source_order, then each
    source's own score (edition, free slot, seeders...)."""
    albums = [c for c in results if c.get('kind') == 'album' and c.get('source') == 'soulseek'
              and c.get('username') not in avoid and identity(c, album, artist)]
    exact = [c for c in albums if not extra_words(c, album, artist)] or albums
    counts = collections.Counter(c.get('file_count') for c in exact if c.get('file_count'))
    typical = counts.most_common(1)[0][0] if counts else None
    scored, extra = [], {}
    for c in albums:
        r = rank(c, album, typical)
        if r is not None:
            extra[id(c)] = len(extra_words(c, album, artist))
            scored.append((r - 3 * extra[id(c)], c))
    for c in results:
        if c.get('source') == 'torrent':
            s, _ = pick_torrent([c], album, artist, avoid)
        elif c.get('source') == 'youtube' and c.get('url') not in avoid:
            s, _ = pick_youtube([c], album, artist)
        else:
            continue
        if s is not None:
            scored.append((s, c))
    scored = [(s, c) for s, c in scored if quality(c) is not None]
    # A folder with an extra word ("Album Order") may be another record: after every exact copy of that quality.
    scored.sort(key=lambda x: (-quality(x[1]), extra.get(id(x[1]), 0) > 0, source_order(x[1]), -x[0]))
    best, seen = [], set()
    for s, c in scored:
        who = provider(c)
        if who not in seen:   # one try per uploader or torrent: a failing uploader fails every folder
            seen.add(who)
            best.append({**c, 'score': round(s, 2), 'typical': typical})
    return best[:limit]


def provider(c):
    """Who a candidate comes from, as recorded when it fails: the uploader, the torrent, the playlist."""
    return c.get('torrent_key') or (c.get('url') if c.get('source') == 'youtube' else c.get('username'))
