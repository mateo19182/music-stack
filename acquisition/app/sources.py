"""Acquisition adapters. Sources retain their originals; workers receive private copies."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from urllib.parse import quote, unquote, urlsplit
import collections
import re
import shutil
import threading
import time
import unicodedata
import uuid

import httpx
import yt_dlp

AUDIO_EXTENSIONS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav", "aiff", "alac", "wma"}
# Names the Soulseek server drops whole searches for (an empty answer, not an error).
# A "*name" wildcard does not get through either, so the word is left out of the query.
FILTERED_TERMS = {"jpegmafia"}
# Same set Nicotine+ turns into spaces, plus Spanish/typographic marks.
_PUNCTUATION = re.compile(r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~¡¿’‘“”–—·•]")
_DISC_FOLDER = re.compile(r"^(?:dis[ck]|cd)\s*\d{1,2}$", re.I)


def soulseek_query(text):
    """Words a Soulseek peer can match: "¡OK!" → "ok", "MELBOURNE'S DEAD" → "melbourne dead".

    Peers match every query word against path words, so punctuation-bearing words, featured
    artists and bracketed qualifiers ("(Director's Cut)") only make a search miss.
    """
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = re.sub(r"[(\[{].*?[)\]}]", " ", text)
    text = re.sub(r"\s(?:feat|ft|featuring)\b.*$", " ", text)
    words = _PUNCTUATION.sub(" ", text).split()
    words = [w for w in words if (len(w) > 1 or w.isdigit()) and w not in FILTERED_TERMS]
    return " ".join(words)


def without_accents(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


class SearchLimiter:
    """One Soulseek search at a time, spaced out, within the server's rate.

    The server bans an account for 30 minutes when it searches too fast (sldl allows 34
    searches per 220 s); overlapping searches also come back empty. Callers wait their turn.
    """

    def __init__(self, per_window=30, window=220.0, spacing=5.0, clock=time.monotonic, sleep=time.sleep):
        self.per_window, self.window, self.spacing = per_window, window, spacing
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.started = collections.deque()

    def __enter__(self):
        self.lock.acquire()
        now = self.clock()
        while self.started and now - self.started[0] >= self.window:
            self.started.popleft()
        wait = 0.0
        if self.started:
            wait = max(wait, self.started[-1] + self.spacing - now)
        if len(self.started) >= self.per_window:
            wait = max(wait, self.started[0] + self.window - now)
        if wait > 0:
            self.sleep(wait)
        self.started.append(self.clock())
        return self

    def __exit__(self, *exc):
        self.lock.release()


soulseek_searches = SearchLimiter()

# Premium 256 kbps Opus, then Premium 256 kbps AAC, then the best audio for everyone (~130-160 kbps Opus).
YOUTUBE_FORMAT = "774/141/bestaudio/best"
# Albums the Wishlist found: Premium quality or nothing (another copy is tried instead).
YOUTUBE_PREMIUM_FORMAT = "774/141"
PREMIUM_FORMATS = {"774", "141"}
PREMIUM_PROBE = "https://music.youtube.com/watch?v=lYBUbBu4W08"   # any YouTube Music track
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}


class SourceError(RuntimeError):
    pass


class DownloadCancelled(SourceError):
    pass


class SoulseekUnavailable(SourceError):
    """slskd or its network is down or overloaded: not the uploader's fault, so the job waits."""


