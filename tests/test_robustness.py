from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_prompt import (
    AGENT_INSTRUCTIONS,
    apply_context_budget,
    build_agent_prompt,
    prompt_size_telemetry,
)
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from musician_analyzer import analyze_snapshot, write_coverage_report
from source_adapters import build_snapshot
from test_workflow import minimal_analysis_packet
import workflow as workflow_module


class PromptBudgetTests(unittest.TestCase):
    def test_prompt_size_telemetry_reports_characters_and_tokens(self) -> None:
        packet = minimal_analysis_packet()
        prompt = build_agent_prompt(packet)
        telemetry = prompt_size_telemetry(prompt)
        self.assertEqual(telemetry["prompt_characters"], len(prompt))
        self.assertGreater(telemetry["estimated_tokens"], 0)
        self.assertEqual(telemetry["chars_per_token_estimate"], 4)

    def test_budget_within_limit_keeps_all_slots(self) -> None:
        packet = minimal_analysis_packet()
        prompt = build_agent_prompt(packet)
        budget = len(prompt) + 1000
        result, report = apply_context_budget(prompt, budget)
        self.assertEqual(result, prompt)
        self.assertFalse(report["budget_exceeded"])
        self.assertEqual(report["truncated_slots"], [])

    def test_budget_over_limit_truncates_slots_deterministically(self) -> None:
        packet = minimal_analysis_packet()
        prompt = build_agent_prompt(packet)
        budget = len(AGENT_INSTRUCTIONS) + 400
        result, report = apply_context_budget(prompt, budget)
        self.assertTrue(report["budget_exceeded"])
        self.assertLess(len(result), len(prompt))
        self.assertTrue(report["truncated_slots"])
        # JSON 载荷与固定指令绝不被截断
        self.assertIn("```json", result)
        self.assertTrue(result.startswith(AGENT_INSTRUCTIONS))
        self.assertEqual(report["original_characters"], len(prompt))

    def test_unbounded_budget_is_a_noop(self) -> None:
        packet = minimal_analysis_packet()
        prompt = build_agent_prompt(packet)
        result, report = apply_context_budget(prompt, None)
        self.assertEqual(result, prompt)
        self.assertEqual(report["context_budget"], None)
        self.assertFalse(report["budget_exceeded"])


class ProfileCatalogModeTests(unittest.TestCase):
    def test_explicit_example_profile_catalog_is_marked_degraded(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "playlist_sample.json"
        example_profiles = ROOT / "styles" / "artist_style_profiles.example.json"
        self.assertTrue(example_profiles.is_file())
        snapshot = build_snapshot(
            fixture,
            reader_name="local_json",
            platform="apple_music",
            playlist_id="sample",
            playlist_name="sample",
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            packet = analyze_snapshot(
                snapshot_path,
                preferred_path=ROOT / "preferred_artists.txt",
                relation_path=ROOT / "relations" / "artist_relations.json",
                output_path=Path(directory) / "analysis.json",
                style_profile_path=example_profiles,
            )
        self.assertEqual(packet["style_analysis"]["profile_catalog_mode"], "example_fallback")
        self.assertTrue(packet["style_analysis"]["profile_coverage"]["degraded"])


class CoverageReportTests(unittest.TestCase):
    def test_coverage_report_is_written_for_degraded_coverage(self) -> None:
        packet = minimal_analysis_packet()
        packet["style_analysis"]["profile_catalog_mode"] = "example_fallback"
        packet["style_analysis"]["profile_coverage"].update(
            degraded=True,
            unclassified_artist_count=1,
            classified_artist_count=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage_report.json"
            report = write_coverage_report(packet, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["artifact_type"], "coverage_report")
        self.assertTrue(report["degraded"])
        self.assertIn("example_fallback", report["degraded_reasons"])
        self.assertEqual(saved["degraded"], True)

    def test_coverage_report_lists_unclassified_artists(self) -> None:
        packet = minimal_analysis_packet()
        packet["style_analysis"]["profile_catalog_mode"] = "private"
        packet["style_analysis"]["artist_profiles"][0]["classification_status"] = "unclassified"
        packet["style_analysis"]["profile_coverage"].update(
            degraded=True,
            classified_artist_count=0,
            unclassified_artist_count=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage_report.json"
            report = write_coverage_report(packet, path)
        self.assertIn("unclassified_artists", report["degraded_reasons"])
        self.assertEqual(report["unclassified_artist_count"], 1)


class Schema1ArchiveTests(unittest.TestCase):
    def _make_schema1_runtime(self, directory: Path) -> None:
        run_dir = directory / "old-run"
        run_dir.mkdir(parents=True)
        (run_dir / "snapshot.json").write_text(
            json.dumps({"schema_version": "1.0", "tracks": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        (run_dir / "musician_analysis.json").write_text(
            json.dumps({"schema_version": "1.0", "packet_type": "musician_analysis"}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_archive_moves_schema1_runs_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_schema1_runtime(root)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = workflow_module.main(["archive-schema1", "--runtime-dir", str(root)])
            self.assertEqual(code, 0)
            archive_root = root / "archive"
            self.assertTrue(archive_root.is_dir())
            archives = [item for item in archive_root.iterdir() if item.is_dir()]
            self.assertEqual(len(archives), 1)
            moved = archives[0]
            self.assertTrue((moved / "old-run" / "snapshot.json").is_file())
            manifest = json.loads((moved / "archive_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["manifest_type"], "schema1_archive_manifest")
            self.assertEqual([item["source"] for item in manifest["archived_runs"]], ["old-run"])
            # 源目录已被搬空
            self.assertFalse((root / "old-run").exists())

    def test_archive_ignores_empty_and_schema2_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "schema2-run").mkdir()
            (root / "schema2-run" / "analysis.json").write_text(
                json.dumps({"schema_version": "2.0"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "empty-run").mkdir()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = workflow_module.main(["archive-schema1", "--runtime-dir", str(root)])
            self.assertEqual(code, 0)
            self.assertTrue((root / "schema2-run" / "analysis.json").is_file())
            self.assertFalse((root / "archive").exists())


if __name__ == "__main__":
    unittest.main()
