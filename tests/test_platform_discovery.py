from __future__ import annotations

import unittest
from copy import deepcopy
from unittest import mock

from contracts import track_key
from platform_discovery import discover_platform_candidates
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


if __name__ == "__main__":
    unittest.main()