def _array(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return value.get("$values", list(value.values()))
    return []


def _get(value, key, default=None):
    if not isinstance(value, dict):
        return default
    return value.get(key, value.get(key[:1].upper() + key[1:], default))


def _state(transfer):
    value = _get(transfer, 'state', '')
    if isinstance(value, int) or str(value).isdigit():
        number = int(value)
        flags = {1: 'Requested', 2: 'Queued', 4: 'Initializing', 8: 'InProgress',
                 16: 'Completed', 32: 'Succeeded', 64: 'Cancelled', 128: 'TimedOut',
                 256: 'Errored', 512: 'Rejected', 1024: 'Aborted', 2048: 'Locally', 4096: 'Remotely'}
        return ', '.join(name for bit, name in flags.items() if number & bit)
    return str(value)


def _terminal(transfer):
    state = _state(transfer).casefold()
    return any(flag in state for flag in ('completed', 'succeeded', 'cancelled', 'timedout', 'errored', 'rejected', 'aborted', 'failed'))


def _queue_limited(transfer):
    """The uploader caps how much one user may queue ("Too many files"/"Too many megabytes"):
    the file can be requested again once the accepted ones finish."""
    reason = _detail(_get(transfer, 'exception') or _get(transfer, 'message') or _get(transfer, 'reason'))
    text = reason.casefold()
    # "Too many files this week/today" is a quota, not a queue limit: asking again will not help.
    return _terminal(transfer) and 'too many' in text and any(word in text for word in ('files', 'megabytes')) \
        and not any(word in text for word in ('week', 'today', 'day', 'month'))


def _not_accepted(transfer):
    """slskd gave up asking the uploader to queue this file (no answer within 3 minutes). A busy
    uploader often accepts the album's other files meanwhile: ask again once those finish."""
    reason = _detail(_get(transfer, 'exception') or _get(transfer, 'message') or _get(transfer, 'reason'))
    return _terminal(transfer) and 'enqueue remotely' in reason.casefold()


def _choose_transfer(matches):
    succeeded = [transfer for transfer in matches if 'succeeded' in _state(transfer).casefold()]
    active = [transfer for transfer in matches if not _terminal(transfer)]
    return (succeeded or active or matches)[-1] if matches else None


def _detail(value):
    if isinstance(value, dict):
        value = (_get(value, 'message') or _get(value, 'exception') or _get(value, 'reason')
                 or _get(value, 'item2') or '')
    return str(value or '').splitlines()[0][:240] if value else ''


def _transfer_failure(username, transfer):
    state = _state(transfer)
    reason = _detail(_get(transfer, 'exception') or _get(transfer, 'message') or _get(transfer, 'reason'))
    text = (state + ' ' + reason).casefold()
    peer = f"Soulseek peer {username}"
    retained = 'Partial files are retained. Retry to resume when the peer is available, or choose another uploader.'
    if 'verification required' in text:
        return (f'{peer} requires a human check: answer their private message in slskd (Messages), then retry '
                f'this job. {retained}')
    if 'banned' in text:
        return f'{peer} has banned this account. Choose another uploader. {retained}'
    if 'too many' in text and any(word in text for word in ('week', 'today', 'day', 'month')):
        return f'{peer} has a download quota ({reason}). Choose another uploader, or retry after it resets. {retained}'
    if 'enqueue remotely' in text:
        return f'{peer} never accepted the request (no answer within 3 minutes). {retained}'
    if any(word in text for word in ('offline', 'useroffline', 'user not found', 'unavailable')):
        return f'{peer} is offline or unavailable. {retained}'
    if any(word in text for word in ('timedout', 'timed out', 'timeout')):
        return f'{peer} did not respond in time. {retained}'
    if any(word in text for word in ('file not found', 'file not shared', 'not sharing', 'not shared')):
        return f'{peer} no longer shares this file. Choose another candidate. Partial files are retained.'
    if 'reject' in text or 'denied' in text or 'banned' in text or 'too many' in text:
        return f'{peer} refused the download' + (f': {reason}.' if reason else '.') + f' {retained}'
    if 'cancel' in text or 'abort' in text:
        return f'Download from {username} was interrupted. {retained}'
    return f'Download from {username} failed' + (f': {reason}.' if reason else f' ({state or "unknown state"}).') + f' {retained}'


_CHECK = re.compile(r'prove (that )?you are|human check|are you (a )?human|verify (that )?you|verification|captcha|'
                    r'please type|reply only|answer only|antworte nur', re.I)
_CHECK_DONE = re.compile(r'you are verified|you have been verified|unlocked|whitelisted', re.I)


def human_checks(conversations, answered_within=3600, now=None):
    """Uploaders whose private message asks for a human check (anti-leech plugins such as
    ProveIt). `open`: no reply from us since; `answered`: replied within the last hour, so the
    user can see the outcome and retry. The user types the answer; nothing here answers."""
    now = now or time.time()
    checks = []
    for conversation in conversations:
        messages = sorted(_array(_get(conversation, 'messages', [])), key=lambda m: _get(m, 'timestamp', ''))
        asked = None
        replied = None
        for m in messages:
            text = str(_get(m, 'message', ''))
            if _get(m, 'direction') == 'In' and _CHECK_DONE.search(text):
                asked, replied = None, None
            elif _get(m, 'direction') == 'In' and _CHECK.search(text):
                asked, replied = m, None
            elif _get(m, 'direction') == 'Out' and asked:
                replied = m
        if not asked:
            continue
        status = 'open'
        if replied:
            when = _timestamp(_get(replied, 'timestamp', ''))
            if not when or now - when > answered_within:
                continue
            status = 'answered'
        recent = messages[-6:]
        checks.append({'username': _get(conversation, 'username'), 'status': status,
                       'since': _get(asked, 'timestamp'), 'message': str(_get(asked, 'message', ''))[:500],
                       'recent': [{'direction': _get(m, 'direction'), 'message': str(_get(m, 'message', ''))[:500],
                                   'timestamp': _get(m, 'timestamp')} for m in recent]})
    return sorted(checks, key=lambda c: (c['status'] != 'open', c['since'] or ''))


def _timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _identity(*parts):
    return sha256("\0".join(str(p) for p in parts).encode()).hexdigest()[:32]


def youtube_url(url):
    """Accept only known media providers; never hand arbitrary URLs to GenericIE."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise SourceError("Invalid media URL.") from None
    allowed = (host in YOUTUBE_HOSTS or host in {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com", "vimeo.com", "www.vimeo.com", "player.vimeo.com", "bandcamp.com", "www.bandcamp.com"}
               or host.endswith(".bandcamp.com"))
    if (parsed.scheme not in {"http", "https"} or not allowed
            or parsed.username or parsed.password or port not in {None, 80, 443}):
        raise SourceError("Paste a YouTube, SoundCloud, Bandcamp or Vimeo URL. Other URL hosts are not supported.")
    return url


def normalize_soulseek(data, kind="track"):
    result, seen = [], set()
    responses = _array(_get(data, "responses", [])) if isinstance(data, dict) else _array(data)
    for response in responses:
        user = _get(response, "username", "")
        if not user:
            continue
        groups = {}
        for file in _array(_get(response, "files", [])):
            filename = _get(file, "filename", "")
            path = PureWindowsPath(filename)
            ext = path.suffix.lower().lstrip(".")
            if ext not in AUDIO_EXTENSIONS or _get(file, "isLocked", False):
                continue
            key = (user, filename)
            if key in seen:
                continue
            seen.add(key)
            normalized = {"filename": filename, "size": _get(file, "size", 0), "format": ext,
                          "bitrate": _get(file, "bitRate", _get(file, "bitrate")),
                          "duration": _get(file, "length")}
            candidate = {"id": _identity("soulseek", *key), "source": "soulseek", "kind": "track",
                         "title": path.stem, "artist": None, "album": None, "username": user,
                         **normalized, "queue_length": _get(response, "queueLength", 0),
                         "free_slots": _get(response, "hasFreeUploadSlot", False), "url": None,
                         "files": [normalized], "file_count": 1}
            if kind == "album":
                parent = path.parent
                if _DISC_FOLDER.match(parent.name) and parent.parent.name:
                    parent = parent.parent   # "Album\\CD1", "Album\\Disc 2" are one release
                folder = str(parent)
                groups.setdefault(folder, {**candidate, "id": _identity("soulseek", user, folder),
                                           "kind": "album", "title": parent.name, "album": parent.name,
                                           "directory": folder, "leaf_folder": parent.name, "files": [],
                                           "folder_complete": False})["files"].append(normalized)
            else:
                result.append(candidate)
        for candidate in groups.values():
            candidate["file_count"] = len(candidate["files"])
            candidate["size"] = sum(f["size"] or 0 for f in candidate["files"])
            candidate["formats"] = dict(collections.Counter(f["format"] for f in candidate["files"]))
            candidate["mixed_formats"] = len(candidate["formats"]) > 1
            result.append(candidate)
    # Generous cap: callers rank by identity first, so a busy peer's right album must survive.
    return sorted(result, key=lambda c: (not c["free_slots"], c["queue_length"] or 0, -(c["bitrate"] or 0)))[:500]


_SIZE_WORD = re.compile(r"[^\w]+")
_TORRENT_DONE = {"uploading", "stalledUP", "queuedUP", "forcedUP", "stoppedUP", "pausedUP", "checkingUP"}


def _torrent_format(title):
    """RuTracker titles end like "2024, FLAC (tracks), lossless" or "MP3, 320 kbps"."""
    lower = title.casefold()
    image = "image" in lower and "tracks" not in lower
    for name in ("flac", "alac", "mp3", "aac", "ogg", "opus", "wav", "ape", "wavpack"):
        if re.search(rf"\b{name}\b", lower):
            bitrate = re.search(r"(\d{3})\s*kbps", lower)
            return name, int(bitrate.group(1)) if bitrate else None, image
    if re.search(r"\[TR(?:16|24)\]", title):
        return "flac", None, image   # RuTracker's hi-res tag: "tracks, 16/24-bit lossless"
    return None, None, image


def normalize_torrents(releases):
    """Prowlarr releases as album candidates. Releases ripped as one file plus a CUE sheet
    ("image") are dropped: the album has to arrive as tracks."""
    result = []
    for release in _array(releases):
        title = _get(release, "title", "")
        link = _get(release, "downloadUrl") or _get(release, "magnetUrl")
        if not title or not link:
            continue
        fmt, bitrate, image = _torrent_format(title)
        if image or fmt in {"ape", "wavpack"}:
            continue
        seeders = _get(release, "seeders") or 0
        key = _identity("torrent", _get(release, "indexer", ""), _get(release, "guid") or link)
        result.append({"id": key, "torrent_key": key, "source": "torrent", "kind": "album", "title": title,
                       "artist": None, "album": None, "username": _get(release, "indexer") or "torrent",
                       "provider": _get(release, "indexer"), "filename": None, "format": fmt, "bitrate": bitrate,
                       "duration": None, "size": _get(release, "size"), "seeders": seeders,
                       "leechers": _get(release, "leechers"), "queue_length": None, "free_slots": None,
                       "url": None, "info_url": _get(release, "infoUrl"), "download_url": link,
                       "file_count": _get(release, "files"), "files": []})
    return sorted([c for c in result if c["seeders"] > 0], key=lambda c: -c["seeders"])


def _torrent_words(text):
    return set(_SIZE_WORD.sub(" ", without_accents(str(text or "")).casefold()).split())


def torrent_selection(files, album=None):
    """The audio files of one album in a torrent. A discography torrent holds many album
    folders; the one whose path names the requested album is taken."""
    audio = [f for f in files if PureWindowsPath(f["name"]).suffix.lower().lstrip(".") in AUDIO_EXTENSIONS
             and not PureWindowsPath(f["name"]).name.startswith("._")]
    if not audio:
        raise SourceError("The torrent has no audio files.")
    if len(audio) == 1 and any(f["name"].lower().endswith(".cue") for f in files):
        raise SourceError("The torrent holds the album as one file with a CUE sheet; choose a release with separate tracks.")
    groups = {}
    for f in audio:
        parent = PureWindowsPath(f["name"].replace("/", "\\")).parent
        if _DISC_FOLDER.match(parent.name) and parent.parent.name:
            parent = parent.parent
        groups.setdefault(str(parent), []).append(f)
    if len(groups) == 1:
        return audio
    wanted = _torrent_words(album)
    if not wanted:
        raise SourceError(f"The torrent holds {len(groups)} albums; queue it with the album name to choose one.")

    def score(folder):
        # Only the album's own folder name counts: the artist and discography names sit above it.
        own = _torrent_words(PureWindowsPath(folder).name)
        return len(wanted & own) / len(wanted), -len(own - wanted)

    best = max(groups, key=score)
    if score(best)[0] < 0.6:
        raise SourceError(f"None of the {len(groups)} albums in the torrent matches “{album}”. Choose another release.")
    return groups[best]


class _QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


class Sources:
    def __init__(self, config):
        self.config = config
        self.last_errors = {}
        self.completed_root = Path(config.get("slskd_download_root", "/data/downloads/slskd")).resolve()
        self.torrent_root = Path(config.get("torrent_download_root", "/downloads/torrents")).resolve()

    def _slskd(self, method, path, **kwargs):
        try:
            response = httpx.request(method, self.config.get("slskd_url", "http://slskd:5030").rstrip("/") + "/api/v0" + path,
                                     headers={"X-API-KEY": self.config.get("slskd_api_key", "")},
                                     timeout=kwargs.pop("timeout", 30), **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None
        except httpx.HTTPStatusError as exc:
            try:
                detail = _detail(exc.response.json())
            except ValueError:
                detail = _detail(exc.response.text)
            if exc.response.status_code == 429:
                raise SoulseekUnavailable('Soulseek is busy. Wait for the current operation to finish, then retry; partial files are retained.') from None
            parts = path.split('/')
            peer = (parts[3] if path.startswith('/transfers/downloads/') and len(parts) > 3
                    else parts[2] if path.startswith('/users/') else '')
            if exc.response.status_code == 500 and 'wait timed out' in detail.casefold() and peer:
                # Browsing or asking one peer: that peer did not answer within slskd's 5 s, not an outage
                # (a real outage also logs slskd out of the server, which the worker checks).
                raise SourceError(_transfer_failure(unquote(peer), {'state': 'Errored', 'message': detail})) from None
            if exc.response.status_code in (502, 503, 504) or exc.response.status_code == 500 and 'wait timed out' in detail.casefold():
                # slskd itself is unreachable or stuck: every request fails alike.
                raise SoulseekUnavailable(f'Soulseek returned HTTP {exc.response.status_code}' +
                                          (f': {detail}.' if detail else '.') + ' Retry when the connection recovers.') from None
            if path.startswith('/transfers/downloads/') and detail:
                username = unquote(path.split('/')[3])
                raise SourceError(_transfer_failure(username, {'state': 'Errored', 'message': detail})) from None
            raise SourceError(f'Soulseek returned HTTP {exc.response.status_code}' +
                              (f': {detail}.' if detail else '.') + ' Retry or choose another source.') from None
        except (httpx.HTTPError, ValueError):
            raise SoulseekUnavailable("Soulseek is unavailable; retry when the connection recovers.") from None

    def soulseek_connected(self):
        """True while slskd is logged in to the Soulseek server, False when it is logged out or
        unreachable, None when it is too busy to answer in time (it is running, so not an outage)."""
        try:
            response = httpx.get(self.config.get("slskd_url", "http://slskd:5030").rstrip("/") + "/api/v0/server",
                                 headers={"X-API-KEY": self.config.get("slskd_api_key", "")}, timeout=10)
            response.raise_for_status()
            return bool(_get(response.json() or {}, 'isLoggedIn'))
        except httpx.TimeoutException:
            return None
        except (httpx.HTTPError, ValueError):
            return False

    def soulseek_checks(self):
        conversations = []
        for conversation in _array(self._slskd('GET', '/conversations', params={'includeInactive': 'true'}) or []):
            username = _get(conversation, 'username')
            if username:
                conversations.append(self._slskd('GET', f'/conversations/{quote(username, safe="")}',
                                                 params={'includeMessages': 'true'}) or {})
        return human_checks(conversations)

    def reply(self, username, message):
        """Send the user's own reply to an uploader's private message."""
        self._slskd('POST', f'/conversations/{quote(username, safe="")}', json=message)
        self._slskd('PUT', f'/conversations/{quote(username, safe="")}')

    def _youtube_options(self):
        options = {"quiet": True, "no_warnings": True, "logger": _QuietLogger(), "socket_timeout": 20,
                   "retries": 2, "extractor_retries": 2, "cachedir": False,
                   "js_runtimes": {"node": {"path": self.config.get("node_path", "node")}},
                   "allowed_extractors": ["youtube.*", "soundcloud.*", "bandcamp.*", "vimeo.*"]}
        # A YouTube Premium account's cookies unlock 256 kbps audio (formats 774 and 141).
        cookies = self.config.get("youtube_cookies_file")
        if cookies and Path(cookies).is_file():
            options["cookiefile"] = cookies
        return options

    def youtube_premium(self):
        """True while the cookies still unlock Premium audio, False once they stopped, None
        without a cookies file or when YouTube could not be asked."""
        options = self._youtube_options()
        if "cookiefile" not in options or not self.config.get("youtube_enabled", True):
            return None
        try:
            with yt_dlp.YoutubeDL({**options, "skip_download": True}) as client:
                info = client.extract_info(self.config.get("youtube_premium_probe", PREMIUM_PROBE), download=False)
        except Exception:
            return None
        return any(f.get("format_id") in PREMIUM_FORMATS for f in info.get("formats") or [])

    def search(self, query, source="all", kind="track"):
        query = query.strip()
        if not query:
            raise SourceError("Enter a search or YouTube URL.")
        if source not in {"all", "soulseek", "youtube", "torrent"} or kind not in {"track", "album"}:
            raise SourceError("Unsupported search source or kind.")
        youtube_paused = not self.config.get("youtube_enabled", True)
        if youtube_paused and source == "youtube":
            raise SourceError("YouTube is paused.")
        if urlsplit(query).scheme or query.startswith(("www.", "youtube.com/", "youtu.be/")):
            if youtube_paused:
                raise SourceError("YouTube is paused.")
            youtube_url(query)
            if source in {"soulseek", "torrent"}:
                raise SourceError("Use the YouTube source for a YouTube URL.")
            self.last_errors = {}
            return self._search_youtube(query, kind)
        selected = {"soulseek": self._search_soulseek, "youtube": self._search_youtube}
        if youtube_paused:
            del selected["youtube"]
        if self.torrents_configured() and (source == "torrent" or kind == "album"):
            selected["torrent"] = self._search_torrent
        if source != "all":
            selected = {source: selected[source]}
        errors, results = {}, []
        with ThreadPoolExecutor(max_workers=len(selected)) as executor:
            futures = {name: executor.submit(search, query, kind) for name, search in selected.items()}
            for name, future in futures.items():
                try:
                    results.extend(future.result())
                except Exception as exc:
                    errors[name] = str(exc) if isinstance(exc, SourceError) else f"{name} search failed. Try again."
        self.last_errors = errors
        if len(errors) == len(selected):
            raise SourceError(" ".join(errors.values()))
        return results

    def _search_soulseek(self, query, kind):
        cleaned = soulseek_query(query)
        if not cleaned:
            raise SourceError("Nothing searchable on Soulseek in that query; add an album or track name.")
        results = self._soulseek_once(cleaned, kind)
        plain = without_accents(cleaned)
        if plain != cleaned:
            # Shares are often named without accents ("Rehuso" for "Rehúso").
            known = {c["id"] for c in results}
            results += [c for c in self._soulseek_once(plain, kind) if c["id"] not in known]
        return results

    def _soulseek_once(self, query, kind):
        search_id = str(uuid.uuid4())
        seconds = float(self.config.get("slskd_search_seconds", 20))
        with soulseek_searches:
            self._slskd("POST", "/searches", json={"id": search_id, "searchText": query,
                         "searchTimeout": int(seconds * 1000), "fileLimit": 1000, "responseLimit": 150,
                         "filterResponses": True, "maximumPeerQueueLength": 150,
                         "minimumPeerUploadSpeed": 0, "minimumResponseFileCount": 1})
            try:
                # searchTimeout restarts on every response, and slskd stores responses only when the
                # search ends: read them after endedAt, never on a fixed clock.
                deadline = time.monotonic() + seconds + 45
                cancelled = False
                while True:
                    data = self._slskd("GET", f"/searches/{search_id}") or {}
                    if _get(data, "endedAt"):
                        break
                    if time.monotonic() >= deadline:
                        if cancelled:
                            break
                        self._slskd("PUT", f"/searches/{search_id}")   # stop it; slskd then stores what arrived
                        cancelled, deadline = True, time.monotonic() + 10
                    time.sleep(1)
                return normalize_soulseek(self._slskd("GET", f"/searches/{search_id}/responses") or [], kind)
            finally:
                try:
                    self._slskd("DELETE", f"/searches/{search_id}")
                except SourceError:
                    pass

    def _search_youtube(self, query, kind):
        direct = bool(urlsplit(query).scheme)
        if kind == "album" and not direct:
            return self._search_youtube_albums(query)
        target = youtube_url(query) if direct else f"ytsearch12:{query}"
        options = {**self._youtube_options(), "extract_flat": "in_playlist", "skip_download": True,
                   "noplaylist": kind == "track" and direct}
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(target, download=False)
        except Exception:
            raise SourceError("yt-dlp search failed. Retry or paste a supported media URL.") from None
        if not info:
            return []
        is_album = direct and kind == "album" and bool(info.get("entries"))
        entries = [info] if is_album else list(info.get("entries") or [info])
        result = []
        for entry in entries:
            if not entry:
                continue
            url = entry.get("webpage_url") or entry.get("url") or f'https://www.youtube.com/watch?v={entry["id"]}'
            if is_album:
                url = query
            try:
                youtube_url(url)
            except SourceError:
                continue
            result.append({"id": _identity("youtube", url), "source": "youtube", "kind": "album" if is_album else "track",
                           "title": entry.get("track") or entry.get("title") or entry["id"],
                           "artist": entry.get("artist"), "provider": entry.get("extractor_key") or entry.get("extractor"), "uploader": entry.get("uploader") or entry.get("channel"),
                           "album": entry.get("album"), "username": None, "filename": None,
                           "format": entry.get("ext"), "bitrate": entry.get("abr"), "duration": entry.get("duration"),
                           "size": entry.get("filesize") or entry.get("filesize_approx"), "queue_length": None,
                           "free_slots": None, "url": url, "files": [],
                           "file_count": len(entry.get("entries") or []) if is_album else 1})
        return result

    def _search_youtube_albums(self, query, limit=5):
        """Official releases on YouTube Music: the album playlists (ids "OLAK5uy_") that YouTube
        builds from label uploads, with tracks on the artist's "Topic" or own channel."""
        options = {**self._youtube_options(), "extract_flat": "in_playlist", "skip_download": True}
        try:
            with yt_dlp.YoutubeDL({**options, "playlistend": limit}) as downloader:
                found = downloader.extract_info("https://music.youtube.com/search?q=" + quote(query) + "#albums", download=False)
        except Exception:
            raise SourceError("YouTube Music search failed. Retry later.") from None
        result = []
        for entry in (found or {}).get("entries") or []:
            try:
                with yt_dlp.YoutubeDL(options) as downloader:
                    info = downloader.extract_info(entry["url"], download=False)
            except Exception:
                continue   # one album page failing must not lose the others
            tracks = [t for t in info.get("entries") or [] if t]
            if not str(info.get("id", "")).startswith("OLAK5uy_") or not tracks:
                continue
            kind_label, _, title = str(info.get("title") or "").partition(" - ")
            if not title:
                kind_label, title = "Album", kind_label
            channel = tracks[0].get("channel") or tracks[0].get("uploader") or ""
            url = f"https://www.youtube.com/playlist?list={info['id']}"
            result.append({"id": _identity("youtube", url), "source": "youtube", "kind": "album", "title": title,
                           "album": title, "release_type": kind_label, "artist": channel.removesuffix(" - Topic") or None,
                           "provider": "YouTube Music", "uploader": channel, "official": True, "username": None,
                           "filename": None, "format": None, "bitrate": None,
                           "duration": sum(t.get("duration") or 0 for t in tracks) or None, "size": None,
                           "queue_length": None, "free_slots": None, "url": url, "files": [],
                           "file_count": info.get("playlist_count") or len(tracks)})
        return result

    def download(self, candidate, destination, progress, cancelled):
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if cancelled():
            raise DownloadCancelled("Download cancelled.")
        if candidate.get("source") == "youtube":
            return self._download_youtube(candidate, destination, progress, cancelled)
        if candidate.get("source") == "soulseek":
            return self._download_soulseek(candidate, destination, progress, cancelled)
        if candidate.get("source") == "torrent":
            return self._download_torrent(candidate, destination, progress, cancelled)
        raise SourceError("Unsupported download source.")

    def torrents_configured(self):
        return bool(self.config.get("prowlarr_url") and self.config.get("prowlarr_api_key"))

    def _prowlarr(self, path, **params):
        try:
            response = httpx.get(self.config["prowlarr_url"].rstrip("/") + "/api/v1" + path, params=params,
                                 headers={"X-Api-Key": self.config["prowlarr_api_key"]}, timeout=90)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, KeyError):
            raise SourceError("Torrent search (Prowlarr) failed. Retry later.") from None

    def _search_torrent(self, query, kind):
        if kind != "album":
            return []   # torrents hold whole releases
        if not self.torrents_configured():
            raise SourceError("Torrent search is not set up.")
        if not any(i.get("enable") for i in _array(self._prowlarr("/indexer"))):
            raise SourceError("No torrent tracker is enabled in Prowlarr.")   # an empty answer would read as "not found"
        return normalize_torrents(self._prowlarr("/search", query=query, categories=3000, type="search", limit=100))

    def _qbit(self, method, path, **kwargs):
        base = self.config.get("qbittorrent_url", "http://qbittorrent:8091").rstrip("/") + "/api/v2"
        for attempt in range(2):
            if not getattr(self, "_qbit_client", None):
                self._qbit_client = httpx.Client(timeout=60, headers={"Referer": base})
                login = self._qbit_client.post(base + "/auth/login", data={
                    "username": self.config.get("qbittorrent_username", "admin"),
                    "password": self.config.get("qbittorrent_password", "")})
                if login.status_code >= 400 or not self._qbit_client.cookies:
                    self._qbit_client = None
                    raise SourceError("qBittorrent refused the login. Check its credentials in the settings.")
            try:
                response = self._qbit_client.request(method, base + path, **kwargs)
            except httpx.HTTPError:
                self._qbit_client = None
                raise SourceError("qBittorrent is unavailable. Retry when it is running.") from None
            if response.status_code == 403 and attempt == 0:
                self._qbit_client = None   # session expired
                continue
            if response.status_code >= 400:
                raise SourceError(f"qBittorrent returned HTTP {response.status_code}.")
            return response.json() if "json" in response.headers.get("content-type", "") else response.text
        raise SourceError("qBittorrent refused the request.")

    def _torrent_info(self, tag):
        found = self._qbit("GET", "/torrents/info", params={"tag": tag})
        return found[0] if found else None

    def _add_torrent(self, candidate, tag):
        link = candidate["download_url"]
        data = {"tags": tag, "category": "acquisition", "savepath": self.torrent_root.as_posix()}
        stopped = {"stopped": "true", "paused": "true"}   # start once the album's files are chosen
        if not link.startswith("magnet:"):
            try:
                response = httpx.get(link, timeout=60, follow_redirects=False)
                if response.is_redirect and response.headers.get("location", "").startswith("magnet:"):
                    link = response.headers["location"]
                else:
                    response.raise_for_status()
                    self._qbit("POST", "/torrents/add", data={**data, **stopped}, files={"torrents": ("release.torrent", response.content)})
                    return
            except httpx.HTTPError:
                raise SourceError("Could not fetch the .torrent file from the tracker. Retry or choose another release.") from None
        # A stopped magnet never fetches its file list: start it, and choose files once it has one.
        self._qbit("POST", "/torrents/add", data={**data, "urls": link})

    def _start_torrent(self, hash):
        try:
            self._qbit("POST", "/torrents/start", data={"hashes": hash})
        except SourceError:
            self._qbit("POST", "/torrents/resume", data={"hashes": hash})   # qBittorrent 4

    def _release_torrent(self, hash, indexes):
        """Stop wanting these files; delete the torrent when no job wants anything from it."""
        if indexes:
            self._qbit("POST", "/torrents/filePrio", data={"hash": hash, "id": "|".join(map(str, indexes)), "priority": 0})
        files = self._qbit("GET", "/torrents/files", params={"hash": hash}) or []
        if not any(f.get("priority") for f in files):
            self._qbit("POST", "/torrents/delete", data={"hashes": hash, "deleteFiles": "true"})

    def _download_torrent(self, candidate, destination, progress, cancelled):
        tag = "acq-" + candidate.get("torrent_key", candidate["id"])[:16]
        added = not self._torrent_info(tag)
        if added:
            self._add_torrent(candidate, tag)
            progress({"message": "Added to qBittorrent; fetching the torrent's file list"})
        stall = float(self.config.get("download_stall_seconds", 600))
        deadline = time.monotonic() + stall
        while True:
            info = self._torrent_info(tag)
            files = self._qbit("GET", "/torrents/files", params={"hash": info["hash"]}) if info else []
            if files and info.get("state") != "metaDL":
                break
            if cancelled():
                raise DownloadCancelled("Download cancelled.")
            if time.monotonic() >= deadline:
                if info:
                    self._qbit("POST", "/torrents/delete", data={"hashes": info["hash"], "deleteFiles": "true"})
                raise SourceError(f"No peer sent the torrent's file list within {round(stall / 60)} minutes (no seeders). Choose another release.")
            time.sleep(float(self.config.get("torrent_poll_seconds", 5)))
        hash = info["hash"]
        if not candidate.get("torrent_files"):
            chosen = torrent_selection(files, candidate.get("requested_album"))
            wanted = {f["index"] for f in chosen}
            skipped = [f["index"] for f in files if f["index"] not in wanted]
            if added and skipped:
                # Another job may share a torrent it added: then only switch our own files on.
                self._qbit("POST", "/torrents/filePrio", data={"hash": hash, "id": "|".join(map(str, skipped)), "priority": 0})
            self._qbit("POST", "/torrents/filePrio", data={"hash": hash, "id": "|".join(map(str, wanted)), "priority": 1})
            candidate.update(torrent_hash=hash, torrent_files=sorted(wanted), file_count=len(wanted),
                             source_title=info.get("name") or candidate.get("title"))
            progress({"candidate": candidate, "message": f"Downloading {len(wanted)} of the torrent's {len(files)} files"})
        self._start_torrent(hash)
        wanted = set(candidate["torrent_files"])
        best, stalled_since = -1, time.monotonic()
        while True:
            if cancelled():
                raise DownloadCancelled("Download cancelled.")
            info = self._torrent_info(tag)
            if not info:
                raise SourceError("The torrent was removed from qBittorrent. Retry to add it again.")
            files = [f for f in self._qbit("GET", "/torrents/files", params={"hash": hash}) if f["index"] in wanted]
            total = sum(f["size"] for f in files) or 1
            got = sum(f["size"] * f.get("progress", 0) for f in files)
            if info.get("state") in {"error", "missingFiles"}:
                raise SourceError(f"qBittorrent reports the torrent as {info['state']}. Retry or choose another release.")
            if files and all(f.get("progress", 0) >= 1 for f in files):
                break
            if got > best:
                best, stalled_since = got, time.monotonic()
            elif time.monotonic() - stalled_since >= stall:
                self._release_torrent(hash, sorted(wanted))
                raise SourceError(f"The torrent made no progress for {round(stall / 60)} minutes "
                                  f"({info.get('num_seeds', 0)} seeders connected); it was removed. Choose another release.")
            seeds = info.get("num_seeds", 0)
            progress({"percent": round(100 * got / total, 1),
                      "message": f"{round(100 * got / total)}% · {seeds} seeder{'s' if seeds != 1 else ''} · "
                                 f"{round((info.get('dlspeed') or 0) / 1024)} KB/s"})
            time.sleep(float(self.config.get("torrent_poll_seconds", 5)))
        root = Path(info.get("save_path") or self.torrent_root).resolve()
        result = []
        for index, file in enumerate(sorted(files, key=lambda f: f["name"])):
            source = (root / file["name"]).resolve()
            if not source.is_relative_to(self.torrent_root) or not source.is_file():
                raise SourceError("A finished torrent file is missing from the download folder. Retry.")
            target = destination / f"{index + 1:03d}-{source.name}"
            if not target.exists() or target.stat().st_size != source.stat().st_size:
                temporary = target.with_suffix(target.suffix + ".copying")
                shutil.copy2(source, temporary)
                temporary.replace(target)
            result.append(target)
        return result   # the torrent keeps seeding from the download folder

    def _download_youtube(self, candidate, destination, progress, cancelled):
        url = youtube_url(candidate["url"])
        started = time.monotonic()
        timeout = float(self.config.get("download_timeout_seconds", 3600))

        def hook(data):
            metadata = data.get("info_dict")
            if metadata:
                # yt-dlp otherwise writes uploader as artist and categories as genre.
                # Keep the uploader in provenance, but tag only declared music metadata.
                metadata["meta_artist"] = metadata.get("artist") or ", ".join(metadata.get("artists") or [])
                metadata["meta_genre"] = metadata.get("genre") or ", ".join(metadata.get("genres") or [])
                metadata["meta_album"] = metadata.get("album") or ""
            if cancelled():
                raise DownloadCancelled("Download cancelled. Partial audio is kept for retry.")
            if time.monotonic() - started > timeout:
                raise SourceError("YouTube download timed out. Retry to resume.")
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            progress({"percent": round(100 * data.get("downloaded_bytes", 0) / total, 1) if total else None,
                      "message": "Downloading audio" if data.get("status") == "downloading" else "Preparing audio"})

        # Inspect the complete playlist before downloading. An explicit limit fails the
        # job rather than silently turning the first N tracks into a complete album.
        preflight = {**self._youtube_options(), "extract_flat": "in_playlist", "skip_download": True,
                     "noplaylist": candidate.get("kind") != "album", "ignoreerrors": False}
        try:
            with yt_dlp.YoutubeDL(preflight) as downloader:
                source_info = downloader.extract_info(url, download=False)
        except Exception:
            raise SourceError("Could not inspect the media source. Retry or choose another candidate.") from None
        entries = self._validate_playlist(source_info, candidate)
        candidate["source_title"] = source_info.get("track") or source_info.get("title")
        candidate["source_metadata"] = self._metadata(source_info)
        if candidate["source_title"]:
            candidate["title"] = candidate["source_title"]
        for key in ("artist", "album"):
            if source_info.get(key):
                candidate[key] = source_info[key]
        candidate["source_files"] = [self._metadata(entry) for entry in entries]
        candidate["file_count"] = len({str(entry["id"]) for entry in entries})
        progress({"candidate": candidate, "message": "Source identity confirmed", "percent": 0})
        hook({})
        options = {**self._youtube_options(),
                   "format": YOUTUBE_PREMIUM_FORMAT if candidate.get("wishlist_id") else YOUTUBE_FORMAT, "noplaylist": candidate.get("kind") != "album",
                   "ignoreerrors": False,
                   "outtmpl": str(destination / "%(id)s.%(ext)s"), "restrictfilenames": True,
                   "continuedl": True, "overwrites": False, "progress_hooks": [hook],
                   # The video thumbnail becomes embedded cover art; Navidrome otherwise shows none.
                   "writethumbnail": True, "postprocessor_hooks": [hook], "postprocessors": [
                       {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
                       {"key": "FFmpegExtractAudio", "preferredcodec": "best"},
                       {"key": "FFmpegMetadata", "add_metadata": True},
                       {"key": "EmbedThumbnail", "already_have_thumbnail": False}], "keepvideo": False}
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloaded_info = downloader.extract_info(url, download=True)
                downloaded_entries = self._validate_playlist(downloaded_info, candidate)
                candidate["source_files"] = [self._metadata(entry) for entry in downloaded_entries]
                candidate["source_metadata"] = self._metadata(downloaded_info)
                candidate["source_title"] = downloaded_info.get("track") or downloaded_info.get("title") or candidate.get("source_title")
                if candidate.get("source_title"):
                    candidate["title"] = candidate["source_title"]
                for key in ("artist", "album"):
                    if downloaded_info.get(key):
                        candidate[key] = downloaded_info[key]
                progress({"candidate": candidate, "message": "Audio downloaded", "percent": 100})
        except DownloadCancelled:
            raise
        except Exception as exc:
            if cancelled():
                raise DownloadCancelled("Download cancelled. Retry to resume.") from None
            reason = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "")[:240]
            raise SourceError(f"yt-dlp download failed ({reason}). Retry or choose another candidate.") from None
        files = [p for p in destination.iterdir() if p.is_file() and p.suffix.lstrip(".").lower() in AUDIO_EXTENSIONS]
        expected_ids = {str(entry["id"]) for entry in entries}
        actual_ids = {p.stem for p in files}
        if not files or not expected_ids.issubset(actual_ids):
            raise SourceError("Some selected audio files are missing. Retry to complete the download before processing.")
        return sorted(p for p in files if p.stem in expected_ids)

    @staticmethod
    def _metadata(info):
        keys = ("id", "title", "track", "artist", "album", "uploader", "channel", "webpage_url", "extractor", "extractor_key", "duration")
        return {key: info[key] for key in keys if info.get(key) is not None}

    def _validate_playlist(self, info, candidate):
        if not info:
            raise SourceError("The source returned no media.")
        playlist = info.get("entries") is not None
        entries = list(info["entries"]) if playlist else [info]
        if playlist and candidate.get("kind") != "album":
            raise SourceError("This URL is a playlist. Select album / playlist to download it.")
        limit = int(self.config.get("max_playlist_files", 500))
        if len(entries) > limit or (info.get("playlist_count") or 0) > limit:
            raise SourceError(f"Playlist exceeds the {limit}-file limit. Choose a smaller playlist or individual tracks.")
        if not entries or any(not entry or not entry.get("id") for entry in entries):
            raise SourceError("A playlist entry is unavailable. Choose another source or download individual tracks.")
        if info.get("playlist_count") and info["playlist_count"] > len(entries):
            raise SourceError("The source returned an incomplete playlist. Download individual tracks or choose another source.")
        return entries

    def _transfers(self, username):
        data = self._slskd("GET", "/transfers/downloads")
        return [file for user in _array(data) if _get(user, "username") == username
                for directory in _array(_get(user, "directories", []))
                for file in _array(_get(directory, "files", [])) if not _get(file, "removed", False)]

    def _completed_file(self, transfer):
        filename = PureWindowsPath(_get(transfer, "filename", ""))
        if any(part in {"..", "."} for part in filename.parts) or not filename.name:
            raise SourceError("Unsafe Soulseek filename.")
        # slskd keeps the last directory component, not the peer's full remote path.
        relative = Path(filename.parent.name) / filename.name
        preferred = (self.completed_root / relative).resolve()
        if not preferred.is_relative_to(self.completed_root):
            raise SourceError("Soulseek path escapes the completed directory.")
        expected_size = int(_get(transfer, "size", 0) or 0)
        if preferred.is_file() and preferred.stat().st_size == expected_size:
            return preferred
        matches = [p.resolve() for p in self.completed_root.rglob("*")
                   if p.name == filename.name and p.resolve().is_relative_to(self.completed_root) and p.is_file() and p.stat().st_size == expected_size]
        if len(matches) != 1:
            raise SourceError("Completed Soulseek file is missing or ambiguous. Retry or choose another source.")
        return matches[0]

    def _download_soulseek(self, candidate, destination, progress, cancelled):
        username = candidate["username"]
        if not username or not candidate.get("files"):
            raise SourceError("Soulseek candidate has no downloadable files.")
        base = f"/transfers/downloads/{quote(username, safe='')}"
        if candidate.get("kind") == "album" and not candidate.get("folder_complete"):
            directory = candidate["directory"]
            response = self._slskd("POST", f"/users/{quote(username, safe='')}/directory", json={"directory": directory})
            files = []
            for folder in _array(response):
                name = _get(folder, "name", _get(folder, "directory", ""))
                if name.rstrip("\\") != directory.rstrip("\\"):
                    continue
                for file in _array(_get(folder, "files", [])):
                    filename = _get(file, "filename", "")
                    if not filename:
                        continue
                    if "\\" not in filename:
                        filename = directory.rstrip("\\") + "\\" + filename
                    if PureWindowsPath(filename).suffix.lower().lstrip(".") in AUDIO_EXTENSIONS:
                        files.append({"filename": filename, "size": _get(file, "size", 0)})
            if not files and not _array(response) and len(candidate["files"]) >= 2:
                # Some clients answer every folder request with nothing. Their search results
                # list the folder's files, so download those (the review still checks the album).
                progress({"candidate": candidate, "message": f"{username} does not list folders; "
                          f"downloading the {len(candidate['files'])} files from the search results"})
            elif not files:
                raise SourceError("Could not confirm the full album folder. Try another uploader.")
            else:
                candidate.update(files=files, file_count=len(files), folder_complete=True)
                progress({"candidate": candidate, "message": f"Confirmed {len(files)} album files"})
        files = candidate["files"]
        for file in files:
            remote = PureWindowsPath(file["filename"])
            if ".." in remote.parts or not remote.name or remote.name in {".", ".."}:
                raise SourceError("Unsafe Soulseek filename.")
        stale_completed = set()
        def choose(matches):
            usable = []
            for transfer in matches:
                identifier = _get(transfer, 'id')
                if identifier in stale_completed:
                    continue
                if 'succeeded' in _state(transfer).casefold():
                    try:
                        self._completed_file(transfer)
                    except SourceError:
                        # A retained API record does not imply the source file still
                        # exists. Reenqueue it once on explicit retry, without deleting
                        # its partials or cancelling another active attempt.
                        stale_completed.add(identifier)
                        continue
                usable.append(transfer)
            return _choose_transfer(usable)

        transfers = self._transfers(username)
        selected = {}
        for file in files:
            matches = [t for t in transfers if _get(t, "filename") == file["filename"] and _get(t, "size") == file["size"]]
            transfer = choose(matches)
            if transfer and (not _terminal(transfer) or 'succeeded' in _state(transfer).casefold()):
                selected[file['filename']] = transfer
        def enqueue(missing, transfers):
            response = self._slskd("POST", base, json=[{"filename": f["filename"], "size": f["size"]} for f in missing])
            enqueued = _array(_get(response or {}, "enqueued", [])) if isinstance(response, dict) else []
            previous_ids = {_get(t, "id") for t in transfers}
            owned_ids = set(candidate.get("owned_transfer_ids", []))
            owned_ids.update(_get(t, "id") for t in enqueued if _get(t, "id") and _get(t, "id") not in previous_ids)
            candidate["owned_transfer_ids"] = sorted(owned_ids)
            failures = _array(_get(response or {}, "failed", [])) if isinstance(response, dict) else []
            if failures:
                # A concurrent job may have queued the same file between our GET
                # and POST. Reuse that transfer; do not fail or enqueue it again.
                current = self._transfers(username)
                unresolved = [file for file in missing if not any(
                    _get(t, 'filename') == file['filename'] and _get(t, 'size') == file['size']
                    and _get(t, 'id') not in stale_completed
                    and (not _terminal(t) or 'succeeded' in _state(t).casefold()) for t in current)]
                if unresolved:
                    first = next((failure for failure in failures if
                                  _get(failure, 'filename', _get(failure, 'item1')) == unresolved[0]['filename']), failures[0])
                    raise SourceError(_transfer_failure(username, {'state': 'Errored', 'message': _detail(first)}))

        missing = [f for f in files if f["filename"] not in selected]
        if missing:
            enqueue(missing, transfers)
            progress({"candidate": candidate, "message": "Downloads enqueued", "percent": 0})
        # No bytes and no finished file for download_stall_seconds, from the start or the last
        # progress, and the uploader is dropped: waiting in a queue that does not move is a failure.
        stall = float(self.config.get("download_stall_seconds", 600))
        deadline = time.monotonic() + stall
        result = []
        best_done, best_bytes = 0, 0
        while time.monotonic() < deadline:
            if cancelled():
                self.cancel(candidate)
                raise DownloadCancelled("Download cancelled. Existing source files are kept.")
            transfers = self._transfers(username)
            done, transferred, total = 0, 0, sum(int(f["size"] or 0) for f in files)
            ids = []
            states = []
            limited, unanswered = [], []
            for file in files:
                matches = [t for t in transfers if _get(t, "filename") == file["filename"] and _get(t, "size") == file["size"]]
                if not matches:
                    continue
                # Reuse the completed transfer or the most recent attempt for this file.
                transfer = choose(matches)
                if transfer is None:
                    continue
                ids.append(_get(transfer, "id"))
                state = _state(transfer)
                states.append(state)
                transferred += int(_get(transfer, "bytesTransferred", 0) or 0)
                if 'succeeded' in state.casefold():
                    done += 1
                elif _queue_limited(transfer):
                    limited.append(file)
                elif _not_accepted(transfer):
                    unanswered.append((file, transfer))
                elif _terminal(transfer):
                    raise SourceError(_transfer_failure(username, transfer))
            candidate["transfer_ids"] = ids
            if unanswered:
                if not done and not limited and not any(not _terminal(t) for t in transfers if _get(t, "id") in ids):
                    raise SourceError(_transfer_failure(username, unanswered[0][1]))   # accepted nothing: gone
                limited += [file for file, _ in unanswered]
            if done > best_done or transferred > best_bytes:
                best_done, best_bytes = max(done, best_done), max(transferred, best_bytes)
                deadline = time.monotonic() + stall
            if limited and not any(not _terminal(t) for t in transfers if _get(t, "id") in ids):
                # Everything the uploader accepted has finished: ask for the refused files again.
                enqueue(limited, transfers)
                progress({'candidate': candidate, 'message': f'{done}/{len(files)} files complete. {username} limits queued '
                          f'files; requested the remaining {len(limited)} again.'})
                time.sleep(float(self.config.get("slskd_poll_seconds", 2)))
                continue
            waiting = any('queued' in state.casefold() and 'remotely' in state.casefold() for state in states)
            positions = [_get(t, 'placeInQueue') for t in transfers if _get(t, 'id') in ids and _get(t, 'placeInQueue') is not None]
            message = f'{done}/{len(files)} files complete. '
            if waiting:
                message += f'Waiting in {username} remote queue' + (f' (position {min(positions)})' if positions else '') + '.'
            elif limited:
                message += f'{username} limits queued files; {len(limited)} more will be requested when these finish.'
            else:
                message += ', '.join(sorted(set(states))) or 'Waiting for Soulseek to confirm the transfer.'
            progress({'candidate': candidate, 'percent': min(100, round(100 * transferred / total, 1)) if total else None,
                      'message': message})
            if done == len(files):
                for index, file in enumerate(files):
                    transfer = next(t for t in reversed(transfers) if _get(t, "filename") == file["filename"]
                                    and _get(t, "size") == file["size"] and 'succeeded' in _state(t).casefold())
                    source = self._completed_file(transfer)
                    target = destination / f"{index + 1:03d}-{source.name}"
                    if not target.exists() or target.stat().st_size != source.stat().st_size:
                        temporary = target.with_suffix(target.suffix + ".copying")
                        shutil.copy2(source, temporary)
                        temporary.replace(target)
                    result.append(target)
                return result
            time.sleep(float(self.config.get("slskd_poll_seconds", 2)))
        self.cancel(candidate)
        minutes = round(stall / 60)
        if best_done or best_bytes:
            raise SourceError(f'{username} stopped sending for {minutes} minutes ({best_done}/{len(files)} files complete); '
                              'trying another copy.')
        raise SourceError(f'{username} did not start sending within {minutes} minutes (busy or queued); trying another copy.')

    def cancel(self, candidate):
        if candidate.get("source") == "torrent":
            if candidate.get("torrent_hash"):
                try:
                    self._release_torrent(candidate["torrent_hash"], candidate.get("torrent_files", []))
                except SourceError:
                    pass
            return
        if candidate.get("source") != "soulseek":
            return
        # A job may reuse transfers started by another job or Aurral. Only cancel
        # IDs returned by this job's enqueue response, never all matching files.
        for transfer_id in candidate.get("owned_transfer_ids", []):
            try:
                self._slskd("DELETE", f'/transfers/downloads/{quote(candidate["username"], safe="")}/{quote(str(transfer_id), safe="")}',
                            params={"remove": "false"})
            except SourceError:
                pass
