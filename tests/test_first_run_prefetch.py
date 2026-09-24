"""Cold-start discovery must use the same routing rules as final selection."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

from candidate_routes import resolve_candidate_route
from web_workflow import (_candidate_prefetch_signature, _merge_prefetched_candidates,
                          _prefetch_large_playlist_window, _summary_prefetch_packet)


TAXONOMY = Path(__file__).resolve().parents[1] / "styles" / "style_taxonomy.json"


class FirstRunPrefetchTests(unittest.TestCase):
    def _packet(self, count: int) -> dict:
        tracks = [
            {"title": f"Song {index}", "artist": "Seed",
             "platform_track_id": str(index)}
            for index in range(count)
        ]
        snapshot = {"snapshot_id": f"snapshot-{count}", "tracks": tracks}
        return _summary_prefetch_packet(snapshot, tracks, TAXONOMY, None)

    def test_summary_prefetch_uses_actual_size_based_analysis_mode(self) -> None:
        self.assertEqual(self._packet(201)["analysis_mode"], "taste_summary")
        self.assertEqual(self._packet(2001)["analysis_mode"], "artist_summary")

    def test_prefetch_signature_covers_routing_mode(self) -> None:
        packet = self._packet(201)
        alternate = {**packet, "analysis_mode": "public_facts_only"}
        self.assertNotEqual(_candidate_prefetch_signature(packet),
                            _candidate_prefetch_signature(alternate))

    def test_relation_display_casing_does_not_discard_verified_prefetch(self) -> None:
        packet = self._packet(91)
        packet["primary_distribution"][0]["artist"] = "The Plot In You"
        packet["primary_distribution"][0]["entity_ref"] = "artist:theplotinyou"
        signature = _candidate_prefetch_signature(packet)
        final = {**packet, "primary_distribution": [
            {**packet["primary_distribution"][0], "artist": "The Plot in You"},
        ]}
        self.assertEqual(signature, _candidate_prefetch_signature(final))

        changed_punctuation = {**final, "primary_distribution": [
            {**final["primary_distribution"][0], "artist": "The Plot-In You"},
        ]}
        self.assertNotEqual(signature, _candidate_prefetch_signature(changed_punctuation))

        # Identity/count and playlist exclusions are still selection anchors.
        renamed = {**final, "primary_distribution": [
            {**final["primary_distribution"][0], "artist": "Another Artist",
             "entity_ref": "artist:anotherartist"},
        ]}
        self.assertNotEqual(signature, _candidate_prefetch_signature(renamed))
        changed_count = {**final, "primary_distribution": [
            {**final["primary_distribution"][0], "count": 2},
        ]}
        self.assertNotEqual(signature, _candidate_prefetch_signature(changed_count))
        changed_exclusion = {**final, "playlist_exclusion": {**final["playlist_exclusion"],
                                                              "source_track_count": 90}}
        self.assertNotEqual(signature, _candidate_prefetch_signature(changed_exclusion))

    def test_second_public_neighbor_is_not_lost_before_summary_finishes(self) -> None:
        packet = self._packet(201)
        url = "https://www.last.fm/music/Seed/+similar"
        candidate = {
            "artist": "Neighbor", "sources": [url],
            "provider_similarity": {
                "artist": "Neighbor", "seed": "Seed", "rank": 2,
                "match": 0.9, "url": url,
            },
        }
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"],
                         "style_neighbor")

    def test_relation_prefetch_survives_hard_limit(self) -> None:
        def row(index: int, kind: str) -> dict:
            return {"canonical_track_id": f"platform:netease:{kind}-{index}",
                    "title": f"Song {kind} {index}", "artist": f"Artist {kind} {index}",
                    "candidate_type": kind}

        base = [row(index, "artist_continuation") for index in range(120)]
        relations = [row(index, "musician_relation") for index in range(6)]
        pool = _merge_prefetched_candidates(
            base, relations, {"canonical_track_ids": set(), "track_keys": set()}, 120,
        )
        self.assertEqual(len(pool), 120)
        self.assertEqual(sum(item["candidate_type"] == "musician_relation" for item in pool), 6)


class PrefetchGapFillTests(unittest.TestCase):
    @staticmethod
    def row(index: int, kind: str) -> dict:
        return {"canonical_track_id": f"platform:netease:{index}",
                "title": f"Song {index}", "artist": f"Artist {index}",
                "candidate_type": kind}

    def test_two_missing_style_neighbors_keep_verified_pool_and_exclusions(self) -> None:
        import time
        from unittest import mock
        from contracts import track_key
        from web_workflow import _fill_prefetch_route_shortfall

        packet = {"primary_distribution": [
            {"artist": f"Seed {i}", "entity_ref": f"artist:seed{i}"}
            for i in range(48)]}
        original = ([self.row(i, "style_neighbor") for i in range(13)]
                    + [self.row(i, "artist_continuation") for i in range(13, 25)]
                    + [self.row(i, "exploration") for i in range(25, 66)])
        excluded = self.row(102, "style_neighbor")
        excluded["title"] = "Blocked"
        history = {"track_keys": {track_key("Blocked", excluded["artist"])},
                   "canonical_track_ids": set()}
        new = [original[0], self.row(100, "style_neighbor"),
               self.row(101, "style_neighbor"), excluded]
        calls = []
        def discover(probe, _client, **options):
            calls.append((probe["primary_distribution"][0]["artist"], options))
            return new, {"verification_rejected_count": 0}
        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}), \
             mock.patch("lastfm_pipeline.LastFM"), \
             mock.patch("lastfm_pipeline.discover", side_effect=discover):
            for hard_limit in (66, 200):
                pool, report = _fill_prefetch_route_shortfall(
                    packet, original, history,
                    {"style_neighbor": 15, "artist_continuation": 12, "exploration": 3},
                    hard_limit=hard_limit, concurrency=8, deadline=time.monotonic() + 60)
                self.assertEqual(len(pool), hard_limit if hard_limit == 66 else 68)
                self.assertEqual(sum(item["candidate_type"] == "style_neighbor" for item in pool), 15)
                self.assertEqual(report["route_shortfall"], {})
                self.assertEqual(report["attempts"][0]["anchor_start"], 16)
                self.assertFalse(any(item["title"] == "Blocked" for item in pool))
        self.assertEqual([first for first, _ in calls], ["Seed 16", "Seed 16"])

    def test_no_shortfall_does_not_research(self) -> None:
        import time
        from unittest import mock
        from web_workflow import _fill_prefetch_route_shortfall
        pool = ([self.row(i, "style_neighbor") for i in range(15)]
                + [self.row(i, "artist_continuation") for i in range(15, 27)]
                + [self.row(i, "exploration") for i in range(27, 30)])
        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}), \
             mock.patch("lastfm_pipeline.LastFM", side_effect=AssertionError("unneeded network")):
            result, report = _fill_prefetch_route_shortfall(
                {"primary_distribution": []}, pool,
                {"track_keys": set(), "canonical_track_ids": set()},
                {"style_neighbor": 15, "artist_continuation": 12, "exploration": 3},
                hard_limit=200, concurrency=8, deadline=time.monotonic() + 60)
        self.assertEqual(result, pool)
        self.assertEqual(report, {"attempts": [], "route_shortfall": {}})

    def test_large_playlist_window_verifies_only_second_sixteen_anchors(self) -> None:
        packet = {"source_track_count": 1917,
                  "primary_distribution": [
                      {"artist": f"Seed {i}", "entity_ref": f"artist:seed{i}"}
                      for i in range(40)],
                  "entities": [{"name": "Independent relation"}]}
        history = {"track_keys": {"blocked"}, "canonical_track_ids": {"platform:x:1"}}
        expected = [self.row(200, "style_neighbor")]

        def discovery(probe, _client, **options):
            self.assertEqual([a["artist"] for a in probe["primary_distribution"][:16]],
                             [f"Seed {i}" for i in range(16, 32)])
            self.assertEqual(probe["entities"], [])
            self.assertEqual(options["max_candidates"], 60)
            self.assertEqual(options["excluded_track_keys"], history["track_keys"])
            self.assertEqual(options["excluded_canonical_track_ids"], history["canonical_track_ids"])
            return expected, {"verification_rejected_count": 2}

        with mock.patch("lastfm_pipeline.LastFM"), \
             mock.patch("lastfm_pipeline.discover", side_effect=discovery):
            rows, report = _prefetch_large_playlist_window(
                packet, history, hard_limit=120, concurrency=8,
                deadline=time.monotonic() + 60)
        self.assertEqual(rows, expected)
        self.assertEqual(report, {"anchor_start": 16, "verified_count": 1,
                                  "verification_rejected_count": 2})
        self.assertEqual(packet["primary_distribution"][0]["artist"], "Seed 0")

    def test_small_playlist_does_not_speculatively_query_extra_window(self) -> None:
        with mock.patch("lastfm_pipeline.LastFM", side_effect=AssertionError("network")):
            self.assertEqual(_prefetch_large_playlist_window(
                {"source_track_count": 123, "primary_distribution": [{}] * 40},
                {}, hard_limit=120, concurrency=8,
                deadline=time.monotonic() + 60), ([], {}))

    def test_window_preserves_scarce_route_when_pool_is_full(self) -> None:
        base = [self.row(i, "artist_continuation") for i in range(30)]
        base += [self.row(i, "exploration") for i in range(30, 40)]
        supplement = [self.row(i, "style_neighbor") for i in range(40, 55)]
        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}):
            pool = _merge_prefetched_candidates(
                base, [], {"track_keys": set(), "canonical_track_ids": set()}, 40,
                supplemental=supplement, packet={}, needed={
                    "style_neighbor": 15, "artist_continuation": 12, "exploration": 3,
                })
        self.assertEqual(len(pool), 40)
        self.assertEqual(sum(item["candidate_type"] == "style_neighbor" for item in pool), 15)
        self.assertEqual(len({item["canonical_track_id"] for item in pool}), 40)

    def test_relation_shortfall_uses_only_source_backed_relation_discovery(self) -> None:
        from web_workflow import _fill_prefetch_route_shortfall
        packet = {"primary_distribution": [{"artist": f"Seed {i}", "entity_ref": f"artist:{i}"}
                                           for i in range(64)],
                  "entities": [{"related_projects": [{"name": "Verified project",
                                                       "person": "Verified member",
                                                       "sources": ["https://musicbrainz.org/artist/example"]}]}]}
        original = [self.row(i, "musician_relation") for i in range(5)]
        new = self.row(100, "musician_relation")
        calls = []

        def discover(_packet, _client, **options):
            calls.append(options)
            return [new], {"verification_rejected_count": 2}

        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}), \
             mock.patch("lastfm_pipeline.LastFM"), \
             mock.patch("lastfm_pipeline.discover", side_effect=discover):
            pool, report = _fill_prefetch_route_shortfall(
                packet, original, {"track_keys": set(), "canonical_track_ids": set()},
                {"musician_relation": 6}, hard_limit=40, concurrency=8,
                deadline=time.monotonic() + 60)
        self.assertEqual(len(pool), 6)
        self.assertEqual(report["route_shortfall"], {})
        self.assertEqual(report["attempts"][0]["route"], "musician_relation")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["include_similarity"])
        self.assertEqual(calls[0]["relation_top_track_limit"], 12)

    def test_relation_shortfall_without_public_projects_fails_fast(self) -> None:
        from web_workflow import _fill_prefetch_route_shortfall
        original = [self.row(i, "musician_relation") for i in range(5)]
        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}), \
             mock.patch("lastfm_pipeline.LastFM", side_effect=AssertionError("unrelated network")):
            pool, report = _fill_prefetch_route_shortfall(
                {"primary_distribution": [{}] * 64, "entities": []}, original,
                {"track_keys": set(), "canonical_track_ids": set()},
                {"musician_relation": 6}, hard_limit=40, concurrency=8,
                deadline=time.monotonic() + 60)
        self.assertEqual(pool, original)
        self.assertEqual(report["route_shortfall"], {"musician_relation": 1})
        self.assertEqual(report["attempts"], [])

    def test_missing_relation_route_is_reallocated_by_original_mix_weight(self) -> None:
        from web_workflow import _reallocate_route_mix_for_available_evidence

        packet = {
            "strict_recall_mix": True,
            "recommendation_policy": {
                "target_recommendations": 10,
                "recall_mix": [
                    {"candidate_type": "style_neighbor", "target_ratio": 0.4},
                    {"candidate_type": "artist_continuation", "target_ratio": 0.3},
                    {"candidate_type": "musician_relation", "target_ratio": 0.2},
                    {"candidate_type": "exploration", "target_ratio": 0.1},
                ],
            },
        }
        candidates = (
            [self.row(index, "style_neighbor") for index in range(40)]
            + [self.row(index + 40, "artist_continuation") for index in range(35)]
            + [self.row(index + 75, "exploration") for index in range(20)]
        )
        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}):
            report = _reallocate_route_mix_for_available_evidence(packet, candidates)

        self.assertIsNotNone(report)
        self.assertEqual(report["route_shortfall"], {"musician_relation": 6})
        self.assertEqual(report["effective_quota_per_group"], {
            "style_neighbor": 5,
            "artist_continuation": 4,
            "musician_relation": 0,
            "exploration": 1,
        })
        self.assertTrue(packet["strict_recall_mix"])
        self.assertEqual(packet["recommendation_policy"]["recall_mix"], [
            {"candidate_type": "style_neighbor", "target_ratio": 0.5},
            {"candidate_type": "artist_continuation", "target_ratio": 0.4},
            {"candidate_type": "exploration", "target_ratio": 0.1},
        ])

    def test_relation_candidates_are_evenly_reserved_when_below_original_quota(self) -> None:
        from web_workflow import _reallocate_route_mix_for_available_evidence

        packet = {
            "strict_recall_mix": True,
            "recommendation_policy": {
                "target_recommendations": 10,
                "recall_mix": [
                    {"candidate_type": "style_neighbor", "target_ratio": 0.4},
                    {"candidate_type": "artist_continuation", "target_ratio": 0.3},
                    {"candidate_type": "musician_relation", "target_ratio": 0.2},
                    {"candidate_type": "exploration", "target_ratio": 0.1},
                ],
            },
        }
        candidates = (
            [self.row(index, "style_neighbor") for index in range(40)]
            + [self.row(index + 40, "artist_continuation") for index in range(35)]
            + [self.row(index + 75, "musician_relation") for index in range(3)]
            + [self.row(index + 78, "exploration") for index in range(20)]
        )
        with mock.patch("candidate_routes.resolve_candidate_route",
                        side_effect=lambda item, _packet: {"candidate_type": item["candidate_type"]}):
            report = _reallocate_route_mix_for_available_evidence(packet, candidates)

        self.assertIsNotNone(report)
        self.assertEqual(report["effective_quota_per_group"], {
            "style_neighbor": 5,
            "artist_continuation": 3,
            "musician_relation": 1,
            "exploration": 1,
        })
if __name__ == "__main__":
    unittest.main()
