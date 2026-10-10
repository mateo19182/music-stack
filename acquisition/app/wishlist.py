"""Requests: everything the owner asks for, and the one way music gets downloaded.

A request is an album, a track, or an exact link. For albums and tracks: search every source
at once, keep what is that record, sort by quality, try the best copy, and take the next one
when a download fails. A list that runs out is searched again a day later, for a week. A copy
picked from search results is tried first; if it fails, the request searches like any other.
A link is downloaded as given, once a day for a week until it works.

Albums and tracks that came in lossy are searched once a week for a better copy, for four
weeks; review replaces the lossy tracks. Downloads are jobs (app.main); a request follows one
job at a time. (The table is still called "wishlist".)
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import secrets
import time

import httpx

from . import matching
from .lookup import album_tracklist
from .sources import SourceError, SoulseekUnavailable, soulseek_query, youtube_url

log = logging.getLogger("acquisition")

DAY = 24 * 3600
SEARCH_AGAIN = DAY             # after a list runs out, or nothing was found
SEARCHES = 7                   # a week of daily searches, then give up
UPGRADE_EVERY = 7 * DAY        # a lossy album or track: look for a better copy this often
UPGRADE_FOR = 28 * DAY         # ...for this long after it arrived
LOSSLESS = 3                   # matching.quality of a lossless copy
YOUTUBE_TITLE_FLOOR = 0.2      # 2026-10-09: right albums scored 0.23 and 0.40, same-titled ones by others 0.03-0.18
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


def soulseek_queries(album, artist, year=None):
    """Artist + album; the album alone (the server drops some artist names); the artist alone
    (typos in the album name); album + year for short titles under a dropped artist name."""
    first = search_artist(artist)
    name = matching.re.sub(r"\s*[\(\[].*?[\)\]]", "", album).strip() or album
    ladder = [f"{first} {name}".strip(), name if len(matching.key(name)) >= 5 or len(name.split()) >= 2 else None,
              first if len(matching.key(first)) >= 3 else None,
              f"{name} {year}" if year and len(matching.key(name)) < 5 else None]
    # The app cleans queries the same way; skip rungs that would send an identical search.
    return list({soulseek_query(q): q for q in reversed([q for q in ladder if q]) if soulseek_query(q)}.values())[::-1]


def link_candidate(url, kind):
    url = youtube_url(url)
    return {"id": hashlib.sha256((kind + url).encode()).hexdigest(), "source": "youtube", "kind": kind,
            "url": url, "title": url, "files": []}


def name(item):
    return item.get("album") if item.get("kind", "album") == "album" else item.get("title") or item.get("url") or ""


class Wishlist:
    def __init__(self, store, config, sources, enqueue, soulseek_ready=lambda: True, clock=time.time, judge=None,
                 tracklist=None, youtube_ready=lambda: True, youtube_enabled=lambda: True):
        self.store, self.config, self.sources, self.enqueue = store, config, sources, enqueue
        self.judge = judge   # app.llm_match.Judge, or None: the matching rules alone
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
            for k in ("star", "position", "list_order", "playlist", "year"):
                if k in extra:
                    existing[k] = extra[k]
            if pick and not existing.get("job_id"):
                existing.update(candidates=[pick], pinned=True, status="wanted", note="Trying your pick")
            elif renew and existing["status"] in ("not_found", "gave_up", "skipped"):
                self.retry(existing)
                return existing
            self.save(existing)
            return existing
        item = {"id": id, "owner": owner, "kind": kind, "artist": artist, "album": album, "title": title, "url": url,
                "list": list_name, "status": "wanted", "note": "Waiting to search", "candidates": [], "tried": [],
                "rounds": 0, "searched_at": None, "next_search_at": 0, "job_id": None, "wrong_jobs": [],
                "added_at": self.clock(), **extra}
        if pick:
            item.update(candidates=[pick], pinned=True, note="Trying your pick")
        elif kind == "link":
            item["candidates"] = [link_candidate(url, extra.get("link_kind", "track"))]
        elif not artist or not (album if kind == "album" else title):
            item.update(status="skipped", note="Needs an artist and " + ("an album" if kind == "album" else "a title"))
        if item.get("skip"):
            item.update(status="skipped", note=str(item["skip"]))
        self.save(item)
        return item

    def retry(self, item):
        item.update(status="wanted", rounds=0, next_search_at=0, candidates=[], searched_at=None,
                    note="Retry requested; searching again")
        if item.get("kind") == "link":
            item.update(tried=[], candidates=[link_candidate(item["url"], item.get("link_kind", "track"))])
        self.save(item)

    # ---- the loop ------------------------------------------------------------------------
    def tick(self):
        items = [i for i in self.items() if i.get("status") != "skipped"]
        if not items:
            return
        library = self._library()
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
            if self._upgrading(item):
                self._next(item, jobs, upgrade=True)
            return
        if item["status"] in DONE:
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
        if quality is not None and quality < LOSSLESS and item["kind"] != "link":
            item.update(upgrade_until=self.clock() + UPGRADE_FOR, next_search_at=0, rounds=0,
                        note="In the library (lossy); looking for a better copy weekly")
        self.save(item)

    def _upgrading(self, item):
        """A lossy copy arrived less than UPGRADE_FOR ago: keep looking for a better copy."""
        if item.get("quality") is None or item["quality"] >= LOSSLESS or not item.get("upgrade_until"):
            return False
        if self.clock() < item["upgrade_until"]:
            return True
        item.update(upgrade_until=None, candidates=[], note="In the library (lossy; no better copy turned up in 4 weeks)")
        self.save(item)
        return False

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
            item.update(status="have", job_id=None, candidates=[], quality=quality, rounds=0, pinned=False)
            if quality is not None and quality < LOSSLESS and item["kind"] != "link":
                if not upgrade:
                    item.update(upgrade_until=self.clock() + UPGRADE_FOR, next_search_at=self.clock() + UPGRADE_EVERY)
                item["note"] = "Added to the library (lossy); looking for a better copy weekly"
            else:
                item.update(upgrade_until=None, note="Added to the library")
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
            return False   # the link again tomorrow, not now
        self.save(item)
        return True

    def _allowed(self, item, c):
        if not matching.is_youtube(c):
            return True
        return self.youtube_enabled() if item["kind"] == "link" else self.youtube_ready()

    def _next(self, item, jobs, upgrade=False):
        """Try the best untried copy; when the list runs out, search again later."""
        now = self.clock()
        tried = {t["provider"] for t in item["tried"]}
        untried = [c for c in item["candidates"] if (item["kind"] == "link" or matching.provider(c) not in tried)
                   and self._allowed(item, c)]
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
            self._exhausted(item, upgrade, "Your pick failed" if item.get("pinned") else "All copies failed")
            return
        if now < item.get("next_search_at", 0) or self._searches_left <= 0:
            return
        self._searches_left -= 1
        found = self._search(item)
        if found is None:
            return   # a search error, not "not found": try next tick
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

    def _exhausted(self, item, upgrade, why):
        now = self.clock()
        item["candidates"] = []
        if item.get("pinned") and item["kind"] != "link":
            # The copy picked from search results failed: search like any other request, now.
            item.update(pinned=False, next_search_at=0, status="wanted", note=f"{why}; searching other copies")
            self.save(item)
            return
        item["rounds"] = item.get("rounds", 0) + 1
        if upgrade:
            item.update(next_search_at=now + UPGRADE_EVERY, note=f"In the library (lossy); {why.lower()} yet, looking again in a week")
        elif item["rounds"] >= SEARCHES:
            gave = "gave_up" if item["tried"] else "not_found"
            item.update(status=gave, note=(f"Gave up after {len(item['tried'])} tries in a week" if item["tried"]
                                           else "Not found on Soulseek, RuTracker or YouTube Music in a week"))
        else:
            item.update(status="wanted", next_search_at=now + SEARCH_AGAIN,
                        note=f"{why}; {'trying' if item['kind'] == 'link' else 'searching'} again tomorrow")
        self.save(item)

    def _search(self, item):
        """Every source at once, then one ranked list. None when Soulseek could not be searched
        (an error is not "not found"); a tracker or YouTube error only leaves their copies out."""
        if item["kind"] == "link":
            return [link_candidate(item["url"], item.get("link_kind", "track"))]
        if not self.soulseek_ready():
            return None
        album, artist, year, title = item["album"], item["artist"], item.get("year"), item.get("title", "")
        avoid = {t["provider"] for t in item["tried"]}
        first = search_artist(artist)
        track = item["kind"] == "track"

        def soulseek():
            results = []
            queries = ([f"{first} {title}".strip(), title if len(matching.key(title)) >= 8 else None] if track
                       else soulseek_queries(album, artist, year))
            for query in filter(None, queries):
                found = self.sources().search(query, "soulseek", item["kind"])
                results += found   # the model may accept folders the rules would not: keep every step's results
                if (matching.ranked_tracks(found, artist, title, avoid) if track else matching.ranked(found, album, artist, avoid)):
                    break
            return results

        def torrent():
            if track or not self.sources().torrents_configured():
                return []
            plain = matching.re.sub(r"\s*[\(\[].*?[\)\]]", "", album).strip() or album
            results = []
            for query in dict.fromkeys(q for q in (f"{first} {plain}".strip(), first) if q):
                found = self.sources().search(query, "torrent", "album")
                results += found
                if any(c.get("source") == "torrent" for c in matching.ranked(found, album, artist, avoid)):
                    break
            return results

        def youtube():
            if not self.youtube_ready():
                return []
            # Requests take YouTube only at Premium quality (256 kbps), so that is what these copies are.
            if track:
                return [{**c, "premium_only": True} for c in self.sources().search(f"{first} {title}".strip(), "youtube", "track")]
            # The album title alone too: blog lists misspell artists ("Andrea" for Andrae Durden).
            results, seen = [], set()
            for query in dict.fromkeys((f"{first} {album}".strip(), album.strip())):
                found = self.sources().search(query, "youtube", "album")
                results += [{**c, "premium_only": True} for c in found if c.get("url") not in seen]
                seen |= {c.get("url") for c in found}
            return results

        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            futures = {n: pool.submit(fn) for n, fn in (("soulseek", soulseek), ("torrent", torrent), ("youtube", youtube))}
        results = []
        for source, future in futures.items():
            try:
                results += future.result()
            except SoulseekUnavailable:
                raise
            except SourceError as exc:
                if source == "soulseek":
                    return None
                log.info("Request %s: %s search failed: %s", item["id"], source, str(exc)[:200])
        if track:
            return matching.ranked_tracks(results, artist, title, avoid)
        return self._rank(item, results, avoid)

    def _rank(self, item, results, avoid):
        """The model decides which results are the album; code orders them by quality and source.
        Without a usable answer from the model, the matching rules decide, as before."""
        album, artist = item["album"], item["artist"]
        pool = [c for c in results if self._eligible(c, avoid)]
        # Results the rules recognise first, so a flood of unrelated ones cannot push them past what the model sees.
        pool.sort(key=lambda c: (not matching.identity(c, album, artist), -matching.quality(c)) + matching.availability(c))
        verdicts = None
        if self.judge and pool:
            if "tracklist" not in item:
                item["tracklist"] = self.tracklist(item) or None
            verdicts = self.judge.verdicts(artist, album, pool, item.get("year"), item.get("tracklist"))
        if verdicts is None:
            return matching.ranked(results, album, artist, avoid)
        shown = pool[:len(verdicts)]
        item["rejected"] = [{"source": c.get("source"), "name": c.get("directory") or c.get("title"),
                             "problem": verdicts[n]["problem"], "reason": verdicts[n]["reason"]}
                            for n, c in enumerate(shown) if not verdicts[n]["match"]][:8]
        best, seen = [], set()
        for n, c in enumerate(shown):
            who = matching.provider(c)
            if verdicts[n]["match"] and who not in seen:
                seen.add(who)
                best.append({**c, "judged": "model", "reason": verdicts[n]["reason"]})
        if not best:
            # Last resort: an official YouTube Music album with the requested title, word for word and
            # in order, that the model found at least plausible (a misspelled artist, "BLINDAO" under a
            # collaborator's channel). Same-titled records by other artists score under this floor.
            wanted = " ".join(matching.words(album))
            for n, c in enumerate(shown):
                p = verdicts[n].get("probability") or 0
                title = f' {" ".join(matching.words(c.get("title")))} '
                if c.get("source") == "youtube" and wanted and f" {wanted} " in title and p >= YOUTUBE_TITLE_FLOOR:
                    best.append({**c, "judged": "model", "reason": verdicts[n]["reason"] + "; exact title on YouTube Music"})
                    break
        return best[:8]

    @staticmethod
    def _eligible(c, avoid):
        """Facts, not judgement: an album-sized result of acceptable quality from a source we may still try."""
        if c.get("kind") != "album" or matching.quality(c) is None or matching.provider(c) in avoid:
            return False
        if c.get("source") == "soulseek":
            return not c.get("mixed_formats") and (c.get("file_count") or 0) >= 2
        if c.get("source") == "torrent":
            return (c.get("seeders") or 0) > 0
        return c.get("source") == "youtube" and bool(c.get("official"))

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
        if item["kind"] == "album":
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
                "note": item.get("note"), "quality": item.get("quality"), "upgrading": bool(item.get("upgrade_until")),
                "tries": len(item.get("tried", [])),
                "tried": [{k: t.get(k) for k in ("source", "provider", "outcome", "error", "at")} for t in item.get("tried", [])[-10:]],
                "rejected": item.get("rejected", [])[:5],
                "candidates": len(item.get("candidates", [])), "next": [
                    {k: c.get(k) for k in ("source", "username", "provider", "title", "format", "bitrate", "file_count", "seeders", "reason")}
                    for c in item.get("candidates", [])[:5]],
                "searched_at": item.get("searched_at"), "next_search_at": item.get("next_search_at") or None,
                "job": {"id": job["id"], "stage": job["stage"], "progress": job.get("progress"), "detail": job.get("detail"),
                        "source": (job.get("candidate") or {}).get("source")} if job else None}
