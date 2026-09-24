from __future__ import annotations

import sys
import tempfile
import unittest
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_prompt import build_agent_prompt
from agent_runner import run_agent
from contracts import ContractError, STYLE_AXIS_IDS, validate_analysis_packet, write_json
from musician_analyzer import (
    _aggregate_style_axes, _normalize_style_profile, _style_assignment,
    analyze_and_validate, load_style_taxonomy, write_coverage_report,
)
from recommender import DEFAULT_WEIGHTS, _style_fit
from recommender import score_candidate
from source_adapters import build_snapshot


class ProfileQualityTests(unittest.TestCase):
    def analyze_unknown(self, root):
        write_json(root / "input.json", {"declared_track_count": 3, "tracks": [
            {"title": "Unknown one", "artist": "Uncatalogued", "album": "One"},
            {"title": "Unknown two", "artist": "Uncatalogued", "album": "Two"},
            {"title": "Unknown three", "artist": "Another uncatalogued"},
        ]})
        snapshot = build_snapshot(root / "input.json", reader_name="local_json", platform="local", playlist_id="test", playlist_name="test")
        write_json(root / "snapshot.json", snapshot)
        return analyze_and_validate(
            root / "snapshot.json", preferred_path=root / "absent.txt",
            relation_path=ROOT / "relations/artist_relations.json", output_path=root / "analysis.json",
            markdown_path=root / "analysis.md", style_profile_path=ROOT / "styles/artist_style_profiles.example.json",
        )

    def test_unknown_never_becomes_a_perfect_style_match(self):
        candidate = {"style_mix": [{"style_ref": "style:alternative_rock", "weight": 1}],
                     "sources": ["https://www.last.fm/music/Example/+tags"],
                     "evidence_items": [{"claim_type": "style", "url": "https://www.last.fm/music/Example/+tags"}]}
        self.assertEqual(_style_fit(candidate, {"style_analysis": {"interest_profiles": [], "style_definitions": []},
                                                "track_style_assignments": []}), 0)

    def test_unknown_analysis_writes_review_queue_but_cannot_call_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet = self.analyze_unknown(root)
            validate_analysis_packet(packet)
            self.assertIn("未知", (root / "analysis.md").read_text(encoding="utf-8"))
            report = write_coverage_report(packet, root / "coverage.json")
            self.assertEqual([item["affected_track_count"] for item in report["review_queue"]], [2, 1])
            self.assertEqual(report["classified_track_share"], 0)
            self.assertIn("unclassified_tracks", report["degraded_reasons"])
            with patch("agent_runner.run_external_agent") as agent:
                with self.assertRaisesRegex(ContractError, "画像覆盖不足"):
                    run_agent(root / "analysis.json", prompt_path=root / "prompt.md", output_path=root / "bundle.json",
                              report_output_path=root / "report.txt", command="fixture", mock=False, timeout=1)
                agent.assert_not_called()
            self.assertFalse((root / "bundle.json").exists())

    def test_track_override_precedes_album_and_preserves_field_provenance(self):
        taxonomy = load_style_taxonomy(ROOT / "styles/style_taxonomy.json")
        ref = taxonomy["known_style_refs"][0]
        raw = {"canonical_name": "Fixture", "style_mix": [{"style_ref": ref, "role": "primary", "weight": 1}],
               "style_axes": dict.fromkeys(STYLE_AXIS_IDS, 20), "confidence": "high", "summary": "fixture profile",
               "sources": ["https://example.com/artist"], "release_overrides": [
                   {"match_albums": ["Album"], "style_axes": dict.fromkeys(STYLE_AXIS_IDS, 50), "sources": ["https://example.com/album"]},
                   {"match_titles": ["Song"], "match_albums": ["Album"], "confidence": "low",
                    "style_axes": dict.fromkeys(STYLE_AXIS_IDS, 90), "sources": ["https://example.com/song"]},
               ]}
        profile = _normalize_style_profile(raw, fallback_name="Fixture", known_style_refs=set(taxonomy["known_style_refs"]))
        track = {"title": "Song", "artist": "Fixture", "album": "Album", "track_key": "fixture::song"}
        assignment = _style_assignment(track, profile)
        self.assertEqual(assignment["style_axes"]["heaviness"], 90)
        self.assertEqual(assignment["confidence"], "low")
        self.assertEqual(assignment["field_provenance"]["style_axes"]["sources"], ["https://example.com/song"])
        self.assertEqual(assignment["field_provenance"]["style_mix"]["scope"], "artist")
        track["album"] = "Other"
        self.assertEqual(_style_assignment(track, profile)["style_axes"]["heaviness"], 20)

    def test_configurable_coverage_floor_is_enforced(self):
        from test_workflow import minimal_analysis_packet
        packet = minimal_analysis_packet()
        packet["recommendation_policy"]["analysis_quality"] = {"min_classified_share": 1.1}
        with self.assertRaises(ContractError):
            build_agent_prompt(packet)


