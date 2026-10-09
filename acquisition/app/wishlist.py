"""Albums the owner wants: search once, try the ranked candidates one by one, stop when it's in the library.

One rule replaces the blog batch's counters: when a download fails, take the next candidate
from the list the last search produced. Search again only when the list is used up or older
than FRESH (48 h). Two rounds without a successful download give up; an empty search waits a day.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time

import httpx

from . import matching
from .lookup import album_tracklist
from .sources import SourceError, SoulseekUnavailable, soulseek_query

log = logging.getLogger("acquisition")

FRESH = 12 * 3600            # search results older than this are stale: older lists sent us to offline uploaders
NOT_FOUND_WAIT = 24 * 3600   # an album nobody shares today may be shared tomorrow
RETRY_WAIT = 3600            # after a used-up list, search again this much later
YOUTUBE_TITLE_FLOOR = 0.2     # 2026-10-09: right albums scored 0.23 and 0.40, same-titled ones by others 0.03-0.18
MAX_ROUNDS = 2               # used-up or empty searches before giving up
AHEAD = {"soulseek": 6, "torrent": 4, "youtube": 2}   # jobs waiting per source; more go stale
QUEUE_CAP = 95               # the app refuses more than 100 unfinished jobs per owner
# Errors that say nothing about the uploader; they do not count toward blocking one.
TRANSIENT = ("wait timed out", "Soulseek is busy", "HTTP 503", "HTTP 502", "yt-dlp download failed")
BLOCKING = ("banned this account", "download quota", "human check")
ACTIVE = ("queued", "downloading", "process_queued", "processing", "publish_queued", "publishing")
DONE = ("have", "not_found", "gave_up", "skipped")


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
                "searched_at": None, "next_search_at": 0, "youtube_searched": False, "job_id": None,
                "wrong_jobs": [], "added_at": self.clock(), **extra}
        if not item["artist"] or not item["album"]:
            item.update(status="skipped", note="Not an album (needs an artist and an album)")
        elif item.get("skip"):
            item.update(status="skipped", note=str(item["skip"]))
        self.save(item)
        return item

    def retry(self, item):
        item.update(status="wanted", rounds=0, next_search_at=0, candidates=[], searched_at=None,
                    youtube_searched=False, note="Retry requested; searching again")
        self.save(item)

    # ---- the loop ------------------------------------------------------------------------
    def tick(self):
        items = [i for i in self.items() if i.get("status") not in ("skipped",)]
        if not items:
            return
        library = self._library()
        jobs = {j["id"]: j for j in self.store.list("jobs")}
        blocked = self._blocked(jobs.values())
        # A few searches per tick (each can take a minute): job updates for other albums stay prompt.
        self._searches_left = int(self.config.get("wishlist_searches_per_tick", 3))
        for item in items:
            try:
                self._advance(item, library, jobs, blocked, items)
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

    def _advance(self, item, library, jobs, blocked, everything):
        if library is not None and matching.library_has(library, item):
            if item["status"] != "have":
                item.update(status="have", note="In the library", job_id=None, candidates=[])
                self.save(item)
            return
        if item["status"] in DONE:
            return
        if matching.superseded(item, [o for o in everything if o["owner"] == item["owner"]]):
            item.update(status="skipped", note="Covered by an expanded edition in the same list")
            self.save(item)
            return
        if item.get("job_id"):
            if not self._follow_job(item, jobs.get(item["job_id"])):
                return   # still running, or waiting for review
        self._next(item, jobs, blocked)

    def _follow_job(self, item, job):
        """Update the item from its job. True when the item needs its next candidate."""
        if not job:
            item.update(job_id=None)
            return True
        stage = job["stage"]
        if stage in ACTIVE:
            status = "downloading" if stage in ("downloading",) else "queued" if stage == "queued" else "processing"
            if item["status"] != status:
                item.update(status=status, note=job.get("detail") or stage)
                self.save(item)
            return False
        if stage == "review":
            if item["status"] != "review":
                item.update(status="review", note="Waiting for your review")
                self.save(item)
            return False
        if stage == "published" and job["id"] not in item.get("wrong_jobs", []):
            item.update(status="have", note="Added to the library", job_id=None, candidates=[])
            self.save(item)
            return False
        error = job.get("error") or ""
        retries = int(job.get("retry_count", 0))
        if stage == "failed" and job.get("failed_stage") == "download" and (job.get("progress") or 0) >= 50 and retries < 2:
            # Most of the album is here already: same source again. Anything else takes the next copy.
            if self.store.transition_job(job["id"], {"failed"}, "queued", cancel_requested=False, error=None,
                                         retry_count=retries + 1, detail="Retry queued by the wishlist"):
                item.update(status="queued", note="Retrying the same source")
                self.save(item)
                return False
        outcome = {"published": "wrong record", "rejected": "rejected in review", "cancelled": "cancelled"}.get(stage, "failed")
        candidate = job.get("candidate") or {}
        item["tried"].append({"provider": matching.provider(candidate), "source": candidate.get("source"),
                              "job": job["id"], "outcome": outcome, "error": error[:300], "at": self.clock()})
        item.update(job_id=None, status="wanted", note=f"{outcome.capitalize()}: {error[:160]}" if error else outcome.capitalize())
        self.save(item)
        return True

    def _next(self, item, jobs, blocked):
        now = self.clock()
        tried = {t["provider"] for t in item["tried"]}
        fresh = item.get("searched_at") and now - item["searched_at"] < FRESH
        if fresh:
            # Re-check cached candidates against today's rules: a fix should not wait 48 h to apply.
            current = {matching.provider(c) for c in matching.ranked(item["candidates"], item["album"], item["artist"],
                                                                      limit=len(item["candidates"]))}
            for c in item["candidates"]:
                if c.get("judged") != "model" and matching.provider(c) not in current:
                    continue
                who = matching.provider(c)
                if who in tried or who in blocked:
                    continue
                if not self._room(c["source"], jobs, item["owner"]):
                    if item["note"] != "Waiting for queue room":
                        item.update(status="wanted", note="Waiting for queue room")
                        self.save(item)
                    return
                self._queue(item, c, jobs)
                return
            if not item.get("youtube_searched"):
                # The list is used up: YouTube Music's official album is the last resort.
                item["youtube_searched"] = True
                found = self._search(item, sources=("youtube",))
                if found is None:
                    item["youtube_searched"] = False   # an error, not "not found": try next tick
                    return
                item["candidates"] += found
                self.save(item)
                if found:
                    return self._next(item, jobs, blocked)
            item["rounds"] = item.get("rounds", 0) + 1
            if item["rounds"] >= MAX_ROUNDS:
                gave = "not_found" if not any(t for t in item["tried"]) else "gave_up"
                item.update(status=gave, candidates=[], note=(
                    "Not found on Soulseek, RuTracker or YouTube Music" if gave == "not_found"
                    else f"Gave up after {len(item['tried'])} tries"))
            else:
                wait = RETRY_WAIT if item["candidates"] else NOT_FOUND_WAIT
                item.update(status="wanted", candidates=[], searched_at=None, next_search_at=now + wait,
                            note=("All candidates failed" if item["candidates"] else "Not found") + f"; searching again in {wait // 3600} h")
            self.save(item)
            return
        mine = sum(1 for j in jobs.values() if j.get("owner") == item["owner"] and j["stage"] in ACTIVE + ("review",))
        if now < item.get("next_search_at", 0) or mine >= QUEUE_CAP or self._searches_left <= 0:
            return
        self._searches_left -= 1
        found = self._search(item)
        if found is None:
            return   # search error, not "not found"
        item.update(candidates=found, searched_at=now, youtube_searched=False, status="wanted",
                    note=f"Found {len(found)} candidates" if found else "Nothing on Soulseek or RuTracker")
        self.save(item)
        self._next(item, jobs, blocked)

    def _search(self, item, sources=("soulseek", "torrent")):
        """Ranked candidates, or None when a search failed (not the same as found nothing)."""
        album, artist = item["album"], item["artist"]
        avoid = {t["provider"] for t in item["tried"]}
        sources_ = self.sources()
        results = []
        if "soulseek" in sources and self.soulseek_ready():
            for query in soulseek_queries(album, artist, item.get("year")):
                try:
                    found = sources_.search(query, "soulseek", "album")
                except SoulseekUnavailable:
                    raise
                except SourceError:
                    return None
                results += found   # the model may accept folders the rules would not: keep every step's results
                if matching.ranked(found, album, artist, avoid):
                    break
        elif "soulseek" in sources:
            return None
        if "torrent" in sources and sources_.torrents_configured():
            first = matching.clean_artist(artist)
            name = matching.re.sub(r"\s*[\(\[].*?[\)\]]", "", album).strip() or album
            for query in dict.fromkeys(q for q in (f"{first} {name}".strip(), first) if q):
                try:
                    found = sources_.search(query, "torrent", "album")
                except SourceError:
                    break   # tracker down: keep the Soulseek results
                results += found
                if any(c.get("source") == "torrent" for c in matching.ranked(found, album, artist, avoid)):
                    break
        if "youtube" in sources:
            # The album title alone too: blog lists misspell artists ("Andrea" for Andrae Durden).
            seen = set()
            for query in dict.fromkeys((f"{matching.clean_artist(artist)} {album}".strip(), album.strip())):
                try:
                    found = sources_.search(query, "youtube", "album")
                except SourceError:
                    return None
                results += [c for c in found if c.get("url") not in seen]
                seen |= {c.get("url") for c in found}
        return self._rank(item, results, avoid)

    def _rank(self, item, results, avoid):
        """The model decides which results are the album; code orders them by quality and source.
        Without a usable answer from the model, the matching rules decide, as before."""
        album, artist = item["album"], item["artist"]
        pool = [c for c in results if self._eligible(c, avoid)]
        pool.sort(key=lambda c: (-matching.quality(c), matching.SOURCE_ORDER.get(c.get("source"), 9), -self._source_score(c)))
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
                best.append({**c, "judged": "model", "reason": verdicts[n]["reason"], "score": round(self._source_score(c), 2)})
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

    @staticmethod
    def _source_score(c):
        if c.get("source") == "soulseek":
            return (0.5 if c.get("free_slots") else 0) - min(c.get("queue_length") or 0, 50) / 100
        if c.get("source") == "torrent":
            return min(c.get("seeders") or 0, 50) / 50
        return 0

    def _musicbrainz_tracklist(self, item):
        try:
            with httpx.Client() as client:
                return album_tracklist(client, matching.clean_artist(item["artist"]), item["album"])
        except Exception:
            return None   # evidence only: the model judges without it

    def _room(self, source, jobs, owner):
        mine = [j for j in jobs.values() if j.get("owner") == owner and j["stage"] in ACTIVE + ("review",)]
        if len(mine) >= QUEUE_CAP:
            return False
        if source == "soulseek":
            busy = [j for j in mine if j["stage"] == "queued" and (j.get("candidate") or {}).get("source") == "soulseek"]
        else:
            busy = [j for j in mine if j["stage"] in ("queued", "downloading") and (j.get("candidate") or {}).get("source") == source]
        return len(busy) < AHEAD.get(source, 2)

    def _queue(self, item, candidate, jobs):
        candidate = {k: v for k, v in candidate.items() if k not in ("score", "typical", "owner")}
        candidate.update(requested_artist=matching.clean_artist(item["artist"]) or None, requested_album=item["album"],
                         wishlist_id=item["id"])
        try:
            job = self.enqueue(candidate, {"username": item["owner"]})
        except Exception as exc:   # the app's queue is full: wait
            item.update(status="wanted", note="Waiting for queue room")
            self.save(item)
            log.info("Wishlist could not queue %s: %s", item["id"], getattr(exc, "detail", exc))
            return
        # Count it now, so the next item in this tick sees the room it took.
        jobs[job["id"]] = {"id": job["id"], "owner": item["owner"], "stage": "queued", "candidate": candidate}
        item.update(job_id=job["id"], status="queued", note=f"Queued from {candidate['source']}: {candidate.get('username') or candidate.get('title')}")
        self.save(item)

    def _blocked(self, jobs):
        """Uploaders avoided for every album: they banned us, have a quota, wait for a human
        check, or failed 3 downloads (a queue that never moves)."""
        failures, blocked = {}, set()
        for j in jobs:
            c = j.get("candidate") or {}
            if c.get("source") != "soulseek" or not c.get("username"):
                continue
            error = j.get("error") or ""
            if j["stage"] == "failed" and any(w in error for w in BLOCKING):
                blocked.add(c["username"])
            if j["stage"] == "cancelled" or j["stage"] == "failed" and j.get("failed_stage") == "download" \
                    and not any(w in error for w in TRANSIENT):
                failures[c["username"]] = failures.get(c["username"], 0) + 1
        return blocked | {u for u, n in failures.items() if n >= 3}

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
