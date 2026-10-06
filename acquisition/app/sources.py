"""Acquisition adapters. Sources retain their originals; workers receive private copies."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from urllib.parse import quote, unquote, urlsplit
import shutil
import time
import uuid

import httpx
import yt_dlp

AUDIO_EXTENSIONS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav", "aiff", "alac", "wma"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}


class SourceError(RuntimeError):
    pass


class DownloadCancelled(SourceError):
    pass


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
                folder = str(path.parent)
                groups.setdefault(folder, {**candidate, "id": _identity("soulseek", user, folder),
                                           "kind": "album", "title": path.parent.name, "album": path.parent.name,
                                           "directory": folder, "files": [], "folder_complete": False})["files"].append(normalized)
            else:
                result.append(candidate)
        for candidate in groups.values():
            candidate["file_count"] = len(candidate["files"])
            candidate["size"] = sum(f["size"] or 0 for f in candidate["files"])
            result.append(candidate)
    return sorted(result, key=lambda c: (not c["free_slots"], c["queue_length"] or 0, -(c["bitrate"] or 0)))[:150]


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
                raise SourceError('Soulseek is busy. Wait for the current operation to finish, then retry; partial files are retained.') from None
            if path.startswith('/transfers/downloads/') and detail:
                username = unquote(path.split('/')[3])
                raise SourceError(_transfer_failure(username, {'state': 'Errored', 'message': detail})) from None
            raise SourceError(f'Soulseek returned HTTP {exc.response.status_code}' +
                              (f': {detail}.' if detail else '.') + ' Retry or choose another source.') from None
        except (httpx.HTTPError, ValueError):
            raise SourceError("Soulseek is unavailable; retry when the connection recovers.") from None

    def _youtube_options(self):
        return {"quiet": True, "no_warnings": True, "logger": _QuietLogger(), "socket_timeout": 20,
                "retries": 2, "extractor_retries": 2, "cachedir": False,
                "js_runtimes": {"node": {"path": self.config.get("node_path", "node")}},
                "allowed_extractors": ["youtube.*", "soundcloud.*", "bandcamp.*", "vimeo.*"]}

    def search(self, query, source="all", kind="track"):
        query = query.strip()
        if not query:
            raise SourceError("Enter a search or YouTube URL.")
        if source not in {"all", "soulseek", "youtube"} or kind not in {"track", "album"}:
            raise SourceError("Unsupported search source or kind.")
        if urlsplit(query).scheme or query.startswith(("www.", "youtube.com/", "youtu.be/")):
            youtube_url(query)
            if source == "soulseek":
                raise SourceError("Use the YouTube source for a YouTube URL.")
            self.last_errors = {}
            return self._search_youtube(query, kind)
        selected = {"soulseek": self._search_soulseek, "youtube": self._search_youtube}
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
        search_id = str(uuid.uuid4())
        seconds = float(self.config.get("slskd_search_seconds", 20))
        self._slskd("POST", "/searches", json={"id": search_id, "searchText": query,
                     "searchTimeout": int(seconds * 1000), "fileLimit": 1000, "responseLimit": 150,
                     "filterResponses": True, "maximumPeerQueueLength": 150,
                     "minimumPeerUploadSpeed": 0, "minimumResponseFileCount": 1})
        deadline = time.monotonic() + seconds
        data = {}
        try:
            while time.monotonic() < deadline:
                data = self._slskd("GET", f"/searches/{search_id}", params={"includeResponses": "true"})
                if _get(data, "isComplete", False) or "Completed" in str(_get(data, "state", "")):
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            if not _get(data, "responses"):
                data = self._slskd("GET", f"/searches/{search_id}/responses")
            return normalize_soulseek(data, kind)
        finally:
            try:
                self._slskd("DELETE", f"/searches/{search_id}")
            except SourceError:
                pass

    def _search_youtube(self, query, kind):
        direct = bool(urlsplit(query).scheme)
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

    def download(self, candidate, destination, progress, cancelled):
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if cancelled():
            raise DownloadCancelled("Download cancelled.")
        if candidate.get("source") == "youtube":
            return self._download_youtube(candidate, destination, progress, cancelled)
        if candidate.get("source") == "soulseek":
            return self._download_soulseek(candidate, destination, progress, cancelled)
        raise SourceError("Unsupported download source.")

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
        options = {**self._youtube_options(), "format": "bestaudio/best", "noplaylist": candidate.get("kind") != "album",
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
        except Exception:
            if cancelled():
                raise DownloadCancelled("Download cancelled. Retry to resume.") from None
            raise SourceError("yt-dlp download failed. Retry or choose another candidate.") from None
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
            if not files:
                raise SourceError("Could not confirm the full album folder. Try another uploader.")
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
        missing = [f for f in files if f["filename"] not in selected]
        if missing:
            response = self._slskd("POST", base, json=[{"filename": f["filename"], "size": f["size"]} for f in missing])
            enqueued = _array(_get(response or {}, "enqueued", [])) if isinstance(response, dict) else []
            previous_ids = {_get(t, "id") for t in transfers}
            owned_ids = set(candidate.get("owned_transfer_ids", []))
            owned_ids.update(_get(t, "id") for t in enqueued if _get(t, "id") and _get(t, "id") not in previous_ids)
            candidate["owned_transfer_ids"] = sorted(owned_ids)
            progress({"candidate": candidate, "message": "Downloads enqueued", "percent": 0})
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
        deadline = time.monotonic() + float(self.config.get("download_timeout_seconds", 3600))
        result = []
        states = []
        while time.monotonic() < deadline:
            if cancelled():
                self.cancel(candidate)
                raise DownloadCancelled("Download cancelled. Existing source files are kept.")
            transfers = self._transfers(username)
            done, transferred, total = 0, 0, sum(int(f["size"] or 0) for f in files)
            ids = []
            states = []
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
                elif _terminal(transfer):
                    raise SourceError(_transfer_failure(username, transfer))
            candidate["transfer_ids"] = ids
            waiting = any('queued' in state.casefold() and 'remotely' in state.casefold() for state in states)
            positions = [_get(t, 'placeInQueue') for t in transfers if _get(t, 'id') in ids and _get(t, 'placeInQueue') is not None]
            message = f'{done}/{len(files)} files complete. '
            if waiting:
                message += f'Waiting in {username} remote queue' + (f' (position {min(positions)})' if positions else '') + '; download starts when an upload slot opens.'
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
        if any('queued' in state.casefold() and 'remotely' in state.casefold() for state in states):
            raise SourceError(f'Still waiting in {username} remote queue when the job time limit was reached. The existing transfer and partial files are retained; retry continues waiting without adding another transfer, or choose another uploader.')
        raise SourceError(f'Download from {username} exceeded the job time limit. Existing transfers and partial files are retained; retry to continue or choose another uploader.')

    def cancel(self, candidate):
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
