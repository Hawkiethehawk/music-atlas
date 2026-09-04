from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts import (
    ContractError,
    track_key,
    validate_feedback_log,
    validate_feedback_log_refs,
    validate_feedback_record,
)
from feedback import feedback_outcome_counts, load_feedback_log, match_feedback_to_bundle, new_feedback_record, save_feedback_log
from test_workflow import candidate_fixture, minimal_analysis_packet
from recommender import rank_bundle


def _ranked_bundle() -> dict:
    packet = minimal_analysis_packet()
    candidates = [
        candidate_fixture(packet, index, candidate_type)
        for index, candidate_type in enumerate(
            ("artist_continuation", "musician_relation", "style_neighbor", "exploration"),
            1,
        )
    ]
    bundle = {
        "schema_version": "2.0",
        "bundle_type": "recommendation_bundle",
        "bundle_stage": "candidate_pool",
        "status": "ready",
        "analysis_id": "analysis-test",
        "generated_at": "2026-01-01T00:00:00Z",
        "candidate_pool": candidates,
        "recommendations": [],
    }
    return rank_bundle(bundle, packet)


class FeedbackContractTests(unittest.TestCase):
    def test_feedback_record_requires_outcome_timestamp_and_ids(self) -> None:
        record = new_feedback_record(
            analysis_id="analysis-a",
            recommendation_id="musicbrainz:id-1",
            outcome="saved",
        )
        validate_feedback_record(record)
        self.assertEqual(record["schema_version"], "2.0")
        self.assertEqual(record["record_type"], "recommendation_feedback")
        with self.assertRaises(ContractError):
            validate_feedback_record({**record, "outcome": "unsubscribed"})
        with self.assertRaises(ContractError):
            validate_feedback_record({**record, "recommendation_id": ""})

    def test_feedback_log_is_an_array_of_valid_records(self) -> None:
        log = [
            new_feedback_record(analysis_id="a", recommendation_id="r1", outcome="saved"),
            new_feedback_record(analysis_id="a", recommendation_id="r2", outcome="skipped"),
        ]
        validate_feedback_log(log)
        self.assertEqual(feedback_outcome_counts(log), {"hidden": 0, "replayed": 0, "saved": 1, "skipped": 1})
        with self.assertRaises(ContractError):
            validate_feedback_log(log + [{"not": "a record"}])

    def test_feedback_log_save_and_load_roundtrip(self) -> None:
        log = [
            new_feedback_record(analysis_id="a", recommendation_id="r1", outcome="replayed"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feedback.json"
            save_feedback_log(log, path)
            reloaded = load_feedback_log(path)
        self.assertEqual(reloaded, log)

    def test_feedback_log_refs_matches_ranked_bundle_and_reports_unmatched(self) -> None:
        bundle = _ranked_bundle()
        packet = minimal_analysis_packet()
        canonical_ids = [item["canonical_track_id"] for item in bundle["recommendations"]]
        log = [
            new_feedback_record(
                analysis_id="analysis-test",
                recommendation_id=canonical_ids[0],
                outcome="saved",
            ),
            new_feedback_record(
                analysis_id="analysis-test",
                recommendation_id="musicbrainz:not-ranked",
                outcome="skipped",
            ),
            new_feedback_record(
                analysis_id="analysis-other",
                recommendation_id=canonical_ids[0],
                outcome="hidden",
            ),
        ]
        result = validate_feedback_log_refs(log, bundle, packet)
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(len(result["unmatched"]), 2)
        reasons = {item["reason"] for item in result["unmatched"]}
        self.assertEqual(reasons, {"recommendation_not_in_ranked_bundle", "analysis_id_mismatch"})

    def test_match_feedback_never_alters_ranking_policy(self) -> None:
        bundle = _ranked_bundle()
        packet = minimal_analysis_packet()
        canonical_ids = [item["canonical_track_id"] for item in bundle["recommendations"]]
        log = [
            new_feedback_record(analysis_id="analysis-test", recommendation_id=canonical_ids[0], outcome="saved"),
            new_feedback_record(analysis_id="analysis-test", recommendation_id=canonical_ids[1], outcome="hidden"),
        ]
        before = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        matched = match_feedback_to_bundle(log, bundle, packet)
        after = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)
        self.assertEqual(matched["matched_count"], 2)
        self.assertIn("saved", matched["outcome_counts"])


if __name__ == "__main__":
    unittest.main()
