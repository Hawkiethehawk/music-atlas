from __future__ import annotations

import unittest
from copy import deepcopy
from unittest import mock

from contracts import track_key
from platform_discovery import collect_track_facts, discover_platform_candidates
from recommender import rank_candidates
from tests.test_workflow import candidate_fixture, minimal_analysis_packet


class FullPlaylistExclusionTests(unittest.TestCase):
    def test_excludes_tracks_outside_the_analysis_subset(self) -> None:
        packet = {
            "analysis_id": "analysis-test",
            "primary_distribution": [{"artist": "Imminence", "entity_ref": "artist:imminence", "count": 1}],
            "favorite_track_keys": [track_key("Analysed", "Imminence")],
            # This is a song in the original playlist but outside a chosen analysis limit.
            "playlist_exclusion": {"track_keys": [track_key("Already Elsewhere", "Imminence")], "platform_track_ids": ["2"]},
        }
        hits = [
            {"title": "Already Elsewhere", "artist": "Imminence", "album": "A", "cover": None,
             "platform_id": "2", "url": "https://music.163.com/song?id=2"},
            {"title": "New Song", "artist": "Imminence", "album": "B", "cover": None,
             "platform_id": "3", "url": "https://music.163.com/song?id=3"},
        ]
        with mock.patch("platform_discovery.SOURCES", {"netease": lambda _title, _artist: hits}), \
             mock.patch("platform_discovery.SOURCE_PRIORITY", ("netease",)), \
             mock.patch("platform_discovery.netease_song_cover", return_value=None):
            candidates, _report = discover_platform_candidates(packet, max_candidates=10)
        self.assertEqual([(item["title"], item["platform_track_id"]) for item in candidates], [("New Song", "3")])

    def test_ranking_defense_excludes_full_playlist_track(self) -> None:
        packet = minimal_analysis_packet()
        candidates = [
            candidate_fixture(packet, index, candidate_type)
            for index, candidate_type in enumerate(
                ("artist_continuation", "musician_relation", "style_neighbor", "exploration", "exploration"), 1
            )
        ]
        excluded = candidates[0]
        packet["playlist_exclusion"] = {
            "source_track_count": 91,
            "track_keys": [track_key(excluded["title"], excluded["artist"])],
            "platform_track_ids": [],
        }

        selected, _manifest = rank_candidates(deepcopy(candidates), packet, limit=4)

        self.assertEqual(len(selected), 4)
        self.assertNotIn(excluded["canonical_track_id"], {item["canonical_track_id"] for item in selected})


class TrackFactSourceTests(unittest.TestCase):
    def test_song_links_are_recorded_without_repeating_search(self) -> None:
        snapshot = {
            "snapshot_id": "apple-test", "platform": "apple_music",
            "tracks": [
                {"title": "First", "artist": "A", "album": "One", "platform_track_id": "101",
                 "links": {"apple_music": "https://music.apple.com/us/song/first/101"}},
                {"title": "Second", "artist": "B", "album": "Two", "platform_track_id": "202",
                 "links": {"apple_music": "https://music.apple.com/us/album/two/12?i=202"}},
            ],
        }
        with mock.patch("platform_discovery.verify_many") as search:
            bundle = collect_track_facts(snapshot)
        search.assert_not_called()
        self.assertEqual((bundle["track_count"], bundle["verified_count"],
                          bundle["source_recorded_count"], bundle["unverified_count"]), (2, 0, 2, 0))
        self.assertTrue(all(item["platform_fact"] is None for item in bundle["records"]))
        self.assertTrue(all(item["source_origin"] == "snapshot_song_url" for item in bundle["records"]))

    def test_missing_or_mismatched_links_only_search_missing_and_keep_failure_unverified(self) -> None:
        snapshot = {
            "snapshot_id": "netease-test", "platform": "netease",
            "tracks": [
                {"title": "Linked", "artist": "A", "platform_track_id": "1",
                 "links": {"netease": "https://music.163.com/song?id=1"}},
                {"title": "Missing", "artist": "B", "platform_track_id": "2", "links": {}},
                {"title": "Wrong ID", "artist": "C", "platform_track_id": "3",
                 "links": {"netease": "https://music.163.com/song?id=999"}},
                {"title": "Skipped", "artist": "D", "platform_track_id": "4", "links": {}},
            ],
        }
        fact = {"source": "netease", "url": "https://music.163.com/song?id=2",
                "title": "Missing", "artist": "B", "platform_track_id": "2"}
        with mock.patch("platform_discovery.verify_many", return_value=[fact, None, {"source": "skipped"}]) as search:
            bundle = collect_track_facts(snapshot, concurrency=4)
        search.assert_called_once_with([("Missing", "B"), ("Wrong ID", "C"), ("Skipped", "D")], concurrency=4)
        self.assertEqual([item["status"] for item in bundle["records"]],
                         ["source_recorded", "verified", "unverified", "unverified"])
        self.assertEqual((bundle["verified_count"], bundle["source_recorded_count"],
                          bundle["unverified_count"]), (1, 1, 2))
        self.assertIsNone(bundle["records"][2]["source_url"])
        self.assertIsNone(bundle["records"][3]["platform_fact"])

    def test_apple_missing_song_link_is_not_searched_or_mislabeled(self) -> None:
        snapshot = {"snapshot_id": "apple-missing", "platform": "apple_music", "tracks": [
            {"title": "No URL", "artist": "A", "platform_track_id": "1", "links": {}},
            {"title": "Wrong URL", "artist": "B", "platform_track_id": "2",
             "links": {"apple_music": "https://music.apple.com/us/song/wrong/999"}},
        ]}
        with mock.patch("platform_discovery.verify_many") as search:
            bundle = collect_track_facts(snapshot)
        search.assert_not_called()
        self.assertEqual([record["status"] for record in bundle["records"]],
                         ["unverified", "unverified"])
        self.assertTrue(all(record["platform_fact"] is None and record["source_url"] is None
                            for record in bundle["records"]))

    def test_independent_search_cannot_overwrite_mismatched_playlist_identity(self) -> None:
        snapshot = {"snapshot_id": "netease-mismatch", "platform": "netease", "tracks": [
            {"title": "Original", "artist": "A", "platform_track_id": "2", "links": {}}]}
        wrong = {"source": "netease", "url": "https://music.163.com/song?id=3",
                 "title": "Original", "artist": "A", "platform_track_id": "3"}
        with mock.patch("platform_discovery.verify_many", return_value=[wrong]):
            bundle = collect_track_facts(snapshot)
        self.assertEqual(bundle["records"][0]["status"], "unverified")
        self.assertIsNone(bundle["records"][0]["source_url"])


if __name__ == "__main__":
    unittest.main()
