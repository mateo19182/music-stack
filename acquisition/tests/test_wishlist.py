import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import matching
from app.store import Store, now, uid
import json as jsonlib

import httpx

from app import finder as finder_module
from app import wishlist as wishlist_module
from app.finder import Finder
from app.wishlist import Wishlist


def slsk(user, fmt="flac", bitrate=None, files=10, folder="Moor Mother - Jazz Codes"):
    return {"id": f"s-{user}", "source": "soulseek", "kind": "album", "username": user, "directory": f"Music\\{folder}",
            "title": folder, "file_count": files, "format": fmt, "bitrate": bitrate, "free_slots": True, "queue_length": 0}


def torrent(key, title="Moor Mother - Jazz Codes - 2022, FLAC (tracks)", fmt="flac", bitrate=None, seeders=5):
    return {"id": f"t-{key}", "torrent_key": key, "source": "torrent", "kind": "album", "username": "RuTracker",
            "title": title, "format": fmt, "bitrate": bitrate, "seeders": seeders}


def yt(title, channel, url, artist=None):
    return {"id": f"y-{url}", "source": "youtube", "kind": "album", "title": title, "official": True, "uploader": channel,
            "artist": artist, "url": f"https://www.youtube.com/playlist?list={url}", "file_count": 7}


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
        w = Wishlist(self.store, {"navidrome_db": "/nonexistent", "download_workers": 4}, lambda: sources, enqueue,
                     clock=lambda: self.clock[0])
        return w

    def finish(self, item_id, stage, **data):
        item = self.store.get("wishlist", item_id)
        job = self.store.get("jobs", item["job_id"])
        self.store.update_job(job["id"], stage=stage, **data)

    def test_every_source_is_searched_at_once_and_failures_take_the_next_copy(self):
        sources = FakeSources({"soulseek": [slsk("a"), slsk("b", "mp3", 320)], "torrent": [torrent("t1")],
                               "youtube": [yt("Jazz Codes", "Moor Mother - Topic", "OLAK5uy_j", "Moor Mother")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes", "Blog 2025")
        w.tick()
        self.assertEqual({s for _, s in sources.calls}, {"soulseek", "torrent", "youtube"})
        searches = len(sources.calls)
        # Lossless first, then 320, YouTube last.
        self.assertEqual([c.get("format") or c["source"] for c in self.store.get("wishlist", item["id"])["candidates"]][-2:],
                         ["mp3", "youtube"])
        first = self.queued[-1]
        self.assertEqual(first["format"], "flac")
        self.finish(item["id"], "failed", failed_stage="download", error="did not start sending within 10 minutes", progress=60)
        w.tick()
        self.assertNotEqual(matching.provider(self.queued[-1]), matching.provider(first))   # no retry of the same copy
        self.assertEqual(self.queued[-1]["format"], "flac")
        self.finish(item["id"], "failed", failed_stage="download", error="x", progress=0)
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "b")
        self.assertEqual(len(sources.calls), searches)   # no new search
        self.assertEqual(self.queued[-1]["requested_album"], "Jazz Codes")

    def test_one_search_then_missing_until_searched_again(self):
        sources = FakeSources({})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "not_found")
        calls = len(sources.calls)
        self.clock[0] += 30 * 24 * 3600
        w.tick()
        self.assertEqual(len(sources.calls), calls)   # no scheduled searches
        w.retry(self.store.get("wishlist", item["id"]))
        sources.results["soulseek"] = [slsk("a")]
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "a")

    def test_a_request_whose_copies_all_failed_gives_up(self):
        sources = FakeSources({"soulseek": [slsk("a")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.finish(item["id"], "failed", failed_stage="download", error="x", progress=0)
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual(saved["status"], "gave_up")
        calls = len(sources.calls)
        self.clock[0] += 30 * 24 * 3600
        w.tick()
        self.assertEqual(len(sources.calls), calls)

    def test_a_lossy_album_is_parked_until_the_owner_searches_all_parked(self):
        sources = FakeSources({"youtube": [yt("Jazz Codes", "Moor Mother - Topic", "OLAK5uy_j", "Moor Mother")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.assertEqual(self.queued[-1]["source"], "youtube")   # the best there is today: take it now
        self.finish(item["id"], "published")
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual((saved["status"], saved["quality"]), ("have", 2))
        self.assertIn("parked", saved["note"])
        calls = len(sources.calls)
        self.clock[0] += 60 * 24 * 3600
        w.tick()
        self.assertEqual(len(sources.calls), calls)   # parked: never searched on its own
        # A 320 is no better than Premium YouTube (both 256 kbps and up): nothing is queued, and it parks again.
        sources.results["soulseek"] = [slsk("b", "mp3", 320)]
        self.assertEqual(w.search_parked("mateo"), (0, 1))
        w.tick()
        self.assertGreater(len(sources.calls), calls)
        self.assertEqual(len(self.queued), 1)
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual((saved["status"], saved["upgrade_wanted"]), ("have", False))
        calls = len(sources.calls)
        w.tick()
        self.assertEqual(len(sources.calls), calls)
        # The next search all parked finds a FLAC: queued, and once published the album is lossless.
        sources.results["soulseek"] = [slsk("a")]
        w.search_parked("mateo")
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "a")
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "have")
        self.finish(item["id"], "published")
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual((saved["quality"], saved["upgrade_wanted"]), (3, False))
        self.assertEqual(w.search_parked("mateo"), (0, 0))   # lossless now: nothing left to look for

    def test_search_all_parked_searches_missing_requests_once_more(self):
        sources = FakeSources({})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "not_found")
        calls = len(sources.calls)
        self.clock[0] += 60 * 24 * 3600
        w.tick()
        self.assertEqual(len(sources.calls), calls)
        sources.results["soulseek"] = [slsk("a")]
        self.assertEqual(w.search_parked("mateo", "Elsewhere"), (0, 0))   # another list: untouched
        self.assertEqual(w.search_parked("mateo"), (1, 0))
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "a")

    def test_lossy_albums_from_before_are_parked_for_the_lossless_search(self):
        sources = FakeSources({"soulseek": [slsk("a")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        saved = self.store.get("wishlist", item["id"])
        saved.update(status="have")
        w.save(saved)
        self.store.put("jobs", uid(), {"candidate": {"source": "youtube", "wishlist_id": item["id"]}}, owner="mateo",
                       stage="published", created_at=now())
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual(saved["quality"], 0)
        self.assertEqual(self.queued, [])
        w.search_parked("mateo")
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "a")
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "have")

    def test_albums_in_the_library_before_their_request_get_quality_from_the_files(self):
        sources = FakeSources({"soulseek": [slsk("a")]})
        w = self.wishlist(sources)
        album = {"keys": {matching.album_key("Jazz Codes")}, "numbers": matching.numbers("Jazz Codes"),
                 "artists": {matching.key("Moor Mother")}, "tracks": [f"t{n}" for n in range(6)], "titles": set(),
                 "qualities": [3, 3, 0, 3, 3, 3]}
        w._library = lambda: {"al1": album}
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual((saved["status"], saved["quality"]), ("have", 0))
        self.assertIn("parked", saved["note"])
        w.search_parked("mateo")
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "a")
        self.assertEqual(matching.file_quality("m4a", 987), 3)
        self.assertEqual(matching.file_quality("ogg", 135), 0)

    def test_youtube_copies_are_left_out_without_a_premium_login(self):
        premium = [False]
        sources = FakeSources({"youtube": [yt("Jazz Codes", "Moor Mother - Topic", "OLAK5uy_j", "Moor Mother")]})
        w = self.wishlist(sources)
        w.youtube_ready = lambda: premium[0]
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.assertEqual(self.queued, [])
        self.assertNotIn("youtube", {s for _, s in sources.calls})
        premium[0] = True
        w.retry(self.store.get("wishlist", item["id"]))
        w.tick()
        self.assertEqual(self.queued[-1]["source"], "youtube")

    def test_a_track_request_finds_the_recording_not_its_remix(self):
        def file(user, name, fmt="flac", bitrate=None, free=True):
            return {"id": f"s-{user}", "source": "soulseek", "kind": "track", "username": user, "free_slots": free,
                    "filename": f"Music\\Vulfpeck\\MSG\\{name}.{fmt}", "title": name, "format": fmt, "bitrate": bitrate}
        sources = FakeSources({"soulseek": [file("remix", "03 - Sauna (Remix)"), file("mp3", "Vulfpeck - Sauna", "mp3", 320),
                                            file("flac", "03 - Sauna"), file("other", "03 - Sauna Time")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Vulfpeck", list_name="Requests", kind="track", title="Sauna")
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual([c["username"] for c in saved["candidates"]], ["flac", "mp3"])   # "Sauna Time" is another song
        self.assertEqual((self.queued[-1]["username"], self.queued[-1]["requested_title"]), ("flac", "Sauna"))
        self.assertTrue(self.queued[-1]["premium_only"])
        self.assertNotIn("torrent", {s for _, s in sources.calls})

    def test_a_track_request_skips_other_remixers_and_reads_credits_and_album_in_names(self):
        def file(user, name, duration=175):
            return {"id": f"s-{user}", "source": "soulseek", "kind": "track", "username": user, "free_slots": True,
                    "filename": f"music\\100 gecs\\{name}.flac", "format": "flac", "duration": duration}
        title = "hand crushed by a mallet (Remix) [feat. Fall Out Boy, Craig Owens, Nicole Dollanganger]"
        found = [file("laura", "49 - 100 gecs - hand crushed by a mallet (Laura Les Remix)", 91),
                 file("nothankyou", "14 hand crushed by a mallet (N0thanky0u Remix)", 160),
                 file("credits", "100 gecs feat. Fall Out Boy, Craig Owens & Nicole Dollanganger - hand crushed by a mallet (remix)"),
                 file("album", "100 gecs - 1000 gecs and The Tree of Clues - 05 - hand crushed by a mallet (remix)"),
                 file("plain", "05 hand crushed by a mallet (Remix)"), file("original", "09 hand crushed by a mallet", 126)]
        kept = matching.ranked_tracks(found, "100 gecs", title, album="1000 gecs and The Tree of Clues")
        self.assertEqual({c["username"] for c in kept}, {"credits", "album", "plain"})
        named = matching.ranked_tracks(found, "100 gecs", "hand crushed by a mallet (Laura Les Remix)")
        self.assertEqual([c["username"] for c in named], ["laura"])
        # Credits before " - " are not the title; another song's words still are.
        pantha = [file("p", "Screama, Asher, Tez Kidd, DJ Pantha, Speckz - Badmans Of Bassline (Original Mix)", 339),
                  file("q", "DJ Pantha - Badmans Of Bassline Megamix", 339)]
        self.assertEqual([c["username"] for c in matching.ranked_tracks(pantha, "DJ Pantha", "Badmans Of Bassline")], ["p"])

    def test_a_dj_mix_track_requests_the_song_it_is_cut_from(self):
        def file(user, name):
            return {"id": f"s-{user}", "source": "soulseek", "kind": "track", "username": user, "free_slots": True,
                    "filename": f"music\\Bakey\\Take It Further EP\\{name}.flac", "format": "flac"}
        sources = FakeSources({"soulseek": [file("full", "01 Bakey - Yeh")]})
        w = self.wishlist(sources)
        w.add("mateo", "BAKEY", "Shall Not Fade: Killjoy (DJ Mix)", "Saved tracks", kind="track", title="Yeh - Mixed")
        w.tick()
        self.assertEqual(sources.calls[0], ("BAKEY Yeh", "soulseek"))
        self.assertEqual(self.queued[-1]["username"], "full")
        self.assertEqual(matching.unmixed("Yeh (Mixed)"), "Yeh")
        self.assertEqual(matching.unmixed("Mixed"), "Mixed")

    def test_when_every_listed_copy_fails_one_more_search_skips_the_uploaders_tried(self):
        sources = FakeSources({"soulseek": [slsk("a")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        sources.results["soulseek"] = [slsk("a"), slsk("b")]
        self.finish(item["id"], "failed", failed_stage="download", error="x", progress=0)
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "b")
        calls = len(sources.calls)
        sources.results["soulseek"] = [slsk("a"), slsk("b"), slsk("c")]
        self.finish(item["id"], "failed", failed_stage="download", error="x", progress=0)
        w.tick()
        self.assertEqual(len(sources.calls), calls)   # once only
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "gave_up")

    def test_a_retry_asks_failed_uploaders_again_but_not_rejected_copies(self):
        sources = FakeSources({"soulseek": [slsk("busy"), slsk("wrong", "mp3", 320)]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.finish(item["id"], "failed", failed_stage="download", error="did not start sending", progress=0)
        w.tick()
        self.finish(item["id"], "rejected")
        w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "gave_up")
        self.clock[0] += 60
        w.retry(self.store.get("wishlist", item["id"]))
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "busy")
        self.finish(item["id"], "failed", failed_stage="download", error="did not start sending", progress=0)
        w.tick()
        w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "gave_up")
        self.assertEqual([c["username"] for c in self.queued], ["busy", "wrong", "busy"])

    def test_a_link_is_downloaded_as_given_once(self):
        w = self.wishlist(FakeSources({}))
        item = w.add("mateo", "", list_name="Requests", kind="link", url="https://youtu.be/dQw4w9WgXcQ", link_kind="track")
        w.tick()
        self.assertEqual(self.queued[-1]["url"], "https://youtu.be/dQw4w9WgXcQ")
        self.assertFalse(self.queued[-1]["premium_only"])
        self.finish(item["id"], "failed", failed_stage="download", error="Video unavailable")
        w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "gave_up")
        self.clock[0] += 30 * 24 * 3600
        w.tick()
        self.assertEqual(len(self.queued), 1)

    def test_youtube_links_wait_while_youtube_is_paused(self):
        w = self.wishlist(FakeSources({}))
        w.youtube_enabled = lambda: False
        item = w.add("mateo", "", list_name="Requests", kind="link", url="https://youtu.be/dQw4w9WgXcQ")
        w.tick()
        self.assertEqual(self.queued, [])
        self.assertIn("YouTube is paused", self.store.get("wishlist", item["id"])["note"])
        bandcamp = w.add("mateo", "", list_name="Requests", kind="link", url="https://artist.bandcamp.com/album/x", link_kind="album")
        w.tick()
        self.assertEqual(self.queued[-1]["url"], "https://artist.bandcamp.com/album/x")

    def test_a_picked_copy_goes_first_and_a_search_follows_if_it_fails(self):
        sources = FakeSources({"soulseek": [slsk("a")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes", "Requests", pick=slsk("mine", "mp3", 320))
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "mine")
        self.assertEqual(sources.calls, [])   # no search for a picked copy
        self.finish(item["id"], "failed", failed_stage="download", error="did not start sending")
        w.tick()
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "a")

    def test_asking_again_restarts_a_request_that_gave_up_but_an_import_does_not(self):
        w = self.wishlist(FakeSources({}))
        item = w.add("mateo", "Moor Mother", "Jazz Codes", "Blog")
        saved = self.store.get("wishlist", item["id"])
        saved.update(status="gave_up")
        w.save(saved)
        self.assertEqual(w.add("mateo", "Moor Mother", "Jazz Codes", "Blog")["status"], "gave_up")
        again = w.add("mateo", "Moor Mother", "Jazz Codes", "Blog", renew=True)
        self.assertEqual(again["status"], "wanted")

    def test_queueing_waits_while_every_download_worker_has_a_job_waiting(self):
        for n in range(4):
            self.store.put("jobs", uid(), {"candidate": {"source": "soulseek", "username": f"u{n}"}}, owner="mateo",
                           stage="queued", created_at=now())
        w = self.wishlist(FakeSources({"soulseek": [slsk("a")]}))
        waiting = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.assertEqual(self.store.get("wishlist", waiting["id"])["note"], "Waiting for a download slot")
        self.assertEqual(self.queued, [])

    def test_an_album_in_the_library_needs_nothing(self):
        sources = FakeSources({"soulseek": [slsk("a")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        with patch.object(Wishlist, "_library", return_value={}), patch("app.matching.library_has", return_value=True):
            w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "have")
        self.assertEqual(sources.calls, [])

    def test_a_saved_track_that_reaches_the_library_is_had_not_offered(self):
        sources = FakeSources({"soulseek": [slsk("a")]})
        w = self.wishlist(sources)
        here = w.add("mateo", "Vulfpeck", "", "Saved tracks", kind="track", title="Sauna", status="suggested")
        missing = w.add("mateo", "Vulfpeck", "", "Saved tracks", kind="track", title="Dean Town", status="suggested")
        library = {"a": {"artists": {matching.key("Vulfpeck")}, "titles": {matching.track_key("Sauna (feat. Antwaun Stanley)")},
                         "tracks": [], "qualities": []}}
        with patch.object(Wishlist, "_library", return_value=library):
            w.tick()
        self.assertEqual(self.store.get("wishlist", here["id"])["status"], "have")
        self.assertEqual(self.store.get("wishlist", missing["id"])["status"], "suggested")
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
        # Who can deliver now goes first: a seeded torrent before a queued uploader, a thin torrent after it.
        queued = {**slsk("queueduser"), "free_slots": False, "queue_length": 20}
        order = matching.ranked([queued, torrent("t1"), torrent("t2", seeders=1)], "Jazz Codes", "Moor Mother")
        self.assertEqual([matching.provider(c) for c in order], ["t1", "queueduser", "t2"])


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


def test_failed_downloads_take_the_next_copy_instead_of_retrying(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    w = _wishlist(store, FakeSources({"soulseek": [slsk("a"), slsk("b")]}), None, queued)
    item = w.add("mateo", "Moor Mother", "Jazz Codes")
    w.tick()
    job = store.get("wishlist", item["id"])["job_id"]
    store.update_job(job, stage="failed", failed_stage="download", progress=0,
                     error="yt-dlp download failed ([youtube] x: Video unavailable). Retry or choose another candidate.")
    w.tick()
    assert [c["username"] for c in queued] == ["a", "b"]


def test_a_timed_out_browse_takes_the_next_copy(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    w = _wishlist(store, FakeSources({"soulseek": [slsk("a"), slsk("b")]}), None, queued)
    item = w.add("mateo", "Moor Mother", "Jazz Codes")
    w.tick()
    job = store.get("wishlist", item["id"])["job_id"]
    store.update_job(job, stage="failed", failed_stage="download", progress=10,
                     error="Soulseek returned HTTP 500: The wait timed out after 5000 milliseconds.")
    w.tick()
    assert [c["username"] for c in queued] == ["a", "b"]


def _wishlist(store, sources, finder, queued, youtube=True):
    def enqueue(candidate, user):
        id = uid()
        store.put("jobs", id, {"candidate": candidate, "source": candidate["source"]}, owner=user["username"],
                  stage="queued", created_at=now())
        queued.append(candidate)
        return {"id": id}
    return Wishlist(store, {"navidrome_db": "/nonexistent"}, lambda: sources, enqueue, finder=finder,
                    tracklist=lambda item: None, youtube_ready=lambda: youtube)


class ScriptedModel:
    """The model's side of the finder: each turn's tool calls, in order; what it was sent is kept."""
    def __init__(self, *turns):
        self.turns, self.sent = list(turns), []

    def __call__(self, url, json=None, **kwargs):
        self.sent.append(jsonlib.loads(jsonlib.dumps(json)))   # the finder keeps adding to its messages
        turn = self.turns.pop(0)
        if turn is None:
            return httpx.Response(503, request=httpx.Request("POST", url))
        calls = [{"id": f"call{n}", "type": "function", "function": {"name": name, "arguments": jsonlib.dumps(args)}}
                 for n, (name, args) in enumerate(turn)]
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": calls}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    def shown(self, turn):
        """The results the model was shown in a turn's request: {id: description}."""
        return {k: v for m in self.sent[turn]["messages"] if m["role"] == "tool"
                for k, v in jsonlib.loads(m["content"]).get("shown", {}).items()}


def finder(*turns):
    model = ScriptedModel(*turns)
    return Finder({"llm_api_key": "test"}, post=model), model


def search(query, source="soulseek", kind="album"):
    return ("search", {"query": query, "source": source, "kind": kind})


def test_the_model_searches_and_picks_and_code_keeps_the_facts(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    scene = slsk("scene", folder="Moor_Mother-Jazz_Codes-(ANTI123)-WEB-FLAC-2022-DASH")
    mp3 = slsk("mp3", fmt="mp3", bitrate=320)
    low = slsk("low", fmt="mp3", bitrate=128)
    f, model = finder([search("moor mother jazz codes")],
                      [("finish", {"picks": [{"id": "r2", "reason": "320"}, {"id": "r1", "reason": "scene rip"},
                                             {"id": "r9", "reason": "never shown"}], "note": "found it"})])
    w = _wishlist(store, FakeSources({"soulseek": [mp3, scene, low]}), f, queued)
    item = w.add("mateo", "Moor Mother", "Jazz Codes")
    w.tick()
    # The 128k copy is never shown; an id the model invents is dropped; lossless goes first.
    assert len(model.shown(1)) == 2
    assert [c["username"] for c in queued] == ["scene"]
    saved = store.get("wishlist", item["id"])
    assert [c["username"] for c in saved["candidates"]] == ["scene", "mp3"]
    assert saved["candidates"][0]["reason"] == "scene rip" and saved["search_note"] == "found it"
    assert saved["searches"] == [{"query": "moor mother jazz codes", "source": "soulseek", "kind": "album", "results": 3}]


def test_the_model_searches_again_when_a_name_is_dropped(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    f, model = finder([search("overmono good lies")], [search("good lies")],
                      [("finish", {"picks": [{"id": "r1", "reason": "the album"}], "note": "artist name ignored"})])
    sources = PhraseSources({"soulseek": [slsk("a", folder="Overmono - Good Lies")]}, "overmono")
    w = _wishlist(store, sources, f, queued)
    w.add("mateo", "Overmono", "Good Lies")
    w.tick()
    assert jsonlib.loads(model.sent[1]["messages"][-1]["content"])["results"] == 0
    assert [c["username"] for c in queued] == ["a"]


def test_without_the_model_one_plain_search_and_the_rules_decide(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    f, _ = finder(None)
    sources = FakeSources({"soulseek": [slsk("a"), slsk("chapter", folder="Moor Mother - Jazz Codes Order")]})
    w = _wishlist(store, sources, f, queued)
    w.add("mateo", "Moor Mother", "Jazz Codes")
    w.tick()
    assert [c["username"] for c in queued] == ["a"] and "judged" not in queued[0]
    assert ("Moor Mother Jazz Codes", "soulseek") in sources.calls


def test_the_search_budget_and_unavailable_sources_are_the_model_s_to_handle(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    turns = [[search(f"q{n}")] for n in range(finder_module.MAX_SEARCHES + 1)]
    f, model = finder(*turns, [search("x", "youtube"), search("x", "torrent", "track")], [("finish", {"picks": [], "note": "nothing"})])
    w = _wishlist(store, FakeSources({}), f, queued, youtube=False)
    item = w.add("mateo", "Moor Mother", "Jazz Codes")
    w.tick()
    answers = [jsonlib.loads(m["content"]) for m in model.sent[-1]["messages"] if m["role"] == "tool"]
    assert answers[finder_module.MAX_SEARCHES] == {"error": "No searches left: call finish."}
    assert [a["error"] for a in answers[-2:]] == ["YouTube is not available now.", "Torrents hold whole releases: search kind album."]
    assert store.get("wishlist", item["id"])["status"] == "not_found"


def test_a_song_request_takes_files_not_the_album_folder_around_them(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    folder = slsk("usb", folder="Fred again.. - USB")
    f, _ = finder([search("fred again usb")], [("finish", {"picks": [{"id": "r1", "reason": "album holding the song"}], "note": "x"})])
    w = _wishlist(store, FakeSources({"soulseek": [folder]}), f, queued)
    item = w.add("mateo", "Fred again..", "USB", kind="track", title="Berwyn")
    w.tick()
    assert queued == [] and store.get("wishlist", item["id"])["status"] == "not_found"


def test_a_substitute_waits_for_a_choice_and_a_single_s_song_keeps_its_title(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    song = {"id": "s-gibbs", "source": "soulseek", "kind": "track", "username": "gibbs", "title": "1995", "free_slots": True,
            "filename": "Music\\Freddie Gibbs\\Alfredo 2\\07 1995.flac", "format": "flac", "duration": 170}
    f, _ = finder([search("freddie gibbs 1995", kind="track")],
                  [("finish", {"picks": [{"id": "r1", "reason": "the single's song", "substitute": True}], "note": "single"})])
    w = _wishlist(store, FakeSources({"soulseek": [song]}), f, queued)
    item = w.add("mateo", "Freddie Gibbs", "1995")
    w.tick()
    saved = store.get("wishlist", item["id"])
    assert saved["status"] == "choice" and queued == []
    assert w.detail(saved)["copies"][0]["substitute"] is True
    w.choose(saved, "s-gibbs")
    w.tick()
    assert queued[0]["requested_title"] == "1995" and "requested_album" not in queued[0]


def test_a_request_whose_soulseek_search_keeps_failing_is_parked(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    class Failing(FakeSources):
        def search(self, query, source, kind):
            self.calls.append((query, source))
            raise wishlist_module.SourceError("Soulseek returned HTTP 409: not logged in")
    sources = Failing({})
    w = _wishlist(store, sources, None, queued)
    item = w.add("mateo", "JPEGMAFIA", "The Who", kind="track", title="The Who")
    for _ in range(3):
        w.tick()
    saved = store.get("wishlist", item["id"])
    assert saved["status"] == "not_found" and "3 searches in a row" in saved["note"]
    w.tick()
    assert len([c for c in sources.calls if c[1] == "soulseek"]) == 3


def test_a_soulseek_folder_far_bigger_than_an_album_is_not_a_candidate():
    album = {"source": "soulseek", "kind": "album", "format": "mp3", "bitrate": 320, "username": "u"}
    assert finder_module.eligible({**album, "file_count": 12})
    # An uploader's 480-track "club adjacent" folder, once judged 51% likely the album.
    assert not finder_module.eligible({**album, "file_count": 480})
    assert finder_module.eligible({**album, "file_count": finder_module.MAX_ALBUM_FILES})
    assert not finder_module.eligible({**album, "file_count": 12}, {"u"})


def test_a_track_is_in_the_library_by_artist_and_title_but_not_as_a_remix():
    library = {"a": {"artists": {matching.key("Vulfpeck")}, "titles": {matching.track_key("Sauna (feat. Antwaun Stanley)"),
                                                                      matching.track_key("Birds of a Feather (Remix)")}}}
    assert matching.library_has_track(library, {"artist": "Vulfpeck", "title": "Sauna"})
    assert not matching.library_has_track(library, {"artist": "Vulfpeck", "title": "Birds of a Feather"})
    assert not matching.library_has_track(library, {"artist": "Someone Else", "title": "Sauna"})


def test_tracks_in_library_agrees_with_library_has_track():
    library = {"a": {"artists": {matching.key("Vulfpeck")}, "titles": {matching.track_key("Sauna (feat. Antwaun Stanley)"),
                                                                      matching.track_key("Birds of a Feather (Remix)")}},
               "b": {"artists": {matching.key("Kokoroko")}, "titles": {""}}}
    items = [{"artist": a, "title": t} for a, t in [("Vulfpeck", "Sauna"), ("Vulfpeck", "Birds of a Feather"),
                                                     ("Someone Else", "Sauna"), ("Kokoroko", ""), ("Vulfpeck & Friends", "Sauna")]]
    assert matching.tracks_in_library(library, items) == [i for i in items if matching.library_has_track(library, i)]
    assert len(matching.tracks_in_library(library, items)) == 2


def test_an_old_youtube_copy_is_upgraded_by_premium_youtube(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    w = _wishlist(store, FakeSources({"youtube": [yt("Jazz Codes", "Moor Mother - Topic", "OLAK5uy_j", "Moor Mother")]}),
                  None, queued)
    item = w.add("mateo", "Moor Mother", "Jazz Codes")
    saved = store.get("wishlist", item["id"])
    saved.update(status="have", quality=0, upgrade_wanted=True)
    w.save(saved)
    w.tick()
    assert queued[-1]["source"] == "youtube" and queued[-1]["premium_only"]
    assert matching.quality(queued[-1]) == 2


def test_a_held_request_waits_at_its_copies_until_one_is_chosen(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    sources = FakeSources({"soulseek": [slsk("a"), slsk("b", "mp3", 320)]})
    w = _wishlist(store, sources, None, queued)
    item = w.add("mateo", "Moor Mother", "Jazz Codes", hold=True)
    w.tick()
    saved = store.get("wishlist", item["id"])
    assert (saved["status"], len(saved["candidates"]), queued) == ("choice", 2, [])
    assert saved["note"] == "2 copies found; waiting for a choice"
    searches = len(sources.calls)
    w.tick()
    assert len(sources.calls) == searches and not queued   # waits, instead of searching or queueing
    detail = w.detail(saved)
    assert [(c["id"], c["username"], c["format"], c["tried"]) for c in detail["copies"]] == [("s-a", "a", "flac", False), ("s-b", "b", "mp3", False)]
    assert w.choose(saved, "s-nobody") is None
    assert w.choose(saved, "s-b")["username"] == "b"
    w.tick()
    assert queued[-1]["username"] == "b" and store.get("wishlist", item["id"])["status"] == "queued"
    job = store.get("jobs", store.get("wishlist", item["id"])["job_id"])
    store.update_job(job["id"], stage="failed", error="did not start sending", progress=0)
    w.tick()
    saved = store.get("wishlist", item["id"])
    assert saved["status"] == "choice" and saved["note"].startswith("The chosen copy failed (did not start sending); 1 copy found")
    assert [c["tried"] for c in w.detail(saved)["copies"]] == [False, True]
    assert len(queued) == 1   # the other copy is not taken without a choice
    w.choose(saved, "s-a")
    w.tick()
    assert queued[-1]["username"] == "a"
    store.update_job(store.get("wishlist", item["id"])["job_id"], stage="published")
    w.tick()
    assert store.get("wishlist", item["id"])["status"] == "have"


def test_a_held_request_with_a_picked_result_or_a_link_needs_no_choice(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    w = _wishlist(store, FakeSources({}), None, queued)
    w.add("mateo", "Moor Mother", "Jazz Codes", pick=slsk("a"), hold=True)
    w.add("mateo", "", "", kind="link", url="https://youtu.be/dQw4w9WgXcQ", link_kind="track", hold=True)
    w.tick()
    assert sorted(c["source"] for c in queued) == ["soulseek", "youtube"]


def track_file(user, name, fmt="flac", bitrate=None, free=True):
    return {"id": f"s-{user}", "source": "soulseek", "kind": "track", "username": user, "free_slots": free,
            "filename": f"Music\\100 gecs\\{name}.{fmt}", "format": fmt, "bitrate": bitrate, "duration": 175}


class PhraseSources(FakeSources):
    """Soulseek as its server behaves: nothing at all for a query holding a dropped phrase."""
    def __init__(self, results, dropped):
        super().__init__(results)
        self.dropped = dropped

    def search(self, query, source, kind):
        self.calls.append((query, source))
        if source == "soulseek" and self.dropped in query.casefold():
            return []
        return [c for c in self.results.get(source, []) if c.get("kind", kind) == kind]
