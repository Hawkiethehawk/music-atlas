from __future__ import annotations

# 测试夹具曲目是合成数据，平台上不存在；关闭平台元数据核验，
# 核验逻辑本身由 tests/test_metadata_verify.py 与专门用例覆盖。
import os as _atlas_os
_atlas_os.environ.setdefault("ATLAS_METADATA_VERIFY", "off")

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contracts import read_json, write_json
from feedback import new_feedback_record


class PipelineCliTests(unittest.TestCase):
    def cli(self, *arguments, expected=0):
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": str(len(arguments))}
        completed = subprocess.run(
            [sys.executable, str(ROOT / "workflow.py"), *map(str, arguments)],
            cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        return json.loads(completed.stdout) if completed.stdout.strip() else None

    def test_local_fixture_pipeline_and_failure_outputs(self):
        with tempfile.TemporaryDirectory(prefix="atlas pipeline ") as directory:
            root = Path(directory)
            run = root / "current"
            summary = self.cli(
                "run", "--analysis-mode", "catalog", "--input", ROOT / "tests/fixtures/playlist_sample.json", "--reader", "local_json",
                "--platform", "apple_music", "--playlist-id", "sample", "--playlist-name", "fixture",
                "--style-profiles", ROOT / "styles/artist_style_profiles.example.json", "--runtime-dir", run,
            )
            self.assertEqual(summary["source_track_count"], 3)
            analysis = run / "musician_analysis.json"
            packet = read_json(analysis)
            original_policy = deepcopy(packet["recommendation_policy"])
            agent_argv = [sys.executable, str(ROOT / "tests/fixtures/fake_agent.py")]
            agent_command = subprocess.list2cmdline(agent_argv) if os.name == "nt" else shlex.join(agent_argv)
            bundle = root / "bundle.json"
            self.cli("agent", "--analysis", analysis, "--prompt", run / "agent_prompt.md",
                     "--output", bundle, "--report-output", root / "agent-report.txt", "--command", agent_command)
            self.assertEqual(len(read_json(bundle)["recommendations"]), 10)
            ranked = root / "ranked.json"
            self.cli("validate", "--analysis", analysis, "--bundle", bundle, "--ranked-output", ranked,
                     "--output", root / "report.txt")
            self.assertEqual(read_json(bundle), read_json(ranked))
            web_payload_path = root / "web_payload.json"
            web_summary = self.cli(
                "web-export",
                "--runtime-dir",
                run,
                "--bundle",
                bundle,
                "--output",
                web_payload_path,
            )
            self.assertEqual(web_summary["status"], "web_payload_written")
            web_payload = read_json(web_payload_path)
            self.assertEqual(web_payload["payload_type"], "music_atlas_web")
            self.assertEqual(web_payload["source"]["trackCount"], 3)
            self.assertEqual(len(web_payload["recommendations"]), 10)
            self.assertEqual(web_payload["status"]["publication"], "draft")
            ids = [item["canonical_track_id"] for item in read_json(ranked)["recommendations"]]
            feedback = root / "feedback.json"
            write_json(feedback, [new_feedback_record(analysis_id=packet["analysis_id"], recommendation_id=track_id,
                                                      outcome=outcome, timestamp="2026-06-01T00:00:00Z")
                                  for track_id, outcome in zip(ids, ("saved", "skipped", "replayed"))])
            report = root / "evaluation.json"
            self.cli("evaluate", "--analysis", analysis, "--bundle", ranked, "--feedback", feedback, "--output", report)
            self.assertEqual(read_json(report)["precision"]["accepted_count"], 2)
            self.assertFalse(read_json(report)["policy_changed"])
            proposal = root / "proposal.json"
            self.cli("tune", "--analysis", analysis, "--bundle", ranked, "--feedback", feedback, "--output", proposal)
            self.assertTrue(read_json(proposal)["approval_required"])
            self.assertFalse(read_json(proposal)["auto_applied"])
            self.assertEqual(read_json(proposal)["feedback_sample"]["rejected_count"], 1)
            self.assertEqual(read_json(analysis)["recommendation_policy"], original_policy)
            audit = root / "audit.json"
            failed = self.cli("validate", "--analysis", analysis, "--bundle", bundle,
                              "--ranked-output", root / "audit-ranked.json", "--output", root / "audit-report.txt",
                              "--evidence-audit", audit, expected=2)
            self.assertEqual(failed["status"], "evidence_audit_failed")
            self.assertEqual(read_json(audit)["accepted_count"], 0)
            self.assertEqual(read_json(audit)["recommendation_count"], 10)
            self.assertFalse((root / "audit-ranked.json").exists())
            self.assertFalse((root / "audit-report.txt").exists())
            forged = read_json(bundle)
            forged["recommendations"][0]["title"] = "forged title"
            write_json(root / "forged.json", forged)
            self.cli("validate", "--analysis", analysis, "--bundle", root / "forged.json",
                     "--ranked-output", root / "forged-ranked.json", "--output", root / "forged-report.txt",
                     "--evidence-audit", root / "forged-audit.json", expected=2)
            self.assertEqual(read_json(root / "forged-audit.json")["status"], "invalid_contract")
            self.assertFalse((root / "forged-ranked.json").exists())
            self.assertFalse((root / "forged-report.txt").exists())

    def test_csv_count_mismatch_stops_before_step_two(self):
        with tempfile.TemporaryDirectory(prefix="atlas csv ") as directory:
            root = Path(directory)
            (root / "sample.csv").write_text("title,artist\nFixture,Artist\n", encoding="utf-8")
            write_json(root / "count.json", {"declared_track_count": 100})
            self.cli("run", "--input", root / "sample.csv", "--reader", "csv",
                     "--declared-count-file", root / "count.json", "--runtime-dir", root / "run", expected=2)
            self.assertEqual(read_json(root / "run/snapshot.json")["reader_status"], "incomplete")
            self.assertFalse((root / "run/musician_analysis.json").exists())

    def test_staged_research_and_listening_comparison_commands(self):
        with tempfile.TemporaryDirectory(prefix="atlas listening ") as directory:
            root = Path(directory)
            self.cli("run", "--analysis-mode", "catalog", "--input", ROOT / "tests/fixtures/playlist_sample.json", "--reader", "local_json",
                     "--style-profiles", ROOT / "styles/artist_style_profiles.example.json", "--runtime-dir", root,
                     "--as-of-date", "2026-09-05", "--context-budget", "100000")
            analysis, prompt = root / "musician_analysis.json", root / "agent_prompt.md"
            agent_argv = [sys.executable, str(ROOT / "tests/fixtures/fake_agent.py")]
            command = subprocess.list2cmdline(agent_argv) if os.name == "nt" else shlex.join(agent_argv)
            prompt_before = prompt.read_bytes()
            for name, target, rounds in (("a", 20, 1), ("b", 40, 2)):
                summary = self.cli("agent", "--analysis", analysis, "--prompt", prompt, "--command", command,
                                   "--output", root / f"{name}.json", "--report-output", root / f"{name}.txt",
                                   "--candidate-target", target, "--max-research-rounds", 2)
                self.assertEqual(summary["research_rounds"], rounds)
                self.assertEqual(summary["recommendation_count"], 10)
                bundle = read_json(root / f"{name}.json")
                self.assertEqual(len(bundle["candidate_pool"]), target)
                self.assertEqual(bundle["publication_status"], "draft")
                self.assertTrue(all("program_explanation" in item for item in bundle["recommendations"]))
            self.assertEqual(prompt.read_bytes(), prompt_before)
            pair = ["--analysis-a", analysis, "--bundle-a", root / "a.json", "--bundle-b", root / "b.json"]
            labels = root / "listening.json"
            self.cli("prepare-benchmark", *pair, "--output", labels)
            self.assertTrue(all(item["verdict"] is None for item in read_json(labels)["judgments"]))
            before = labels.read_bytes()
            self.cli("prepare-benchmark", *pair, "--output", labels, expected=2)
            self.assertEqual(labels.read_bytes(), before)
            self.cli("benchmark", *pair, "--judgments", labels, "--output", root / "comparison.json",
                     "--research-report-a", root / "a.research.json", "--research-report-b", root / "b.research.json")
            comparison = read_json(root / "comparison.json")
            self.assertEqual(comparison["status"], "incomplete_labels")
            self.assertIsNone(comparison["acceptance_rate_delta_b_minus_a"])
            self.assertIsNone(comparison["winner"])
            self.assertFalse(comparison["policy_changed"])
            self.assertFalse(comparison["same_research_limits"])
            self.assertEqual(comparison["telemetry_a"]["status"], "measured")
            self.assertEqual(comparison["telemetry_b"]["round_count"], 2)
            self.cli("agent", "--analysis", analysis, "--prompt", prompt, "--command", command,
                     "--output", root / "blocked.json", "--report-output", root / "blocked.txt",
                     "--candidate-target", 40, "--max-research-rounds", 1, expected=2)
            self.assertEqual(read_json(root / "blocked.research.json")["status"], "budget_exhausted")
            self.assertFalse((root / "blocked.json").exists())
            self.assertFalse((root / "blocked.txt").exists())

    def test_agent_inherits_prepared_budget_and_custom_manifest(self):
        with tempfile.TemporaryDirectory(prefix="atlas context ") as directory:
            root = Path(directory)
            self.cli("run", "--analysis-mode", "catalog", "--input", ROOT / "tests/fixtures/playlist_sample.json", "--reader", "local_json",
                     "--style-profiles", ROOT / "styles/artist_style_profiles.example.json", "--runtime-dir", root)
            prompt = root / "prepared.md"
            manifest = root / "custom-manifest.json"
            self.cli("prepare-agent", "--analysis", root / "musician_analysis.json", "--output", prompt,
                     "--manifest", manifest, "--context-budget", "100000")
            before = prompt.read_bytes()
            summary = self.cli("agent", "--analysis", root / "musician_analysis.json", "--prompt", prompt,
                               "--manifest", manifest, "--output", root / "bundle.json",
                               "--report-output", root / "report.txt", "--mock")
            self.assertEqual(summary["context_budget"], 100000)
            self.assertEqual(prompt.read_bytes(), before)
            self.cli("agent", "--analysis", root / "musician_analysis.json", "--prompt", prompt,
                     "--manifest", manifest, "--output", root / "blocked.json", "--mock",
                     "--context-budget", "50000", expected=2)
            self.assertFalse((root / "blocked.json").exists())

    def test_run_and_analyze_accept_explicit_reference_date(self):
        with tempfile.TemporaryDirectory(prefix="atlas date ") as directory:
            root = Path(directory)
            self.cli("run", "--analysis-mode", "catalog", "--input", ROOT / "tests/fixtures/playlist_sample.json", "--reader", "local_json",
                     "--style-profiles", ROOT / "styles/artist_style_profiles.example.json", "--runtime-dir", root,
                     "--as-of-date", "2026-01-10")
            before = read_json(root / "musician_analysis.json")
            self.assertEqual(before["as_of_date"], "2026-01-10")
            self.assertEqual(read_json(root / "pipeline_manifest.json")["as_of_date"], "2026-01-10")
            self.cli("analyze", "--analysis-mode", "catalog", "--snapshot", root / "snapshot.json", "--output", root / "later.json",
                     "--markdown", root / "later.md", "--manifest", root / "later-manifest.json",
                     "--style-profiles", ROOT / "styles/artist_style_profiles.example.json", "--as-of-date", "2026-09-01")
            self.assertNotEqual(read_json(root / "later.json")["analysis_id"], before["analysis_id"])
            self.cli("analyze", "--snapshot", root / "snapshot.json", "--output", root / "invalid.json",
                     "--as-of-date", "2026-02-30", expected=2)
            self.assertFalse((root / "invalid.json").exists())


if __name__ == "__main__":
    unittest.main()
