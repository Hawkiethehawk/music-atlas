from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts import validate_feedback_log_refs, validate_recommendation_bundle
from evaluation import _outcome_by_track, evaluate_offline
from feedback import new_feedback_record
from recommender import rank_bundle
from test_workflow import candidate_fixture, minimal_analysis_packet
from tune import propose_tuning


def ranked_bundle() -> tuple[dict, dict]:
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
    ranked = rank_bundle(bundle, packet)
    validate_recommendation_bundle(ranked, packet)
    return ranked, packet


class OutcomeTimestampTests(unittest.TestCase):
    def test_mixed_timezone_feedback_picks_latest_record(self) -> None:
        bundle, packet = ranked_bundle()
        track_id = bundle["recommendations"][0]["canonical_track_id"]
        log = [
            new_feedback_record(
                analysis_id=packet["analysis_id"],
                recommendation_id=track_id,
                outcome="skipped",
                timestamp="2026-06-01T03:00:00Z",
            ),
            new_feedback_record(
                analysis_id=packet["analysis_id"],
                recommendation_id=track_id,
                outcome="saved",
                timestamp="2026-06-01T12:00:00+08:00",
            ),
        ]
        # 12:00+08:00（04:00Z）晚于 03:00Z，应取 saved；旧字符串比较会误选 skipped
        self.assertEqual(_outcome_by_track(bundle, log)[track_id.casefold()], "saved")
        report = evaluate_offline(bundle, packet, log)
        self.assertEqual(report["precision"]["accepted_count"], 1)

    def test_unparsable_timestamp_falls_back_to_string_compare(self) -> None:
        bundle, packet = ranked_bundle()
        track_id = bundle["recommendations"][0]["canonical_track_id"]
        # 两条时间戳都无法解析时，退回字符串比较："zzz" > "aaa"
        log = [
            new_feedback_record(
                analysis_id=packet["analysis_id"],
                recommendation_id=track_id,
                outcome="skipped",
                timestamp="2026-06-01 aaa",
            ),
            new_feedback_record(
                analysis_id=packet["analysis_id"],
                recommendation_id=track_id,
                outcome="saved",
                timestamp="2026-06-01 zzz",
            ),
        ]
        self.assertEqual(_outcome_by_track(bundle, log)[track_id.casefold()], "saved")

    def test_parsable_timestamp_beats_unparsable_one(self) -> None:
        bundle, packet = ranked_bundle()
        track_id = bundle["recommendations"][0]["canonical_track_id"]
        log = [
            new_feedback_record(
                analysis_id=packet["analysis_id"],
                recommendation_id=track_id,
                outcome="skipped",
                timestamp="2026-06-01T03:00:00Z",
            ),
            new_feedback_record(
                analysis_id=packet["analysis_id"],
                recommendation_id=track_id,
                outcome="saved",
                timestamp="not-a-timestamp",
            ),
        ]
        # 可解析时间戳比乱字符串更可信，排序优先于不可解析记录
        self.assertEqual(_outcome_by_track(bundle, log)[track_id.casefold()], "skipped")


class OfflineEvaluationTests(unittest.TestCase):
    def test_evaluation_reports_all_seven_metric_groups(self) -> None:
        bundle, packet = ranked_bundle()
        ids = [item["canonical_track_id"] for item in bundle["recommendations"]]
        feedback_log = [
            new_feedback_record(analysis_id="analysis-test", recommendation_id=ids[0], outcome="saved"),
            new_feedback_record(analysis_id="analysis-test", recommendation_id=ids[1], outcome="skipped"),
        ]
        report = evaluate_offline(bundle, packet, feedback_log)
        self.assertEqual(report["report_type"], "offline_evaluation")
        self.assertIn("precision", report)
        self.assertIn("novelty", report)
        self.assertIn("diversity", report)
        self.assertIn("calibration", report)
        self.assertIn("repetition", report)
        self.assertIn("sequence_quality", report)
        self.assertEqual(report["policy_changed"], False)
        self.assertEqual(report["precision"]["recommendation_count"], 4)
        self.assertEqual(report["precision"]["accepted_count"], 1)
        self.assertGreater(report["precision"]["precision"], 0)

    def test_evaluation_reports_unmatched_records_without_dropping_them(self) -> None:
        bundle, packet = ranked_bundle()
        feedback_log = [
            new_feedback_record(analysis_id="analysis-test", recommendation_id="musicbrainz:missing", outcome="saved"),
        ]
        report = evaluate_offline(bundle, packet, feedback_log)
        self.assertEqual(report["feedback"]["record_count"], 1)
        self.assertEqual(report["feedback"]["matched_count"], 0)
        self.assertEqual(report["feedback"]["unmatched_count"], 1)
        self.assertEqual(report["feedback"]["unmatched_reasons"]["recommendation_not_in_ranked_bundle"], 1)

    def test_evaluation_never_mutates_the_bundle(self) -> None:
        bundle, packet = ranked_bundle()
        ids = [item["canonical_track_id"] for item in bundle["recommendations"]]
        feedback_log = [
            new_feedback_record(analysis_id="analysis-test", recommendation_id=ids[0], outcome="saved"),
        ]
        before = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        evaluate_offline(bundle, packet, feedback_log)
        self.assertEqual(before, json.dumps(bundle, ensure_ascii=False, sort_keys=True))

    def test_tuning_proposal_requires_human_approval_and_never_applies(self) -> None:
        bundle, packet = ranked_bundle()
        ids = [item["canonical_track_id"] for item in bundle["recommendations"]]
        feedback_log = [
            new_feedback_record(analysis_id="analysis-test", recommendation_id=ids[0], outcome="saved"),
            new_feedback_record(analysis_id="analysis-test", recommendation_id=ids[1], outcome="saved"),
            new_feedback_record(analysis_id="analysis-test", recommendation_id=ids[2], outcome="skipped"),
        ]
        rectify = validate_feedback_log_refs(feedback_log, bundle, packet)
        report = evaluate_offline(bundle, packet, feedback_log)
        feedback_by_track = {ids[0].casefold(): "saved", ids[1].casefold(): "saved", ids[2].casefold(): "skipped"}
        proposal = propose_tuning(report, packet, bundle=bundle, feedback_by_track=feedback_by_track)
        self.assertEqual(proposal["approval_required"], True)
        self.assertEqual(proposal["auto_applied"], False)
        self.assertEqual(proposal["artifact_type"], "policy_tuning_proposal")
        self.assertEqual(set(proposal["suggested_deltas"]["ranking_weights"]), {
            "style_fit",
            "relation_fit",
            "frequency_fit",
            "novelty",
            "evidence_quality",
            "public_association",
        })
        self.assertIn("how_to_apply", proposal)
        # 建议中的 delta 必须有界，避免自动漂移
        for delta in proposal["suggested_deltas"]["ranking_weights"].values():
            self.assertLessEqual(abs(delta), 0.05)


if __name__ == "__main__":
    unittest.main()
