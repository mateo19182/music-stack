"""Requests: everything the owner asks for, and the one way music gets downloaded.

A request is an album, a track, or an exact link. For albums and tracks: the finder (app.finder)
searches every source and keeps what is that record, sorted by quality; try the best copy, and
take the next one when a download fails. One search: when nothing is found, or every copy failed, the request is
parked. A copy picked from search results is tried first; if it fails, the request searches like
any other. A link is downloaded as given, once.

Albums and tracks that came in lossy are parked too. Nothing parked is searched until the owner
searches all parked (search_parked): missing requests search again, lossy ones look for a
lossless copy, which review swaps in. Downloads are jobs (app.main); a request follows one job at
a time. (The table is still called "wishlist".)
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import re
import logging
import secrets
import time

import httpx

from . import matching
from .finder import MAX_ALBUM_FILES
from .lookup import album_tracklist
from .sources import SourceError, SoulseekUnavailable, youtube_url

log = logging.getLogger("acquisition")

DAY = 24 * 3600
LOSSLESS = 3                   # matching.quality of a lossless copy
MAX_TRIED = 16                 # copies tried before a request gives up, over its search and the one that follows
ACTIVE = ("queued", "downloading", "process_queued", "processing", "publish_queued", "publishing")
DONE = ("not_found", "gave_up", "skipped")
KINDS = ("album", "track", "link")


def item_id(owner, list_name, artist, album, kind="album", title="", url=""):
    if kind == "link":
        return hashlib.sha256(f"{owner}\0link\0{url}".encode()).hexdigest()[:24]
    name = matching.album_key(album) if kind == "album" else "track:" + matching.key(title)
    return hashlib.sha256(f"{owner}\0{list_name}\0{matching.key(artist)}\0{name}".encode()).hexdigest()[:24]


def search_artist(artist):
    """The artist to put in a search: none for a compilation, whose folders rarely say "Various Artists"."""
    first = matching.clean_artist(artist)
    return "" if matching.key(first) in {"variousartists", "various", "va", "vvaa"} else first


def avoided(item):
    """Uploaders not to ask again: every copy turned down, and those whose download failed since the
    last retry (a retry gives peers that were offline or busy another chance)."""
    since = item.get("retried_at") or 0
    return {t["provider"] for t in item.get("tried", []) if t.get("outcome") != "failed" or (t.get("at") or 0) >= since}


def tries(item):
    since = item.get("retried_at") or 0
    return sum(1 for t in item.get("tried", []) if (t.get("at") or 0) >= since)


def link_candidate(url, kind):
    url = youtube_url(url)
    return {"id": hashlib.sha256((kind + url).encode()).hexdigest(), "source": "youtube", "kind": kind,
            "url": url, "title": url, "files": []}


def lossy(item):
    """In the library below lossless: parked for the next lossless search."""
    return item.get("kind", "album") != "link" and item.get("quality") is not None and item["quality"] < LOSSLESS


def name(item):
    return item.get("album") if item.get("kind", "album") == "album" else item.get("title") or item.get("url") or ""


OTHER_RECORD = re.compile(r"\b(?:" + "|".join(matching.UNWANTED + ("mix", "vip", "dub")) + r")", re.I)


def _album_keys(artist, album):
    """"X (Remastered)" is album X too; "X (The Remixes)" is another record."""
    album = album or ""
    brackets = "".join(re.findall(r"[\(\[].*?[\)\]]", album))
    names = (album,) if OTHER_RECORD.search(brackets) else (album, re.sub(r"\s*[\(\[].*?[\)\]]", "", album))
    return {(artist, matching.album_key(a)) for a in names if matching.album_key(a)}


def covered_suggestions(items):
    """Ids of the suggestions already asked for another way: the same track requested in another
    list, or its album requested. They leave the picker while that request stands; a stopped one
    does not count."""
    tracks, albums = set(), set()
    ident = lambda i: matching.key(matching.clean_artist(i["artist"]))
    for i in items:
        if i["status"] in ("suggested", "skipped"):
            continue
        if i.get("kind") == "track":
            tracks.add((ident(i), matching.track_key(i.get("title"))))
        elif i.get("kind", "album") == "album":
            albums |= _album_keys(ident(i), i.get("album"))
    return {i["id"] for i in items if i["status"] == "suggested"
            and ((ident(i), matching.track_key(i.get("title"))) in tracks or _album_keys(ident(i), i.get("album")) & albums)}


class Wishlist:
    def __init__(self, store, config, sources, enqueue, soulseek_ready=lambda: True, clock=time.time, finder=None,
                 tracklist=None, youtube_ready=lambda: True, youtube_enabled=lambda: True):
        self.store, self.config, self.sources, self.enqueue = store, config, sources, enqueue
        self.finder = finder   # app.finder.Finder, or None: one plain search and the matching rules
        self.tracklist = tracklist or self._musicbrainz_tracklist
        # YouTube: links need it switched on; albums and tracks need the Premium login too,
        # since without it YouTube is ~130 kbps.
        self.soulseek_ready, self.youtube_ready, self.youtube_enabled = soulseek_ready, youtube_ready, youtube_enabled
        self.clock = clock
        self._playlists_at = 0.0
        self._searches_left = 0

    # ---- records -------------------------------------------------------------------------
    def items(self, owner=None):
        where, params = ("owner=?", (owner,)) if owner else ("1", ())
        return sorted(self.store.list("wishlist", where, params),
                      key=lambda i: (i.get("list_order", 0), not i.get("star"), i.get("position", 0)))

    def save(self, item):
        item["updated_at"] = self.clock()
        self.store.put("wishlist", item["id"], {k: v for k, v in item.items() if k not in ("id", "owner")}, owner=item["owner"])

    def add(self, owner, artist, album="", list_name="Requests", kind="album", title="", url="", pick=None, renew=False,
            **extra):
        """A new request, or the existing one for the same record. `pick` is a search result to
        try first; `url` (kind "link") is downloaded as given, `extra["link_kind"]` track or album.
        `renew`: asked for again, so a request that gave up starts over (a list import does not)."""
        artist, album, title, url = (artist or "").strip(), (album or "").strip(), (title or "").strip(), (url or "").strip()
        id = item_id(owner, list_name, artist, album, kind, title, url)
        existing = self.store.get("wishlist", id)
        if existing:
            for k in ("star", "position", "list_order", "playlist", "year", "hold"):
                if k in extra:
                    existing[k] = extra[k]
            if pick and not existing.get("job_id"):
                existing.update(candidates=[pick], pinned=True, pick=pick.get("id"), status="wanted", note="Trying your pick")
            elif renew and existing["status"] in ("not_found", "gave_up", "skipped"):
                self.retry(existing)
                return existing
            self.save(existing)
            return existing
        item = {"id": id, "owner": owner, "kind": kind, "artist": artist, "album": album, "title": title, "url": url,
                "list": list_name, "status": "wanted", "note": "Waiting to search", "candidates": [], "tried": [],
                "searched_at": None, "next_search_at": 0, "job_id": None, "wrong_jobs": [],
                "added_at": self.clock(), **extra}
        if pick:
            item.update(candidates=[pick], pinned=True, pick=pick.get("id"), note="Trying your pick")
        elif kind == "link":
            item["candidates"] = [link_candidate(url, extra.get("link_kind", "track"))]
        elif not artist or not (album if kind == "album" else title):
            item.update(status="skipped", note="Needs an artist and " + ("an album" if kind == "album" else "a title"))
        if item.get("skip"):
            item.update(status="skipped", note=str(item["skip"]))
        self.save(item)
        return item

    def retry(self, item):
        item.update(status="wanted", next_search_at=0, candidates=[], searched_at=None, searched_again=False,
                    retried_at=self.clock(), note="Retry requested; searching again")
        if item.get("kind") == "link":
            item.update(tried=[], candidates=[link_candidate(item["url"], item.get("link_kind", "track"))])
        self.save(item)

    def search_parked(self, owner, list_name=None):
        """Search everything parked once: missing requests from scratch, lossy ones for a lossless
        copy. Returns (missing, lossy) counts."""
        missing = upgrades = 0
        for item in self.items(owner):
            if item.get("job_id") or (list_name is not None and item["list"] != list_name):
                continue
            if item["status"] in ("not_found", "gave_up"):
                self.retry(item)
                missing += 1
            elif item["status"] == "have" and lossy(item) and not item.get("upgrade_wanted"):
                item.update(upgrade_wanted=True, candidates=[], retried_at=self.clock(),
                            note="In the library (lossy); looking for a lossless copy")
                self.save(item)
                upgrades += 1
        return missing, upgrades

    def choose(self, item, candidate_id):
        """The copy to get, from the candidates a held request found. None when it is not one of them."""
        chosen = next((c for c in item.get("candidates", []) if c.get("id") == candidate_id), None)
        if not chosen or item.get("job_id"):
            return None
        item.update(pick=candidate_id, pinned=True, status="wanted", note="Trying the copy you chose")
        self.save(item)
        return chosen

    # ---- the loop ------------------------------------------------------------------------
    def tick(self):
        everything = self.items()
        library = self._library() if everything else None
        if library is not None:
            self._settle_suggestions(everything, library)
        # Suggestions wait for the owner to choose them; they are never searched.
        items = [i for i in everything if i.get("status") not in ("skipped", "suggested")]
        if not items:
            return
        jobs = {j["id"]: j for j in self.store.list("jobs")}
        # A few searches per tick (each can take a minute): job updates for other requests stay prompt.
        self._searches_left = int(self.config.get("wishlist_searches_per_tick", 3))
        for item in items:
            try:
                self._advance(item, library, jobs, items)
            except SoulseekUnavailable:
                return   # slskd is down: try next tick
            except Exception:
                log.exception("Request %s failed to advance", item.get("id"))
        if self.clock() - self._playlists_at > 900:
            self._playlists_at = self.clock()
            try:
                self.sync_playlists(library)
            except Exception as exc:
                log.warning("Playlist sync failed: %s", str(exc)[:200])

    def _settle_suggestions(self, everything, library):
        """A saved track that reached the library some other way is had, no longer offered."""
        waiting = [i for i in everything if i.get("status") == "suggested" and i.get("kind") == "track"]
        for item in matching.tracks_in_library(library, waiting):
            item.update(status="have", note="In the library", candidates=[])
            self.save(item)

    def _advance(self, item, library, jobs, everything):
        item.setdefault("kind", "album")
        if item.get("job_id") and not self._follow_job(item, jobs.get(item["job_id"])):
            return   # still running, or waiting for review
        if item["status"] != "have" and library is not None and self._in_library(library, item):
            item.update(status="have", note="In the library", candidates=[])
            self.save(item)
        if item["status"] == "have":
            if item.get("quality") is None and not item.get("quality_checked"):
                self._backfill_quality(item, jobs, library)
            if item.get("upgrade_wanted"):
                self._next(item, jobs, upgrade=True)
            return
        if item["status"] in DONE or item["status"] == "choice":
            return
        if item["kind"] == "album" and matching.superseded(item, [o for o in everything if o["owner"] == item["owner"]
                                                                  and o.get("kind", "album") == "album"]):
            item.update(status="skipped", note="Covered by an expanded edition in the same list")
            self.save(item)
            return
        self._next(item, jobs)

    @staticmethod
    def _in_library(library, item):
        if item["kind"] == "album":
            return matching.library_has(library, item)
        if item["kind"] == "track":
            return matching.library_has_track(library, item)
        return False   # a link: only its own download counts

    def _backfill_quality(self, item, jobs, library):
        """Albums that arrived before quality was recorded: read it from the job that published them,
        or from the library's files for albums that were there before they were requested."""
        published = [j for j in jobs.values() if j["stage"] == "published"
                     and (j.get("candidate") or {}).get("wishlist_id") == item["id"] and j["id"] not in item.get("wrong_jobs", [])]
        quality = matching.quality(max(published, key=lambda j: j.get("created_at") or "")["candidate"]) if published else None
        if quality is None and item["kind"] == "album" and library is not None:
            quality = matching.library_quality(library, item)
        item.update(quality=quality, quality_checked=quality is not None or library is not None)
        if lossy(item):
            item["note"] = "In the library (lossy); parked for a lossless search"
        self.save(item)

    def _follow_job(self, item, job):
        """Update the request from its job. True when it needs its next copy."""
        upgrade = item["status"] == "have"
        if not job:
            item.update(job_id=None)
            return True
        stage = job["stage"]
        if stage in ACTIVE + ("review",):
            if upgrade:
                note = "Better copy waiting for review" if stage == "review" else f"Getting a better copy: {job.get('detail') or stage}"
                if item.get("note") != note:
                    item.update(note=note)
                    self.save(item)
                return False
            status = ("review" if stage == "review" else "downloading" if stage == "downloading"
                      else "queued" if stage == "queued" else "processing")
            note = "Waiting for your review" if stage == "review" else job.get("detail") or stage
            if item["status"] != status or item.get("note") != note:
                item.update(status=status, note=note)
                self.save(item)
            return False
        if stage == "published" and job["id"] not in item.get("wrong_jobs", []):
            quality = matching.quality(job.get("candidate") or {})
            item.update(status="have", job_id=None, candidates=[], quality=quality, pinned=False, upgrade_wanted=False)
            item["note"] = "Added to the library (lossy); parked for a lossless search" if lossy(item) else "Added to the library"
            self.save(item)
            return False
        if stage == "rejected" and job.get("already_had") and not upgrade:
            # Every track of the copy was already in the library: the request is had, not failed.
            item.update(status="have", job_id=None, candidates=[], pinned=False, note="Already in the library")
            self.save(item)
            return False
        outcome = {"published": "wrong record", "rejected": "rejected in review", "cancelled": "cancelled"}.get(stage, "failed")
        error = job.get("error") or ""
        candidate = job.get("candidate") or {}
        item["tried"].append({"provider": matching.provider(candidate), "source": candidate.get("source"),
                              "job": job["id"], "outcome": outcome, "error": error[:300], "at": self.clock()})
        item["job_id"] = None
        if not upgrade:
            item.update(status="wanted", note=f"{outcome.capitalize()}: {error[:160]}" if error else outcome.capitalize())
        if item["kind"] == "link":
            self._exhausted(item, False, f"Download failed: {error[:120]}" if error else "Download failed")
            return False
        self.save(item)
        return True

    def start_now(self, item):
        """A quick download: the picked copy starts now, in its own lane, instead of at the next pass.
        Returns the job id, or None when that copy cannot start (YouTube paused)."""
        if not item.get("job_id") and item.get("candidates"):
            if not self._allowed(item, item["candidates"][0]):
                return None
            self._queue(item, {**item["candidates"][0], "quick": True}, {})
        return item.get("job_id")

    def _allowed(self, item, c):
        if not matching.is_youtube(c):
            return True
        return self.youtube_enabled() if item["kind"] == "link" else self.youtube_ready()

    def _next(self, item, jobs, upgrade=False):
        """Try the best untried copy; when the list runs out, the request is missing."""
        now = self.clock()
        tried = avoided(item)
        untried = [c for c in item["candidates"] if (item["kind"] == "link" or matching.provider(c) not in tried)
                   and self._allowed(item, c)]
        if upgrade:
            untried = [c for c in untried if not c.get("other_artist")]
        # A YouTube album under another artist's name ("Homework" by Mato for Daft Punk) waits for a choice.
        if untried and (item.get("hold") or untried[0].get("other_artist") or untried[0].get("substitute")) and not upgrade and item["kind"] != "link":
            chosen = [c for c in untried if c.get("id") == item.get("pick")]
            if not chosen:
                self._hold(item, untried)
                return
            untried = chosen
        if untried:
            if self._room(jobs):
                self._queue(item, untried[0], jobs, upgrade)
            elif not upgrade and item.get("note") != "Waiting for a download slot":
                item.update(note="Waiting for a download slot")
                self.save(item)
            return
        if item["candidates"] and (item["kind"] != "link" or not self._allowed(item, item["candidates"][0])):
            if item["kind"] == "link":
                if item.get("note") != "Waiting: YouTube is paused":
                    item.update(note="Waiting: YouTube is paused")
                    self.save(item)
                return
            if upgrade or item.get("pinned") or item.get("searched_again") or tries(item) >= MAX_TRIED:
                self._exhausted(item, upgrade, "Your pick failed" if item.get("pinned") else "All copies failed")
                return
            # The listed copies all failed: one more search, without the uploaders already tried.
            item.update(candidates=[], searched_again=True)
        if self._searches_left <= 0:
            return
        self._searches_left -= 1
        found = self._search(item)
        if found is None:
            return   # a search error, not "not found": try next tick
        item["search_errors"] = 0   # saved with what the search found
        if upgrade:
            found = [c for c in found if (matching.quality(c) or 0) > (item.get("quality") or 0)]
        item.update(candidates=found, searched_at=now)
        if not found:
            self._exhausted(item, upgrade, "No better copy found" if upgrade else "Not found")
            return
        if not upgrade:
            item.update(status="wanted", note=f"Found {len(found)} cop{'y' if len(found) == 1 else 'ies'}")
        self.save(item)
        self._next(item, jobs, upgrade)

    def _hold(self, item, untried):
        """Found copies wait for a choice (the agent's, or the owner's in the web app)."""
        last = item["tried"][-1] if item.get("tried") else None
        failed = f"The chosen copy failed ({last.get('error') or last.get('outcome')}); " if last and item.get("pick") else ""
        note = f"{failed}{len(untried)} cop{'y' if len(untried) == 1 else 'ies'} found; waiting for a choice"
        if item["status"] != "choice" or item.get("note") != note:
            item.update(status="choice", pick=None, note=note)
            self.save(item)

    def _exhausted(self, item, upgrade, why):
        item["candidates"] = []
        if item.get("pinned") and item["kind"] != "link":
            # The copy picked from search results failed: search like any other request, now.
            item.update(pinned=False, next_search_at=0, status="wanted", note=f"{why}; searching other copies")
            self.save(item)
            return
        if upgrade:
            item.update(upgrade_wanted=False, note=f"In the library (lossy); {why.lower()}; parked")
        elif item["tried"]:
            why = "No other copies found" if why == "Not found" else why
            item.update(status="gave_up", note=f"{why} ({len(item['tried'])} tried)")
        else:
            item.update(status="not_found", note="Not found on Soulseek, RuTracker or YouTube Music" if self.youtube_ready()
                        else "Not found on Soulseek or RuTracker; YouTube was not searched (Premium login expired or YouTube paused)")
        self.save(item)

    def _search(self, item):
        """The finder (the model searches and picks), or without it one plain search per source and
        the matching rules. None when Soulseek could not be searched (an error is not "not found");
        a tracker or YouTube error only leaves their copies out."""
        if item["kind"] == "link":
            return [link_candidate(item["url"], item.get("link_kind", "track"))]
        if not self.soulseek_ready():
            return None
        avoid = avoided(item)
        if item.get("album") and "tracklist" not in item:
            item["tracklist"] = self.tracklist(item) or None
        reference = item.get("tracklist")
        if item["kind"] == "track" and reference and not any(
                matching.track_key(t) == matching.track_key(item["title"]) for t, _ in reference.get("tracks") or []):
            reference = None   # the song is not on that album: a hint that would only mislead
        found = None
        if self.finder:
            try:
                found = self.finder.find(item, self.sources(), avoid, self.youtube_ready(), reference)
            except SoulseekUnavailable:
                raise
            except SourceError as exc:
                return self._search_failed(item, exc)
        if found is None:
            found = self._rules_search(item, avoid)
        return None if found is None else [self._other_artist(c, item["artist"]) for c in found]

    def _search_failed(self, item, exc, limit=3):
        """A Soulseek error is retried next tick, but not for ever: some words make the server close
        the connection on every search (2026-11-09, twenty minutes of "the who")."""
        log.info("Request %s: Soulseek search failed: %s", item["id"], str(exc)[:200])
        item["search_errors"] = item.get("search_errors", 0) + 1
        if item["search_errors"] >= limit and not item.get("upgrade_wanted"):
            item.update(status="not_found", candidates=[], search_errors=0,
                        note=f"Soulseek failed on {limit} searches in a row ({str(exc)[:120]}); parked")
        self.save(item)
        return None

    def _rules_search(self, item, avoid):
        """Without the model: artist and title on every source at once, ranked by app.matching."""
        track = item["kind"] == "track"
        name = matching.unmixed(item["title"]) if track else item["album"]
        plain = re.sub(r"\s*[\(\[].*?[\)\]]", "", name).strip() or name
        query = f"{search_artist(item['artist'])} {plain}".strip()
        runs = {"soulseek": item["kind"]} | ({} if track else {"torrent": "album"}) | ({"youtube": item["kind"]} if self.youtube_ready() else {})
        with concurrent.futures.ThreadPoolExecutor(len(runs)) as pool:
            futures = {source: pool.submit(self.sources().search, query, source, kind) for source, kind in runs.items()}
        results = []
        for source, future in futures.items():
            try:
                found = future.result()
            except SoulseekUnavailable:
                raise
            except SourceError as exc:
                if source == "soulseek":
                    return self._search_failed(item, exc)
                log.info("Request %s: %s search failed: %s", item["id"], source, str(exc)[:200])
                continue
            # Requests take YouTube only at Premium quality (256 kbps), so that is what these copies are.
            results += [{**c, "premium_only": True} for c in found] if source == "youtube" else found
        if track:
            return matching.ranked_tracks(results, item["artist"], name, avoid, album=item.get("album") or "")
        return matching.ranked(results, item["album"], item["artist"], avoid)

    @staticmethod
    def _other_artist(c, artist):
        """A YouTube album credited to someone else than the requested artist (typos allowed) is marked:
        same-titled records by others pass the model too often (2026-10-16: Viper's "The Money Store")."""
        if c.get("source") != "youtube":
            return c
        wanted = matching.key(matching.clean_artist(artist))
        credited = {matching.key(matching.clean_artist(re.sub(r"\s*-\s*Topic$", "", str(c.get(f) or ""))))
                    for f in ("artist", "uploader")} - {""}
        if not wanted or matching.artist_matches(wanted, credited):
            return c
        name = c.get("artist") or re.sub(r"\s*-\s*Topic$", "", str(c.get("uploader") or ""))
        return {**c, "other_artist": True, "reason": f"{c.get('reason', '')}; credited to {name}, not {artist}".lstrip("; ")}

    def _musicbrainz_tracklist(self, item):
        try:
            with httpx.Client() as client:
                return album_tracklist(client, matching.clean_artist(item["artist"]), item["album"])
        except Exception:
            return None   # evidence only: the model judges without it

    def _room(self, jobs):
        """Queue another download only while fewer are waiting than there are download workers:
        a long waiting line goes stale (uploaders go offline) and downloads stay few."""
        waiting = sum(1 for j in jobs.values() if j["stage"] == "queued")
        return waiting < int(self.config.get("download_workers", 4))

    def _queue(self, item, candidate, jobs, upgrade=False):
        candidate = {k: v for k, v in candidate.items() if k not in ("score", "typical", "owner")}
        candidate.update(requested_artist=matching.clean_artist(item["artist"]) or None, wishlist_id=item["id"],
                         # Albums and tracks from YouTube: Premium quality or nothing. A link is taken as given.
                         premium_only=item["kind"] != "link")
        if item["kind"] == "album" and not candidate.get("requested_title"):   # a single's song keeps its title instead
            candidate["requested_album"] = item["album"]
        elif item["kind"] == "track":
            candidate["requested_title"] = item["title"]
        try:
            job = self.enqueue(candidate, {"username": item["owner"]})
        except Exception as exc:   # the app's queue is full: wait
            log.info("Request %s could not be queued: %s", item["id"], getattr(exc, "detail", exc))
            return
        # Count it now, so the next request in this tick sees the room it took.
        jobs[job["id"]] = {"id": job["id"], "owner": item["owner"], "stage": "queued", "candidate": candidate}
        source = candidate.get("username") or candidate.get("title")
        item["job_id"] = job["id"]
        if upgrade:
            item["note"] = f"Getting a better copy from {candidate['source']}: {source}"
        else:
            item.update(status="queued", note=f"Queued from {candidate['source']}: {source}")
        self.save(item)

    def _library(self):
        try:
            return matching.in_library(self.config.get("navidrome_db", "/navidrome/navidrome.db"))
        except Exception as exc:
            log.warning("Wishlist cannot read the Navidrome library: %s", str(exc)[:200])
            return None

    # ---- playlists ---------------------------------------------------------------------------
    def sync_playlists(self, library):
        """One Navidrome playlist per list marked `playlist`, albums in list order."""
        user, password = self.config.get("navidrome_playlist_username"), self.config.get("navidrome_playlist_password")
        if not user or not password or library is None:
            return {}
        lists = {}
        for item in self.items():
            if item.get("playlist") and item.get("status") != "skipped" and item.get("kind", "album") == "album":
                lists.setdefault(item["list"], []).append(item)
        if not lists:
            return {}
        base = self.config.get("navidrome_url", "http://navidrome:4533").rstrip("/") + "/rest/"

        def call(endpoint, params):
            salt = secrets.token_hex(8)
            auth = [("u", user), ("t", hashlib.md5((password + salt).encode()).hexdigest()), ("s", salt),
                    ("v", "1.16.1"), ("c", "acquisition"), ("f", "json")]
            r = httpx.get(base + endpoint, params=auth + params, timeout=60)
            r.raise_for_status()
            data = r.json()["subsonic-response"]
            if data.get("status") != "ok":
                raise RuntimeError(data.get("error", {}).get("message", "Subsonic error"))
            return data

        existing = {p["name"]: p["id"] for p in call("getPlaylists.view", []).get("playlists", {}).get("playlist", [])}
        counts = {}
        for name, items in lists.items():
            songs = list(dict.fromkeys(t for i in sorted(items, key=lambda i: i.get("position", 0))
                                       for t in matching.album_tracks(library, i)))
            params = [("songId", s) for s in songs]
            if name in existing:
                call("createPlaylist.view", [("playlistId", existing[name])] + params)   # replaces the songs
            else:
                call("createPlaylist.view", [("name", name)] + params)
            counts[name] = len(songs)
        return counts

    # ---- views -----------------------------------------------------------------------------
    def view(self, item, jobs=None):
        job = (jobs or {}).get(item.get("job_id")) if item.get("job_id") else None
        return {"id": item["id"], "kind": item.get("kind", "album"), "artist": item["artist"], "name": name(item),
                "album": item.get("album"), "title": item.get("title"), "url": item.get("url"), "list": item["list"],
                "position": item.get("position", 0), "star": bool(item.get("star")), "status": item["status"],
                "hold": bool(item.get("hold")),
                "note": item.get("note"), "quality": item.get("quality"), "upgrading": bool(item.get("upgrade_wanted")), "lossy": lossy(item),
                "tries": len(item.get("tried", [])),
                "tried": [{k: t.get(k) for k in ("source", "provider", "outcome", "error", "at")} for t in item.get("tried", [])[-10:]],
                "rejected": item.get("rejected", [])[:5],
                "candidates": len(item.get("candidates", [])), "next": [
                    {k: c.get(k) for k in ("id", "source", "username", "provider", "title", "format", "bitrate", "file_count", "seeders", "reason")}
                    for c in item.get("candidates", [])[:5]],
                "searched_at": item.get("searched_at"), "next_search_at": item.get("next_search_at") or None,
                "job": {"id": job["id"], "stage": job["stage"], "progress": job.get("progress"), "detail": job.get("detail"),
                        "source": (job.get("candidate") or {}).get("source")} if job else None}

    def detail(self, item, jobs=None):
        """One request with every copy it found, for whoever chooses: the search model's reasons, its searches,
        the files in each copy, what was tried and what the model turned down."""
        tried = avoided(item)
        copies = []
        for c in item.get("candidates", []):
            files = [{"name": str(f.get("filename") or "").replace("\\", "/").rsplit("/", 1)[-1], "size": f.get("size"),
                      "duration": f.get("duration"), "format": f.get("format"), "bitrate": f.get("bitrate")}
                     for f in (c.get("files") or [])[:MAX_ALBUM_FILES]]
            copies.append({"id": c.get("id"), "source": c.get("source"), "username": c.get("username"),
                           "provider": matching.provider(c), "title": c.get("title"), "folder": c.get("directory"),
                           "url": c.get("url"), "format": c.get("format"), "bitrate": c.get("bitrate"),
                           "duration": c.get("duration"), "size": c.get("size"), "file_count": c.get("file_count") or len(files),
                           "files": files, "seeders": c.get("seeders"), "queue_length": c.get("queue_length"),
                           "free_slots": c.get("free_slots"), "reason": c.get("reason"), "judged": c.get("judged"),
                           "substitute": bool(c.get("substitute")),
                           "tried": matching.provider(c) in tried, "chosen": bool(item.get("pick")) and c.get("id") == item.get("pick")})
        return {**self.view(item, jobs), "copies": copies, "tracklist": item.get("tracklist"),
                "searches": item.get("searches", []), "search_note": item.get("search_note"),
                "year": item.get("year"), "added_at": item.get("added_at")}
