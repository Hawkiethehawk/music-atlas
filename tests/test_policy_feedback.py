from __future__ import annotations

# 测试夹具曲目是合成数据，平台上不存在；关闭平台元数据核验，
# 核验逻辑本身由 tests/test_metadata_verify.py 与专门用例覆盖。
import os as _atlas_os
_atlas_os.environ.setdefault("ATLAS_METADATA_VERIFY", "off")

import itertools
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts import ContractError, read_json, stable_hash, validate_recommendation_bundle, write_json
from evaluation import evaluate_offline
from feedback import latest_feedback_outcomes, new_feedback_record
from musician_analyzer import DEFAULT_POLICY, analyze_snapshot, load_recommendation_policy
from recommender import rank_bundle
from source_adapters import build_snapshot
from test_hardening import invoke_cli, pool_bundle
from tune import propose_tuning


class FeedbackTuningTests(unittest.TestCase):
    def setUp(self):
        pool, self.packet = pool_bundle()
        self.bundle = rank_bundle(pool, self.packet)
        self.ids = [item["canonical_track_id"] for item in self.bundle["recommendations"]]

    def record(self, index, outcome, timestamp="2026-06-01T00:00:00Z", analysis_id=None):
        return new_feedback_record(analysis_id=analysis_id or self.packet["analysis_id"],
                                   recommendation_id=self.ids[index], outcome=outcome, timestamp=timestamp)

    def proposal(self, log):
        report = evaluate_offline(self.bundle, self.packet, log)
        return propose_tuning(report, self.packet, bundle=self.bundle,
                              feedback_by_track=latest_feedback_outcomes(self.bundle, log))

    def test_latest_matching_feedback_is_order_independent(self):
        records = [self.record(0, "saved", "2026-06-01T12:00:00+08:00"),
                   self.record(0, "hidden", "2026-06-01T03:00:00"),
                   self.record(0, "hidden", "2026-07-01T00:00:00Z", "other-analysis")]
        for ordered in itertools.permutations(records):
            outcomes = latest_feedback_outcomes(self.bundle, list(ordered))
            self.assertEqual(outcomes, {self.ids[0].casefold(): "saved"})
            report = evaluate_offline(self.bundle, self.packet, list(ordered))
            self.assertEqual(report["precision"]["accepted_count"], 1)
            self.assertEqual(report["feedback"]["unmatched_count"], 1)
            self.assertEqual(self.proposal(list(ordered))["feedback_sample"]["accepted_count"], 1)

    def test_equal_instants_have_stable_tie_breaking(self):
        records = [self.record(0, "saved", "2026-06-01T08:00:00+08:00"), self.record(0, "hidden")]
        self.assertEqual(latest_feedback_outcomes(self.bundle, records),
                         latest_feedback_outcomes(self.bundle, list(reversed(records))))

    def test_empty_unmatched_and_single_outcome_groups_propose_no_changes(self):
        logs = [[], [self.record(0, "saved", analysis_id="other-analysis")],
                [self.record(0, "saved")], [self.record(0, "hidden")]]
        for log in logs:
            with self.subTest(log=log):
                proposal = self.proposal(log)
                self.assertEqual(proposal["status"], "insufficient_feedback")
                self.assertEqual(proposal["suggested_deltas"],
                                 {"ranking_weights": {}, "recall_mix": {}, "sequence_weights": {}})
                self.assertEqual(proposal["suggested_caps"], {})
                self.assertTrue(proposal["approval_required"])
                self.assertFalse(proposal["auto_applied"])

    def test_unobserved_recommendations_are_not_rejected(self):
        proposal = self.proposal([self.record(0, "saved"), self.record(1, "skipped")])
        feature = proposal["observations"]["feature_acceptance"]["style_fit"]
        self.assertEqual((feature["accepted_count"], feature["rejected_count"]), (1, 1))
        self.assertEqual(feature["rejected_mean"], self.bundle["recommendations"][1]["score_features"]["style_fit"])
        self.assertEqual(proposal["feedback_sample"]["rejected_count"], 1)
        type_stats = proposal["observations"]["candidate_type_acceptance"]
        self.assertEqual(sum(stats["covered_count"] for stats in type_stats.values()), 2)
        self.assertEqual(sum(stats["accepted_count"] for stats in type_stats.values()), 1)

    def test_calibration_uses_only_observed_recommendations(self):
        report = evaluate_offline(self.bundle, self.packet, [self.record(0, "saved")])
        self.assertEqual(sum(entry["count"] for entry in report["calibration"]["bins"]), 1)

    def test_tune_cli_and_evaluation_use_identical_outcomes(self):
        log = [self.record(0, "saved", "2026-06-02T00:00:00Z"), self.record(1, "skipped"),
               self.record(0, "hidden"), self.record(0, "hidden", analysis_id="other-analysis")]
        expected = self.proposal(log)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "analysis.json", self.packet)
            write_json(root / "bundle.json", self.bundle)
            write_json(root / "feedback.json", log)
            self.assertEqual(invoke_cli([
                "tune", "--analysis", root / "analysis.json", "--bundle", root / "bundle.json",
                "--feedback", root / "feedback.json", "--output", root / "proposal.json",
            ]), 0)
            actual = read_json(root / "proposal.json")
            self.assertEqual(expected["feedback_sample"], actual["feedback_sample"])
            self.assertEqual(expected["observations"], actual["observations"])
            self.assertEqual(expected["suggested_deltas"], actual["suggested_deltas"])

    def test_tune_cli_uses_ranked_tracks_when_given_a_candidate_pool(self):
        pool, _ = pool_bundle(self.packet)
        log = [self.record(0, "saved"), self.record(1, "skipped")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "analysis.json", self.packet)
            write_json(root / "pool.json", pool)
            write_json(root / "feedback.json", log)
            invoke_cli(["tune", "--analysis", root / "analysis.json", "--bundle", root / "pool.json",
                        "--feedback", root / "feedback.json", "--output", root / "proposal.json"])
            self.assertEqual(read_json(root / "proposal.json")["feedback_sample"], self.proposal(log)["feedback_sample"])

    def test_evaluation_rejects_forged_ranked_scores(self):
        forged = deepcopy(self.bundle)
        for candidate in forged["recommendations"]:
            candidate["ranking_score"] = 0
            candidate["score_features"] = dict.fromkeys(candidate["score_features"], 0)
            candidate["score_breakdown"] = dict.fromkeys(candidate["score_breakdown"], 0)
        validate_recommendation_bundle(forged, self.packet)
        with self.assertRaises(ContractError):
            evaluate_offline(forged, self.packet, [])


class ManualPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ROOT / "tests" / "fixtures" / "playlist_sample.json"
        self.profiles = ROOT / "styles" / "artist_style_profiles.example.json"

    def test_default_policy_is_a_deep_copy(self):
        original = deepcopy(DEFAULT_POLICY)
        result = load_recommendation_policy()
        self.assertEqual(result, original)
        result["ranking_weights"]["style_fit"] = 0
        result["recall_mix"][0]["target_ratio"] = 0
        self.assertEqual(DEFAULT_POLICY, original)

    def test_partial_policy_merges_nested_maps_and_replaces_lists(self):
        original = deepcopy(DEFAULT_POLICY)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            mix = [{"candidate_type": item["candidate_type"], "target_ratio": 0.25}
                   for item in DEFAULT_POLICY["recall_mix"]]
            write_json(path, {"max_per_artist": 1, "ranking_weights": {"style_fit": 0.25, "axis_fit": 0.25},
                              "sequence_policy": {"allow_familiar_anchor": False}, "recall_mix": mix})
            policy = load_recommendation_policy(path)
            self.assertEqual(policy["max_per_artist"], 1)
            self.assertEqual(policy["ranking_weights"]["style_fit"], 0.25)
            self.assertEqual(policy["ranking_weights"]["relation_fit"], 0.15)
            self.assertFalse(policy["sequence_policy"]["allow_familiar_anchor"])
            self.assertEqual(policy["recall_mix"], mix)
            self.assertEqual(DEFAULT_POLICY, original)

    def test_invalid_unknown_and_boundary_changing_policies_are_rejected(self):
        invalid = [[], {"unknown": 1}, {"max_per_artist": True}, {"max_per_artist": 0},
                   {"max_per_project": 1.5}, {"min_projects": 11}, {"candidate_pool_min": 0},
                   {"ranking_weights": {"unknown": 0.1}}, {"ranking_weights": {"style_fit": 0.9}},
                   {"ranking_weights": {"style_fit": float("nan")}}, {"ranking_weights": []},
                   {"recall_mix": DEFAULT_POLICY["recall_mix"][:2]}, {"target_recommendations": 12},
                   {"exclude_current_favorites": False}, {"source_policy": {}},
                   {"sequence_policy": {"mode": "random"}}, {"sequence_policy": {"arc_weight": 0.9}},
                   {"sequence_policy": {"allow_familiar_anchor": "false"}},
                   {"artifact_type": "policy_tuning_proposal", "approval_required": True}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            for overrides in invalid:
                with self.subTest(overrides=overrides):
                    write_json(path, overrides)
                    with self.assertRaises(ContractError):
                        load_recommendation_policy(path)

    def test_policy_changes_analysis_identity_and_manifest_not_shared_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "snapshot.json", build_snapshot(
                self.fixture, reader_name="local_json", platform="apple_music", playlist_id="fixture", playlist_name="fixture"))
            options = dict(preferred_path=ROOT / "preferred_artists.txt", relation_path=ROOT / "relations" / "artist_relations.json",
                           style_profile_path=self.profiles, output_path=root / "analysis.json", manifest_path=root / "manifest.json")
            default = analyze_snapshot(root / "snapshot.json", **options)
            write_json(root / "policy.json", {})
            unchanged = analyze_snapshot(root / "snapshot.json", **options, policy_path=root / "policy.json")
            self.assertEqual(default["analysis_id"], unchanged["analysis_id"])
            write_json(root / "policy.json", {"max_per_artist": 1})
            custom = analyze_snapshot(root / "snapshot.json", **options, policy_path=root / "policy.json")
            self.assertNotEqual(default["analysis_id"], custom["analysis_id"])
            self.assertEqual(custom["recommendation_policy"]["max_per_artist"], 1)
            summary = read_json(root / "manifest.json")["policy"]
            self.assertEqual(summary["sha256"], stable_hash(custom["recommendation_policy"]))
            self.assertEqual(summary["mode"], "explicit")
            custom["recommendation_policy"]["ranking_weights"]["style_fit"] = 0
            self.assertEqual(DEFAULT_POLICY["ranking_weights"]["style_fit"], 0.3)

    def test_analyze_and_run_cli_accept_explicit_policy_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "policy.json", {"max_per_artist": 1})
            self.assertEqual(invoke_cli([
                "run", "--analysis-mode", "catalog", "--input", self.fixture, "--reader", "local_json", "--runtime-dir", root / "run",
                "--style-profiles", self.profiles, "--policy-file", root / "policy.json",
            ]), 0)
            packet = read_json(root / "run" / "musician_analysis.json")
            self.assertEqual(packet["recommendation_policy"]["max_per_artist"], 1)
            self.assertEqual(read_json(root / "run" / "pipeline_manifest.json")["policy"]["sha256"],
                             stable_hash(packet["recommendation_policy"]))
            self.assertEqual(invoke_cli([
                "analyze", "--analysis-mode", "catalog", "--snapshot", root / "run" / "snapshot.json", "--output", root / "analysis.json",
                "--markdown", root / "analysis.md", "--manifest", root / "manifest.json",
                "--style-profiles", self.profiles, "--policy-file", root / "policy.json",
            ]), 0)
            self.assertEqual(read_json(root / "analysis.json")["analysis_id"], packet["analysis_id"])

    def test_invalid_policy_does_not_write_an_analysis_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "policy.json", {"unknown": 1})
            with self.assertRaises(ContractError):
                invoke_cli(["run", "--input", self.fixture, "--runtime-dir", root / "run",
                            "--style-profiles", self.profiles, "--policy-file", root / "policy.json"])
            self.assertFalse((root / "run" / "musician_analysis.json").exists())
            self.assertFalse((root / "run" / "agent_prompt.md").exists())


if __name__ == "__main__":
    unittest.main()