class CandidateRouteTests(unittest.TestCase):
    def test_reference_claims_cannot_inflate_unrelated_candidate_score(self):
        from test_hardening import pool_bundle
        pool, packet = pool_bundle()
        packet["recommendation_policy"]["ranking_weights"] = dict(DEFAULT_WEIGHTS)
        candidate = deepcopy(next(item for item in pool["candidate_pool"] if item["candidate_type"] == "style_neighbor"))
        candidate["analysis_refs"] = [packet["style_analysis"]["known_style_refs"][0]]
        before = score_candidate(candidate, packet)
        candidate["analysis_refs"] = packet["analysis_ref_ids"]
        self.assertEqual(score_candidate(candidate, packet), before)
        self.assertEqual(before["features"]["frequency_fit"], 0)

    def test_claimed_relation_requires_candidate_artist_at_catalog_endpoint(self):
        from test_hardening import pool_bundle
        from contracts import validate_recommendation_bundle
        pool, packet = pool_bundle()
        candidate = next(item for item in pool["candidate_pool"] if item["candidate_type"] == "musician_relation")
        candidate["artist"] = "Unrelated artist"
        with self.assertRaisesRegex(ContractError, "召回类型"):
            validate_recommendation_bundle(pool, packet)


class MultiInterestTests(unittest.TestCase):
    def assignments(self, values):
        return [{"track_key": f"track-{index:03d}", "title": f"Track {index}", "artist": f"Artist {value}",
                 "classification_status": "classified", "confidence": "high",
                 "style_mix": [{"style_ref": "style:alternative_rock" if value < 50 else "style:progressive_rock",
                                "role": "primary", "weight": 1}]}
                for index, value in enumerate(values)]

    def test_unrelated_tags_stay_in_separate_interests(self):
        from preference_model import build_interest_profiles, profile_distance
        assignments = self.assignments([0, 100])
        self.assertEqual(profile_distance(*assignments), 1)
        self.assertEqual(len(build_interest_profiles(assignments)), 2)

    def test_grouping_is_bounded_deterministic_and_excludes_unknown(self):
        from preference_model import build_interest_profiles
        assignments = self.assignments([0, 10, 30, 50, 70, 90, 100])
        expected = build_interest_profiles(assignments)
        self.assertEqual(expected, build_interest_profiles(list(reversed(assignments))))
        self.assertLessEqual(len(expected), 3)
        self.assertEqual(sum(item["track_count"] for item in expected), len(assignments))
        self.assertAlmostEqual(sum(item["share"] for item in expected), 1, places=5)
        assignments.append({"classification_status": "unclassified", "style_mix": []})
        self.assertEqual(expected, build_interest_profiles(assignments))

    def test_artist_bulk_does_not_dominate_linearly(self):
        from preference_model import build_interest_profiles
        profiles = build_interest_profiles(self.assignments([0] * 9 + [100]))
        self.assertAlmostEqual(profiles[0]["share"], 0.75)
        self.assertAlmostEqual(profiles[1]["share"], 0.25)

    def test_tampered_saved_interests_are_rejected(self):
        from preference_model import MODEL_CONFIG, build_interest_profiles
        from test_workflow import minimal_analysis_packet
        packet = minimal_analysis_packet()
        packet["style_analysis"]["interest_model"] = dict(MODEL_CONFIG)
        packet["style_analysis"]["interest_profiles"] = build_interest_profiles(packet["track_style_assignments"])
        validate_analysis_packet(packet)
        packet["style_analysis"]["interest_profiles"][0]["style_mix"][0]["weight"] += 0.01
        with self.assertRaisesRegex(ContractError, "兴趣分组"):
            validate_analysis_packet(packet)

    def test_fallback_interests_are_packet_scoped_bounded_and_not_persisted(self):
        from preference_model import _INTEREST_CACHE_LIMIT, build_interest_profiles, interest_profiles

        packet = {"track_style_assignments": self.assignments([0, 100]), "style_analysis": {}}
        original = deepcopy(packet)
        with patch("preference_model._interest_cache", OrderedDict()) as cache, \
             patch("preference_model.build_interest_profiles", wraps=build_interest_profiles) as build:
            first = interest_profiles(packet)
            self.assertIs(interest_profiles(packet), first)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(packet, original, "缓存不得写入任何 packet/JSON 字段")

            other = deepcopy(packet)
            self.assertEqual(interest_profiles(other), first)
            self.assertIsNot(interest_profiles(other), first, "不同任务不得共享可变画像对象")
            self.assertEqual(build.call_count, 2)

            # Replacing assignments on one packet invalidates its fallback.
            packet["track_style_assignments"] = self.assignments([30, 70])
            self.assertIsNot(interest_profiles(packet), first)
            self.assertEqual(build.call_count, 3)

            for index in range(_INTEREST_CACHE_LIMIT + 2):
                interest_profiles({"track_style_assignments": self.assignments([index]), "style_analysis": {}})
            self.assertLessEqual(len(cache), _INTEREST_CACHE_LIMIT)
            self.assertTrue(all(id(entry[0]) == key for key, entry in cache.items()))


if __name__ == "__main__":
    unittest.main()
