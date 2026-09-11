from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_prompt import build_agent_prompt, prepare_agent_context
from agent_runner import run_agent
from reports import render_report
from contracts import ContractError, read_json, write_json
from recommender import rank_bundle
from research import ResearchExhausted, ResearchFailure
from test_hardening import pool_bundle


class StagedResearchTests(unittest.TestCase):
    def setup_pool(self, root):
        pool, packet = pool_bundle()
        for candidate in pool["candidate_pool"]:
            candidate.pop("explanation")
        write_json(root / "analysis.json", packet)
        return pool, packet

    def execute(self, root, **options):
        return run_agent(root / "analysis.json", prompt_path=root / "prompt.md", output_path=root / "bundle.json",
                         report_output_path=root / "report.txt", command="fixture", mock=False,
                         timeout=10, **options)

    def test_missing_routes_are_supplemented_and_only_selected_tracks_are_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool, packet = self.setup_pool(root)
            first, second = deepcopy(pool), deepcopy(pool)
            first["candidate_pool"], second["candidate_pool"] = pool["candidate_pool"][:2], pool["candidate_pool"][2:]
            with patch("agent_runner.run_external_agent", side_effect=[first, second]) as agent:
                summary = self.execute(root, candidate_target=8)
            self.assertEqual(summary["research_rounds"], 2)
            supplemental = agent.call_args_list[1].args[1]
            payload = json.loads(supplemental.rsplit("\n```json\n", 1)[1].rsplit("\n```", 1)[0])
            self.assertEqual(payload["research_request"]["round"], 2)
            self.assertEqual(len(payload["research_request"]["exclude_canonical_ids"]), 2)
            ranked = read_json(root / "bundle.json")
            self.assertEqual(len(ranked["recommendations"]), packet["recommendation_policy"]["target_recommendations"])
            self.assertTrue(all("program_explanation" not in item for item in ranked["candidate_pool"]))
            self.assertTrue(all("program_explanation" in item for item in ranked["recommendations"]))
            report = read_json(root / "bundle.research.json")
            self.assertEqual(report["final_explanations_generated"], len(ranked["recommendations"]))
            self.assertGreater(report["input_characters_total"], summary["prompt_characters"])

    def test_candidate_missing_style_evidence_is_dropped_not_fatal(self):
        """缺 track_identity/style 证据的候选被程序丢弃，只记录原因，不让整轮研究失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool, packet = self.setup_pool(root)
            broken = deepcopy(pool)
            target = broken["candidate_pool"][0]
            target["evidence_items"] = [
                item for item in target["evidence_items"] if item.get("claim_type") != "style"
            ]
            target["sources"] = [item["url"] for item in target["evidence_items"]]

            with patch("agent_runner.run_external_agent", side_effect=lambda *a, **k: deepcopy(broken)):
                with self.assertRaises(ResearchExhausted):
                    self.execute(root, candidate_target=8, max_candidates=8, max_research_rounds=1)

            report = read_json(root / "bundle.research.json")
            rejected = report.get("rejected_candidates") or []
            self.assertEqual(len(rejected), 1, "应恰好丢弃缺证据的那一个候选")
            self.assertIn("style", rejected[0]["reason"])

    def test_parallel_recommendation_workers_merge_before_program_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool, packet = self.setup_pool(root)
            with patch("agent_runner.run_external_agent", return_value=pool) as agent:
                summary = self.execute(root, recommendation_parallelism=4)
            self.assertEqual(agent.call_count, 4)
            self.assertEqual(summary["recommendation_parallelism"], 4)
            self.assertEqual(summary["research_rounds"], 1)
            report = read_json(root / "bundle.research.json")
            self.assertEqual(report["parallelism"], 4)
            self.assertEqual(report["rounds"][0]["worker_count"], 4)
            self.assertEqual(len(read_json(root / "bundle.json")["recommendations"]), packet["recommendation_policy"]["target_recommendations"])

    def test_parallel_recommendation_reports_round_and_worker_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool, _ = self.setup_pool(root)
            progress = []
            with patch("agent_runner.run_external_agent", return_value=pool):
                self.execute(root, recommendation_parallelism=4, progress=progress.append)
            self.assertEqual(len([event for event in progress if event["event"] == "round_started"]), 1)
            self.assertEqual(len([event for event in progress if event["event"] == "task_started"]), 4)
            completed = [event for event in progress if event["event"] == "task_completed"]
            self.assertEqual(len(completed), 4)
            self.assertEqual({event["parallel_slots"] for event in completed}, {4})
            self.assertEqual(len([event for event in progress if event["event"] == "round_completed"]), 1)

    def test_stagnation_stops_at_round_budget_without_success_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool, _ = self.setup_pool(root)
            pool["candidate_pool"] = pool["candidate_pool"][:2]
            with patch("agent_runner.run_external_agent", return_value=pool) as agent:
                with self.assertRaises(ResearchExhausted):
                    self.execute(root, candidate_target=8, max_research_rounds=2)
            self.assertEqual(agent.call_count, 2)
            self.assertFalse((root / "bundle.json").exists())
            self.assertFalse((root / "report.txt").exists())
            self.assertEqual(read_json(root / "bundle.research.json")["status"], "budget_exhausted")

    def test_supplemental_payload_cannot_escape_hard_context_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool, packet = self.setup_pool(root)
            empty_slots = root / "empty"
            empty_slots.mkdir()
            budget = len(build_agent_prompt(packet, empty_slots))
            prepare_agent_context(packet, root / "prompt.md", prompt_dir=empty_slots, context_budget=budget)
            pool["candidate_pool"] = pool["candidate_pool"][:2]
            with patch("agent_runner.run_external_agent", return_value=pool) as agent:
                with self.assertRaises(ResearchExhausted):
                    self.execute(root, candidate_target=8)
            self.assertEqual(agent.call_count, 1)

    def test_agent_failure_writes_diagnostic_and_preserves_existing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool, _ = self.setup_pool(root)
            pool["candidate_pool"] = pool["candidate_pool"][:2]
            (root / "bundle.json").write_text("existing bundle", encoding="utf-8")
            (root / "report.txt").write_text("existing report", encoding="utf-8")
            with patch("agent_runner.run_external_agent", side_effect=[pool, ContractError("Agent 执行超时：10 秒")]):
                with self.assertRaises(ResearchFailure):
                    self.execute(root, candidate_target=8)
            report = read_json(root / "bundle.research.json")
            self.assertEqual(report["status"], "agent_failed")
            self.assertEqual(report["accepted_candidate_count"], 2)
            self.assertEqual(len(report["rounds"]), 2)
            self.assertEqual(report["rounds"][1]["status"], "failed")
            self.assertIsNone(report["rounds"][1]["output_characters"])
            self.assertEqual(report["input_characters_total"], sum(item["input_characters"] for item in report["rounds"]))
            self.assertEqual(report["research_timeout_seconds"], 10)
            self.assertEqual(report["final_explanations_generated"], 0)
            self.assertNotIn("ranked_bundle_sha256", report)
            self.assertEqual((root / "bundle.json").read_text(encoding="utf-8"), "existing bundle")
            self.assertEqual((root / "report.txt").read_text(encoding="utf-8"), "existing report")

    def test_invalid_research_limits_never_call_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_pool(root)
            for options in ({"max_research_rounds": 4}, {"max_candidates": 201}, {"candidate_target": 1}, {"max_research_rounds": True}):
                with self.subTest(options=options), patch("agent_runner.run_external_agent") as agent:
                    with self.assertRaises(ContractError):
                        self.execute(root, **options)
                    agent.assert_not_called()

    def test_renderer_rejects_tampered_program_explanation(self):
        pool, packet = pool_bundle()
        ranked = rank_bundle(pool, packet)
        ranked["recommendations"][0]["program_explanation"]["text"] = "This altered explanation is not generated from the selected facts."
        with self.assertRaises(ContractError):
            render_report(ranked, packet)


if __name__ == "__main__":
    unittest.main()
