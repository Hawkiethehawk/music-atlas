from __future__ import annotations

import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark import compare_listening_benchmark, prepare_listening_benchmark
from contracts import ContractError, read_json, stable_hash, write_json
from evaluation import evaluate_offline
from feedback import new_feedback_record
from recommender import rank_bundle
from test_hardening import invoke_cli, pool_bundle


class ListeningBenchmarkTests(unittest.TestCase):
    def setUp(self):
        pool, self.packet = pool_bundle()
        self.a = rank_bundle(pool, self.packet)
        alternate = deepcopy(pool)
        alternate["candidate_pool"].reverse()
        self.b = rank_bundle(alternate, self.packet)
        self.labels = prepare_listening_benchmark(self.packet, self.a, self.packet, self.b)

    def compare(self, labels=None, **kwargs):
        return compare_listening_benchmark(self.packet, self.a, self.packet, self.b,
                                          self.labels if labels is None else labels, **kwargs)

    def test_blind_template_has_no_rank_variant_score_or_prefilled_verdict(self):
        for row in self.labels["judgments"]:
            self.assertIsNone(row["verdict"])
            self.assertFalse({"ranking_score", "variant", "rank", "program_explanation"} & set(row))

    def test_no_labels_do_not_become_rejections_or_a_winner(self):
        report = self.compare()
        self.assertEqual(report["status"], "incomplete_labels")
        self.assertIsNone(report["a"]["acceptance_rate"])
        self.assertIsNone(report["acceptance_rate_delta_b_minus_a"])
        self.assertIsNone(report["winner"])
        self.assertEqual(report["telemetry_a"]["status"], "unavailable")

    def test_complete_labels_produce_descriptive_metrics_without_policy_changes(self):
        before = deepcopy((self.packet, self.a, self.b))
        for index, row in enumerate(self.labels["judgments"]):
            row["verdict"] = "like" if index % 2 else "dislike"
            row["reasons"] = ["liked_texture" if index % 2 else "vocal_mismatch"]
        report = self.compare()
        self.assertEqual(report["status"], "descriptive_comparison")
        self.assertIsInstance(report["acceptance_rate_delta_b_minus_a"], float)
        self.assertEqual(report["a"]["feedback_coverage"], 1)
        self.assertFalse(report["policy_changed"])
        self.assertFalse(report["auto_applied"])
        self.assertTrue(report["approval_required"])
        self.assertEqual(before, (self.packet, self.a, self.b))

    def test_unsure_is_excluded_and_invalid_or_duplicate_labels_fail(self):
        for row in self.labels["judgments"]:
            row["verdict"] = "unsure"
        self.assertIsNone(self.compare()["a"]["acceptance_rate"])
        for change in ("duplicate", "verdict", "reason", "identity"):
            labels = deepcopy(self.labels)
            if change == "duplicate":
                labels["judgments"].append(deepcopy(labels["judgments"][0]))
            elif change == "verdict":
                labels["judgments"][0]["verdict"] = "probably"
            elif change == "reason":
                labels["judgments"][0]["reasons"] = ["not-a-reason"]
            else:
                labels["input_fingerprint"] = "other"
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.compare(labels)

    def test_different_input_or_reference_date_is_not_a_controlled_comparison(self):
        for field, value in (("as_of_date", "2026-02-01"), ("source_snapshot_id", "other"), ("preferred_artists", [])):
            packet = deepcopy(self.packet)
            packet[field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                prepare_listening_benchmark(self.packet, self.a, packet, self.b)

    def test_telemetry_is_optional_but_must_match_the_exact_bundle(self):
        report = {"artifact_type": "research_report", "analysis_id": self.packet["analysis_id"],
                  "ranked_bundle_sha256": stable_hash(self.a), "total_elapsed_ms": 100, "input_characters_total": 1000,
                  "candidate_target": 8, "max_candidates": 80, "max_rounds": 2, "research_timeout_seconds": 10, "rounds": [{}]}
        self.assertEqual(self.compare(report_a=report)["telemetry_a"]["status"], "measured")
        report["ranked_bundle_sha256"] = "wrong"
        with self.assertRaises(ContractError):
            self.compare(report_a=report)

    def test_rule_scores_are_not_reported_as_calibrated_probabilities(self):
        feedback = [new_feedback_record(analysis_id=self.packet["analysis_id"], recommendation_id=self.a["recommendations"][0]["canonical_track_id"], outcome="saved")]
        report = evaluate_offline(self.a, self.packet, feedback)
        self.assertIsNone(report["calibration"]["expected_calibration_error"])
        self.assertEqual(report["calibration"]["status"], "not_calibrated")
        self.assertEqual(sum(item["count"] for item in report["calibration"]["bins"]), 1)

    def test_cli_preserves_existing_labels_and_rejects_overwriting_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in (("analysis.json", self.packet), ("a.json", self.a), ("b.json", self.b)):
                write_json(root / name, value)
            args = ["--analysis-a", root / "analysis.json", "--bundle-a", root / "a.json", "--bundle-b", root / "b.json"]
            self.assertEqual(invoke_cli(["prepare-benchmark", *args, "--output", root / "labels.json"]), 0)
            before = (root / "labels.json").read_bytes()
            with self.assertRaises(ContractError):
                invoke_cli(["prepare-benchmark", *args, "--output", root / "labels.json"])
            with self.assertRaises(ContractError):
                invoke_cli(["benchmark", *args, "--judgments", root / "labels.json", "--output", root / "labels.json"])
            self.assertEqual(before, (root / "labels.json").read_bytes())
            self.assertEqual(invoke_cli(["benchmark", *args, "--judgments", root / "labels.json", "--output", root / "report.json"]), 0)
            self.assertEqual(read_json(root / "report.json")["status"], "incomplete_labels")


if __name__ == "__main__":
    unittest.main()
