"""Albums the owner wants. When one is added: search every source at once, sort what is the
album by quality, try the best copy, and take the next one when a download fails.

A list that runs out is searched again a day later, for a week. An album that came in lossy
is searched once a week for a lossless copy, for four weeks; review replaces the lossy tracks.
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
from .sources import SourceError, SoulseekUnavailable, soulseek_query

log = logging.getLogger("acquisition")

DAY = 24 * 3600
SEARCH_AGAIN = DAY             # after a list runs out, or nothing was found
SEARCHES = 7                   # a week of daily searches, then give up
UPGRADE_EVERY = 7 * DAY        # an album that came in lossy: look for lossless this often
UPGRADE_FOR = 28 * DAY         # ...for this long after it arrived
LOSSLESS = 3                   # matching.quality of a lossless copy
YOUTUBE_TITLE_FLOOR = 0.2      # 2026-10-09: right albums scored 0.23 and 0.40, same-titled ones by others 0.03-0.18
ACTIVE = ("queued", "downloading", "process_queued", "processing", "publish_queued", "publishing")
DONE = ("not_found", "gave_up", "skipped")


def item_id(owner, list_name, artist, album):
    return hashlib.sha256(f"{owner}\0{list_name}\0{matching.key(artist)}\0{matching.album_key(album)}".encode()).hexdigest()[:24]


def soulseek_queries(album, artist, year=None):
    """Artist + album; the album alone (the server drops some artist names); the artist alone
    (typos in the album name); album + year for short titles under a dropped artist name."""
    first = matching.clean_artist(artist)
    name = matching.re.sub(r"\s*[\(\[].*?[\)\]]", "", album).strip() or album
    ladder = [f"{first} {name}".strip(), name if len(matching.key(name)) >= 5 or len(name.split()) >= 2 else None,
              first if len(matching.key(first)) >= 3 else None,
              f"{name} {year}" if year and len(matching.key(name)) < 5 else None]
    # The app cleans queries the same way; skip rungs that would send an identical search.
    return list({soulseek_query(q): q for q in reversed([q for q in ladder if q]) if soulseek_query(q)}.values())[::-1]


class Wishlist:
    def __init__(self, store, config, sources, enqueue, soulseek_ready=lambda: True, clock=time.time, judge=None,
                 tracklist=None):
        self.store, self.config, self.sources, self.enqueue = store, config, sources, enqueue
        self.judge = judge   # app.llm_match.Judge, or None: the matching rules alone
        self.tracklist = tracklist or self._musicbrainz_tracklist
        self.soulseek_ready, self.clock = soulseek_ready, clock
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

    def add(self, owner, artist, album, list_name="Wishlist", **extra):
        id = item_id(owner, list_name, artist, album)
        existing = self.store.get("wishlist", id)
        if existing:
            for k in ("star", "position", "list_order", "playlist", "year"):
                if k in extra:
                    existing[k] = extra[k]
            self.save(existing)
            return existing
        item = {"id": id, "owner": owner, "artist": artist or "", "album": album or "", "list": list_name,
                "status": "wanted", "note": "Waiting to search", "candidates": [], "tried": [], "rounds": 0,
                "searched_at": None, "next_search_at": 0, "job_id": None, "wrong_jobs": [],
                "added_at": self.clock(), **extra}
        if not item["artist"] or not item["album"]:
            item.update(status="skipped", note="Not an album (needs an artist and an album)")
        elif item.get("skip"):
            item.update(status="skipped", note=str(item["skip"]))
        self.save(item)
        return item

    def retry(self, item):
        item.update(status="wanted", rounds=0, next_search_at=0, candidates=[], searched_at=None,
                    note="Retry requested; searching again")
        self.save(item)

    # ---- the loop ------------------------------------------------------------------------
    def tick(self):
        items = [i for i in self.items() if i.get("status") != "skipped"]
        if not items:
            return
        library = self._library()
        jobs = {j["id"]: j for j in self.store.list("jobs")}
        # A few searches per tick (each can take a minute): job updates for other albums stay prompt.
        self._searches_left = int(self.config.get("wishlist_searches_per_tick", 3))
        for item in items:
            try:
                self._advance(item, library, jobs, items)
            except SoulseekUnavailable:
                return   # the whole network is down: the health watchdog pauses downloads; try next tick
            except Exception:
                log.exception("Wishlist item %s failed to advance", item.get("id"))
        if self.clock() - self._playlists_at > 900:
            self._playlists_at = self.clock()
            try:
                self.sync_playlists(library)
            except Exception as exc:
                log.warning("Wishlist playlist sync failed: %s", str(exc)[:200])

    def _advance(self, item, library, jobs, everything):
        if item.get("job_id") and not self._follow_job(item, jobs.get(item["job_id"])):
            return   # still running, or waiting for review
        if item["status"] != "have" and library is not None and matching.library_has(library, item):
            item.update(status="have", note="In the library", candidates=[])
            self.save(item)
        if item["status"] == "have":
            if "quality" not in item:
                self._backfill_quality(item, jobs)
            if self._upgrading(item):
                self._next(item, jobs, upgrade=True)
            return
        if item["status"] in DONE:
            return
        if matching.superseded(item, [o for o in everything if o["owner"] == item["owner"]]):
            item.update(status="skipped", note="Covered by an expanded edition in the same list")
            self.save(item)
            return
        self._next(item, jobs)

    def _backfill_quality(self, item, jobs):
        """Albums that arrived before quality was recorded: read it from the job that published them."""
        published = [j for j in jobs.values() if j["stage"] == "published"
                     and (j.get("candidate") or {}).get("wishlist_id") == item["id"] and j["id"] not in item.get("wrong_jobs", [])]
        quality = matching.quality(max(published, key=lambda j: j.get("created_at") or "")["candidate"]) if published else None
        item["quality"] = quality
        if quality is not None and quality < LOSSLESS:
            item.update(upgrade_until=self.clock() + UPGRADE_FOR, next_search_at=0, rounds=0,
                        note="In the library (lossy); looking for lossless weekly")
        self.save(item)

    def _upgrading(self, item):
        """A lossy copy arrived through the Wishlist less than UPGRADE_FOR ago: keep looking for lossless."""
        if item.get("quality") is None or item["quality"] >= LOSSLESS or not item.get("upgrade_until"):
            return False
        if self.clock() < item["upgrade_until"]:
            return True
        item.update(upgrade_until=None, candidates=[], note="In the library (lossy; no lossless copy turned up in 4 weeks)")
        self.save(item)
        return False

    def _follow_job(self, item, job):
        """Update the item from its job. True when the item needs its next candidate."""
        upgrade = item["status"] == "have"
        if not job:
            item.update(job_id=None)
            return True
        stage = job["stage"]
        if stage in ACTIVE + ("review",):
            if upgrade:
                note = "Lossless copy waiting for review" if stage == "review" else f"Getting a lossless copy: {job.get('detail') or stage}"
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
            item.update(status="have", job_id=None, candidates=[], quality=quality, rounds=0)
            if quality is not None and quality < LOSSLESS:
                if not upgrade:
                    item.update(upgrade_until=self.clock() + UPGRADE_FOR, next_search_at=self.clock() + UPGRADE_EVERY)
                item["note"] = "Added to the library (lossy); looking for lossless weekly"
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
        self.save(item)
        return True

    def _next(self, item, jobs, upgrade=False):
        """Try the best untried copy; when the list runs out, search again later."""
        now = self.clock()
        tried = {t["provider"] for t in item["tried"]}
        untried = [c for c in item["candidates"] if matching.provider(c) not in tried]
        if untried:
            if self._room(jobs):
                self._queue(item, untried[0], jobs, upgrade)
            elif not upgrade and item.get("note") != "Waiting for a download slot":
                item.update(note="Waiting for a download slot")
                self.save(item)
            return
        if item["candidates"]:
            # Every copy from the last search failed.
            self._exhausted(item, upgrade, "All copies failed")
            return
        if now < item.get("next_search_at", 0) or self._searches_left <= 0:
            return
        self._searches_left -= 1
        found = self._search(item)
        if found is None:
            return   # a search error, not "not found": try next tick
        if upgrade:
            found = [c for c in found if matching.quality(c) == LOSSLESS]
        item.update(candidates=found, searched_at=now)
        if not found:
            self._exhausted(item, upgrade, "No lossless copy found" if upgrade else "Not found")
            return
        if not upgrade:
            item.update(status="wanted", note=f"Found {len(found)} copies")
        self.save(item)
        self._next(item, jobs, upgrade)

    def _exhausted(self, item, upgrade, why):
        now = self.clock()
        item.update(candidates=[], rounds=item.get("rounds", 0) + 1)
        if upgrade:
            item.update(next_search_at=now + UPGRADE_EVERY, note=f"In the library (lossy); {why.lower()} yet, looking again in a week")
        elif item["rounds"] >= SEARCHES:
            gave = "gave_up" if item["tried"] else "not_found"
            item.update(status=gave, note=(f"Gave up after {len(item['tried'])} tries in a week" if item["tried"]
                                           else "Not found on Soulseek, RuTracker or YouTube Music in a week"))
        else:
            item.update(status="wanted", next_search_at=now + SEARCH_AGAIN, note=f"{why}; searching again tomorrow")
        self.save(item)

    def _search(self, item):
        """Every source at once, then one ranked list. None when Soulseek could not be searched
        (an error is not "not found"); a tracker or YouTube error only leaves their copies out."""
        if not self.soulseek_ready():
            return None
        album, artist, year = item["album"], item["artist"], item.get("year")
        avoid = {t["provider"] for t in item["tried"]}
        first = matching.clean_artist(artist)

        def soulseek():
            results = []
            for query in soulseek_queries(album, artist, year):
                found = self.sources().search(query, "soulseek", "album")
                results += found   # the model may accept folders the rules would not: keep every step's results
                if matching.ranked(found, album, artist, avoid):
                    break
            return results

        def torrent():
            if not self.sources().torrents_configured():
                return []
            name = matching.re.sub(r"\s*[\(\[].*?[\)\]]", "", album).strip() or album
            results = []
            for query in dict.fromkeys(q for q in (f"{first} {name}".strip(), first) if q):
                found = self.sources().search(query, "torrent", "album")
                results += found
                if any(c.get("source") == "torrent" for c in matching.ranked(found, album, artist, avoid)):
                    break
            return results

        def youtube():
            # The album title alone too: blog lists misspell artists ("Andrea" for Andrae Durden).
            results, seen = [], set()
            for query in dict.fromkeys((f"{first} {album}".strip(), album.strip())):
                found = self.sources().search(query, "youtube", "album")
                results += [c for c in found if c.get("url") not in seen]
                seen |= {c.get("url") for c in found}
            return results

        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            futures = {name: pool.submit(fn) for name, fn in (("soulseek", soulseek), ("torrent", torrent), ("youtube", youtube))}
        results = []
        for name, future in futures.items():
            try:
                results += future.result()
            except SoulseekUnavailable:
                raise
            except SourceError as exc:
                if name == "soulseek":
                    return None
                log.info("Wishlist: %s search failed for %s: %s", name, item["id"], str(exc)[:200])
        return self._rank(item, results, avoid)

    def _rank(self, item, results, avoid):
        """The model decides which results are the album; code orders them by quality and source.
        Without a usable answer from the model, the matching rules decide, as before."""
        album, artist = item["album"], item["artist"]
        pool = [c for c in results if self._eligible(c, avoid)]
        pool.sort(key=lambda c: (-matching.quality(c),) + matching.availability(c))
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
        """Queue another download only while every download worker is busy at most: a long
        waiting line goes stale (uploaders go offline) before its turn comes."""
        waiting = sum(1 for j in jobs.values() if j["stage"] == "queued")
        return waiting < int(self.config.get("download_workers", 4))

    def _queue(self, item, candidate, jobs, upgrade=False):
        candidate = {k: v for k, v in candidate.items() if k not in ("score", "typical", "owner")}
        candidate.update(requested_artist=matching.clean_artist(item["artist"]) or None, requested_album=item["album"],
                         wishlist_id=item["id"])
        try:
            job = self.enqueue(candidate, {"username": item["owner"]})
        except Exception as exc:   # the app's queue is full: wait
            log.info("Wishlist could not queue %s: %s", item["id"], getattr(exc, "detail", exc))
            return
        # Count it now, so the next item in this tick sees the room it took.
        jobs[job["id"]] = {"id": job["id"], "owner": item["owner"], "stage": "queued", "candidate": candidate}
        source = candidate.get("username") or candidate.get("title")
        item["job_id"] = job["id"]
        if upgrade:
            item["note"] = f"Getting a lossless copy from {candidate['source']}: {source}"
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
            if item.get("playlist") and item.get("status") != "skipped":
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
        return {"id": item["id"], "artist": item["artist"], "album": item["album"], "list": item["list"],
                "position": item.get("position", 0), "star": bool(item.get("star")), "status": item["status"],
                "note": item.get("note"), "tries": len(item.get("tried", [])),
                "tried": [{k: t.get(k) for k in ("source", "provider", "outcome", "error", "at")} for t in item.get("tried", [])[-10:]],
                "rejected": item.get("rejected", [])[:5],
                "candidates": len(item.get("candidates", [])), "next": [
                    {k: c.get(k) for k in ("source", "username", "provider", "title", "format", "bitrate", "file_count", "seeders", "reason")}
                    for c in item.get("candidates", [])[:5]],
                "searched_at": item.get("searched_at"), "next_search_at": item.get("next_search_at") or None,
                "job": {"id": job["id"], "stage": job["stage"], "progress": job.get("progress"), "detail": job.get("detail")} if job else None}
