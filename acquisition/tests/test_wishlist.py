import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import matching
from app.store import Store, now, uid
from app.wishlist import Wishlist, NOT_FOUND_WAIT


def slsk(user, fmt="flac", bitrate=None, files=10, folder="Moor Mother - Jazz Codes"):
    return {"id": f"s-{user}", "source": "soulseek", "kind": "album", "username": user, "directory": f"Music\\{folder}",
            "title": folder, "file_count": files, "format": fmt, "bitrate": bitrate, "free_slots": True, "queue_length": 0}


def torrent(key, title="Moor Mother - Jazz Codes - 2022, FLAC (tracks)", fmt="flac", bitrate=None, seeders=5):
    return {"id": f"t-{key}", "torrent_key": key, "source": "torrent", "kind": "album", "username": "RuTracker",
            "title": title, "format": fmt, "bitrate": bitrate, "seeders": seeders}


class FakeSources:
    def __init__(self, results):
        self.results, self.calls = results, []

    def search(self, query, source, kind):
        self.calls.append((query, source))
        return list(self.results.get(source, []))

    def torrents_configured(self):
        return True


class WishlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "a.db")
        self.clock = [1_000_000.0]
        self.queued = []

    def tearDown(self):
        self.tmp.cleanup()

    def wishlist(self, sources):
        def enqueue(candidate, user):
            id = uid()
            self.store.put("jobs", id, {"candidate": candidate, "source": candidate["source"]},
                           owner=user["username"], stage="queued", created_at=now())
            self.queued.append(candidate)
            return {"id": id}
        w = Wishlist(self.store, {"navidrome_db": "/nonexistent"}, lambda: sources, enqueue, clock=lambda: self.clock[0])
        return w

    def finish(self, item_id, stage, **data):
        item = self.store.get("wishlist", item_id)
        job = self.store.get("jobs", item["job_id"])
        self.store.update_job(job["id"], stage=stage, **data)

    def test_failures_walk_the_ranked_list_without_searching_again(self):
        sources = FakeSources({"soulseek": [slsk("a"), slsk("b", "mp3", 320)], "torrent": [torrent("t1")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes", "Blog 2025")
        w.tick()
        searches = len(sources.calls)
        # Quality first, then Soulseek before torrents: FLAC from a, FLAC torrent, then a's 320.
        self.assertEqual([c["source"] for c in self.store.get("wishlist", item["id"])["candidates"]], ["soulseek", "torrent", "soulseek"])
        self.assertEqual(self.queued[-1]["username"], "a")
        self.finish(item["id"], "failed", failed_stage="download", error="Soulseek peer a never accepted the request", progress=0)
        w.tick()
        self.assertEqual(self.queued[-1]["source"], "torrent")
        self.finish(item["id"], "failed", failed_stage="download", error="The torrent made no progress for 60 minutes", progress=0)
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "b")
        self.assertEqual(len(sources.calls), searches)   # no new search
        self.assertEqual(self.queued[-1]["requested_album"], "Jazz Codes")
        self.finish(item["id"], "published")
        w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "have")

    def test_not_found_waits_a_day_tries_youtube_once_per_round_then_stops(self):
        sources = FakeSources({})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual(saved["status"], "wanted")
        self.assertEqual(saved["next_search_at"], self.clock[0] + NOT_FOUND_WAIT)
        self.assertEqual(sum(1 for _, s in sources.calls if s == "youtube"), 1)
        calls = len(sources.calls)
        w.tick()
        self.assertEqual(len(sources.calls), calls)   # waits instead of searching every pass
        self.clock[0] += NOT_FOUND_WAIT
        w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "not_found")
        self.assertEqual(sum(1 for _, s in sources.calls if s == "youtube"), 2)

    def test_a_full_soulseek_queue_holds_soulseek_copies_but_not_torrents(self):
        for n in range(6):
            self.store.put("jobs", uid(), {"candidate": {"source": "soulseek", "username": f"u{n}"}}, owner="mateo",
                           stage="queued", created_at=now())
        w = self.wishlist(FakeSources({"soulseek": [slsk("a")]}))
        waiting = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.assertEqual(self.store.get("wishlist", waiting["id"])["note"], "Waiting for queue room")
        self.assertEqual(self.queued, [])   # searched ahead, but the FLAC from Soulseek waits for its slot
        w = self.wishlist(FakeSources({"torrent": [torrent("t1")]}))
        w.add("mateo", "Moor Mother", "Jazz Codes", "Other list")
        w.tick()
        self.assertEqual([c["source"] for c in self.queued], ["torrent"])

    def test_half_downloaded_jobs_resume_with_the_same_source(self):
        sources = FakeSources({"soulseek": [slsk("a"), slsk("b")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        job_id = self.store.get("wishlist", item["id"])["job_id"]
        self.finish(item["id"], "failed", failed_stage="download", error="Download from a exceeded the job time limit", progress=60)
        w.tick()
        self.assertEqual(self.store.get("jobs", job_id)["stage"], "queued")
        self.assertEqual(self.store.get("wishlist", item["id"])["job_id"], job_id)
        self.assertEqual(len(self.queued), 1)

    def test_uploaders_that_banned_us_are_skipped_for_every_album(self):
        self.store.put("jobs", uid(), {"candidate": {"source": "soulseek", "username": "a"}, "error": "a banned this account",
                                       "failed_stage": "download"}, owner="mateo", stage="failed", created_at=now())
        sources = FakeSources({"soulseek": [slsk("a"), slsk("b")]})
        w = self.wishlist(sources)
        w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "b")

    def test_an_album_in_the_library_needs_nothing(self):
        sources = FakeSources({"soulseek": [slsk("a")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        with patch.object(Wishlist, "_library", return_value={}), patch("app.matching.library_has", return_value=True):
            w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "have")
        self.assertEqual(sources.calls, [])

    def test_entries_without_an_album_are_skipped(self):
        w = self.wishlist(FakeSources({}))
        self.assertEqual(w.add("mateo", "", "Some mixtape talk")["status"], "skipped")


class RankingTests(unittest.TestCase):
    def test_ranked_orders_by_quality_then_source_and_skips_wrong_records(self):
        results = [slsk("mp3user", "mp3", 320), torrent("t1"), slsk("flacuser"),
                   slsk("chapter", folder="Moor Mother - Jazz Codes Order"), slsk("remix", folder="Moor Mother - Jazz Codes (Remixes)"),
                   torrent("t2", "Moor Mother - Jazz Codes: Order - 2022, FLAC (tracks)"), slsk("low", "mp3", 128)]
        order = [matching.provider(c) for c in matching.ranked(results, "Jazz Codes", "Moor Mother")]
        self.assertEqual(order, ["flacuser", "t1", "chapter", "mp3user"])   # remixes, chapter torrent, 128 kbps: out


def test_an_album_split_by_per_track_album_artists_counts_as_one():
    library = {f"id{n}": {"keys": {matching.album_key("3RMX82")}, "numbers": matching.numbers("3RMX82"),
                          "artists": {matching.key(f"Machinedrum, Guest {n}")}, "tracks": [f"t{n}a", f"t{n}b"]} for n in range(3)}
    assert len(matching.album_tracks(library, {"artist": "MachineDrum", "album": "3RMX82"})) == 6
    assert matching.library_has(library, {"artist": "MachineDrum", "album": "3RMX82"})


def test_a_discography_must_be_the_same_artist_not_one_sharing_a_word():
    titles = ["(Classic Hard Rock) Blond Viper - Discography - 1990-2000, FLAC (tracks)",
              "(Drum & Bass) Viper Recordings Discography - 2010-2024, FLAC (tracks)",
              "(Melodic Rock | AOR) Faith Nation - Дискография - 2010-2020, FLAC (tracks)",
              "(Rap) Viper - Дискография / Discography - 2008-2024, MP3, 320 kbps"]
    found = [matching.pick_torrent([{"source": "torrent", "title": t, "format": "flac" if "FLAC" in t else "mp3",
                                     "bitrate": 320, "seeders": 3, "torrent_key": t}], "The Hiram Clarke Hamster", "Viper")[1]
             for t in titles]
    assert [bool(f) for f in found] == [False, False, False, True]
