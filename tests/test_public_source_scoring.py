"""The new recommendation path uses public tags, never inferred listening axes."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation import _sequence_quality_metrics
from preference_model import build_interest_profiles, profile_distance
from recommender import DEFAULT_WEIGHTS, _candidate_style_vector, _sequence_candidates, score_candidate


class PublicSourceScoringTests(unittest.TestCase):
    def packet(self):
        return {
            "as_of_date": "2026-09-24",
            "primary_distribution": [{"artist": "Known Artist", "entity_ref": "artist:a", "count": 2}],
            "credited_distribution": [], "preferred_artists": [], "entities": [],
            "style_analysis": {"interest_profiles": [], "style_definitions": []},
            "track_style_assignments": [
                {"track_key": "a::one", "artist": "Known Artist", "classification_status": "classified",
                 "confidence": "high", "style_refs": ["style:post_rock"]},
            ],
            "recommendation_policy": {
                "ranking_weights": DEFAULT_WEIGHTS,
                "sequence_policy": {"mode": "public_tag_continuity", "transition_weight": 0.55,
                                    "ranking_weight": 0.45, "allow_familiar_anchor": False,
                                    "prefer_adjacent_transitions": True},
            },
        }

    def candidate(self, *, scope="album", supported=True, ref="style:post_rock", ident="a"):
        url = "https://www.last.fm/music/Test/+tags"
        return {"canonical_track_id": ident, "artist": "New Artist", "title": ident,
                "project": "A", "candidate_type": "style_neighbor", "sources": [url],
                "evidence_items": [{"claim_type": "style", "url": url}],
                "style_evidence": {"status": "supported" if supported else "unavailable",
                                   "scope": scope, "url": url, "retrieved_at": "2026-09-24T00:00:00Z",
                                   "tags": [{"tag": "post-rock", "style_ref": ref}]},
                "ranking_score": 75.0}

    def test_public_scope_is_preserved_without_eight_axis(self):
        style_scores = []
        for scope in ("track", "album", "artist"):
            with self.subTest(scope=scope):
                candidate = self.candidate(scope=scope)
                self.assertEqual(_candidate_style_vector(candidate), {"style:post_rock": 1.0})
                scored = score_candidate(candidate, self.packet())
                self.assertGreater(scored["features"]["style_fit"], 0)
                self.assertEqual(set(scored["features"]), set(DEFAULT_WEIGHTS))
                self.assertNotIn("axis_fit", scored["features"])
                style_scores.append(scored["features"]["style_fit"])
        self.assertGreater(style_scores[0], style_scores[1])
        self.assertGreater(style_scores[1], style_scores[2])

    def test_no_source_makes_style_unavailable_not_zero_weighted(self):
        candidate = self.candidate()
        candidate["sources"] = []
        candidate["evidence_items"] = []
        self.assertEqual(_candidate_style_vector(candidate), {})
        scored = score_candidate(candidate, self.packet())
        self.assertIn("style_fit", scored["unavailable_features"])
        self.assertEqual(scored["breakdown"]["style_fit"], 0)

    def test_tag_distance_and_sequence_do_not_emit_energy(self):
        a, b, c = self.candidate(ident="a"), self.candidate(ident="b"), self.candidate(ident="c", ref="style:jazz")
        for item in (a, b, c):
            item["candidate_type"] = "exploration"
        ordered, manifest = _sequence_candidates([a, b, c], self.packet())
        self.assertEqual(manifest["mode"], "public_tag_continuity")
        self.assertEqual([item["canonical_track_id"] for item in ordered[:2]], ["a", "b"])
        self.assertTrue(all("sequence_energy" not in item for item in ordered))
        self.assertEqual(manifest["positions"][1]["transition_distance"], 0)
        metrics = _sequence_quality_metrics(ordered, {"sequence": manifest})
        self.assertNotIn("arc_conformance", metrics)
        self.assertGreater(metrics["supported_transition_share"], 0)

    def test_unknown_transition_is_not_reported_as_a_distance(self):
        known, unknown = self.candidate(ident="known"), self.candidate(ident="unknown")
        unknown["sources"], unknown["evidence_items"] = [], []
        _, manifest = _sequence_candidates([known, unknown], self.packet())
        self.assertIsNone(manifest["positions"][1]["transition_distance"])
        metrics = _sequence_quality_metrics([known, unknown], {"sequence": manifest})
        self.assertIsNone(metrics["mean_supported_tag_distance"])
        self.assertEqual(metrics["supported_transition_share"], 0)

    def test_unmatched_tags_split_without_audio_axis(self):
        assignments = [
            {"track_key": "one", "artist": "A", "classification_status": "classified", "confidence": "high",
             "style_refs": ["style:post_rock"]},
            {"track_key": "two", "artist": "B", "classification_status": "classified", "confidence": "high",
             "style_refs": ["style:jazz"]},
        ]
        self.assertEqual(profile_distance(assignments[0], assignments[1]), 1.0)
        profiles = build_interest_profiles(assignments)
        self.assertEqual(len(profiles), 2)
        self.assertTrue(all("style_axes" not in item for item in profiles))


if __name__ == "__main__":
    unittest.main()
