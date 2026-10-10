import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import matching
from app.store import Store, now, uid
from app.wishlist import Wishlist, SEARCH_AGAIN, SEARCHES, UPGRADE_EVERY, UPGRADE_FOR


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

    def test_a_used_up_list_is_searched_again_daily_for_a_week_then_given_up(self):
        sources = FakeSources({})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual(saved["status"], "wanted")
        self.assertEqual(saved["next_search_at"], self.clock[0] + SEARCH_AGAIN)
        calls = len(sources.calls)
        w.tick()
        self.assertEqual(len(sources.calls), calls)   # waits instead of searching every pass
        for _ in range(SEARCHES - 1):
            self.clock[0] += SEARCH_AGAIN
            w.tick()
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "not_found")

    def test_a_lossy_album_is_upgraded_weekly_for_four_weeks(self):
        sources = FakeSources({"youtube": [yt("Jazz Codes", "Moor Mother - Topic", "OLAK5uy_j", "Moor Mother")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.assertEqual(self.queued[-1]["source"], "youtube")   # the best there is today: take it now
        self.finish(item["id"], "published")
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual((saved["status"], saved["quality"]), ("have", 0))
        calls = len(sources.calls)
        self.clock[0] += UPGRADE_EVERY - 60
        w.tick()
        self.assertEqual(len(sources.calls), calls)   # not before a week
        # A week later a 320 shows up: not lossless, so nothing is queued.
        sources.results["soulseek"] = [slsk("b", "mp3", 320)]
        self.clock[0] += 60
        w.tick()
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "have")
        # The next week a FLAC: queued, and once published the album is lossless.
        sources.results["soulseek"] = [slsk("a")]
        self.clock[0] += UPGRADE_EVERY
        w.tick()
        self.assertEqual(self.queued[-1]["username"], "a")
        self.assertEqual(self.store.get("wishlist", item["id"])["status"], "have")
        self.finish(item["id"], "published")
        w.tick()
        saved = self.store.get("wishlist", item["id"])
        self.assertEqual((saved["quality"], saved["upgrade_until"]), (3, None))

    def test_the_upgrade_search_stops_after_four_weeks(self):
        sources = FakeSources({"youtube": [yt("Jazz Codes", "Moor Mother - Topic", "OLAK5uy_j", "Moor Mother")]})
        w = self.wishlist(sources)
        item = w.add("mateo", "Moor Mother", "Jazz Codes")
        w.tick()
        self.finish(item["id"], "published")
        w.tick()
        self.clock[0] += UPGRADE_FOR
        w.tick()
        calls = len(sources.calls)
        sources.results["soulseek"] = [slsk("a")]
        self.clock[0] += UPGRADE_EVERY
        w.tick()
        self.assertEqual(len(sources.calls), calls)
        self.assertIn("no lossless copy", self.store.get("wishlist", item["id"])["note"])

    def test_lossy_albums_from_before_join_the_upgrade_search(self):
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
        self.assertEqual(self.queued[-1]["username"], "a")
        self.assertEqual(saved["status"], "have")

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
        self.clock[0] += SEARCH_AGAIN
        w.tick()
        self.assertEqual(self.queued[-1]["source"], "youtube")

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


class FakeJudge:
    def __init__(self, accept=None, fail=False):
        self.accept, self.fail, self.seen = accept, fail, []

    def verdicts(self, artist, album, candidates, year=None, tracklist=None):
        self.seen.append([matching.provider(c) for c in candidates])
        if self.fail:
            return None
        return {n: {"match": matching.provider(c) in self.accept, "problem": "none" if matching.provider(c) in self.accept
                    else "other_record", "reason": "test"} for n, c in enumerate(candidates)}




def _wishlist(store, sources, judge, queued):
    def enqueue(candidate, user):
        id = uid()
        store.put("jobs", id, {"candidate": candidate, "source": candidate["source"]}, owner=user["username"],
                  stage="queued", created_at=now())
        queued.append(candidate)
        return {"id": id}
    return Wishlist(store, {"navidrome_db": "/nonexistent"}, lambda: sources, enqueue, judge=judge, tracklist=lambda item: None)


def test_the_model_decides_which_results_are_the_album(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    # A scene folder the rules reject, and a chapter the rules accept: the model's verdict wins.
    scene = slsk("scene", folder="Moor_Mother-Jazz_Codes-(ANTI123)-WEB-FLAC-2022-DASH")
    chapter = slsk("chapter", folder="Moor Mother - Jazz Codes Order")
    judge = FakeJudge(accept={"scene"})
    w = _wishlist(store, FakeSources({"soulseek": [chapter, scene]}), judge, queued)
    item = w.add("mateo", "Moor Mother", "Jazz Codes")
    w.tick()
    assert [c["username"] for c in queued] == ["scene"]
    saved = store.get("wishlist", item["id"])
    assert [c["judged"] for c in saved["candidates"]] == ["model"]
    assert saved["rejected"][0]["name"].endswith("Jazz Codes Order")


def test_without_a_model_answer_the_rules_decide_as_before(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    w = _wishlist(store, FakeSources({"soulseek": [slsk("a"), slsk("chapter", folder="Moor Mother - Jazz Codes Order")]}),
                  FakeJudge(fail=True), queued)
    w.add("mateo", "Moor Mother", "Jazz Codes")
    w.tick()
    assert [c["username"] for c in queued] == ["a"]
    assert "judged" not in queued[0]


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





class OddsJudge:
    """Decisions-style verdicts: nothing matches, with a probability per title."""
    def __init__(self, odds):
        self.odds = odds

    def verdicts(self, artist, album, candidates, year=None, tracklist=None):
        return {n: {"match": False, "problem": "unclear", "probability": self.odds[c["title"]], "reason": "x"}
                for n, c in enumerate(candidates)}


def test_an_exact_youtube_title_the_model_finds_plausible_is_the_last_resort(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    sources = FakeSources({"youtube": [yt("Parallel Movement", "BDTom - Topic", "OLAK5uy_b"), yt("BLINDAO", "marquitos", "OLAK5uy_a")]})
    w = _wishlist(store, sources, OddsJudge({"BLINDAO": 0.4, "Parallel Movement": 0.17}), queued)
    w.add("mateo", "ODDLIQUOR", "BLINDAO")
    w.tick()
    assert [c["title"] for c in queued] == ["BLINDAO"]
    assert ("BLINDAO", "youtube") in sources.calls   # the title alone is searched too


def test_same_titled_youtube_albums_the_model_doubts_are_not_taken(tmp_path):
    store, queued = Store(tmp_path / "a.db"), []
    # Other artists' records: exact title but under the floor, or the title's words out of order.
    sources = FakeSources({"youtube": [yt("Liquor Store Run", "LiL'WooFyWooF - Topic", "OLAK5uy_c"),
                                       yt("Indigo Blue", "Evan Purdy - Topic", "OLAK5uy_d")]})
    w = _wishlist(store, sources, OddsJudge({"Liquor Store Run": 0.06, "Indigo Blue": 0.5}), queued)
    w.add("mateo", "bastienGOAT", "Liquor Store Run")
    w.tick()
    w.add("mateo", "Darius C", "Blue Indigo")
    w.tick()
    assert queued == []
