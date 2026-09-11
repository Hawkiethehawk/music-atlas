from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_runner import run_agent
from agent_prompt import apply_context_budget, build_agent_prompt, prepare_agent_context
from reports import render_report
from contracts import ContractError, read_json, validate_recommendation_bundle, write_json
from evidence import source_evidence_grade
from recommender import rank_bundle, score_candidate
from musician_analyzer import analyze_and_validate
from source_adapters import QQPublicPlaylistReader, build_snapshot
from test_hardening import invoke_cli, pool_bundle
from test_source_adapters import QQ_SAMPLE_PAGE


class EvidenceOutputTests(unittest.TestCase):
    def test_negative_evidence_never_writes_outputs(self):
        for status in ("contradictory", "inaccessible", "stale"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pool, packet = pool_bundle()
                pool["candidate_pool"][-1]["evidence_items"][0]["verification_result"] = status
                write_json(root / "analysis.json", packet)
                with patch("agent_runner.run_external_agent", return_value=pool):
                    with self.assertRaises(ContractError):
                        run_agent(root / "analysis.json", prompt_path=root / "prompt.md",
                                  output_path=root / "bundle.json", report_output_path=root / "report.txt",
                                  command="fixture", mock=False, timeout=1)
                self.assertFalse((root / "bundle.json").exists())
                self.assertFalse((root / "report.txt").exists())

    def test_old_retrieval_and_invalid_identifier_are_rejected(self):
        for update in ({"retrieved_at": "2001-01-01T00:00:00Z"},
                       {"url": "https://musicbrainz.org/recording/not-an-id"}):
            pool, packet = pool_bundle()
            pool["candidate_pool"][0]["evidence_items"][0].update(update)
            if "url" in update:
                pool["candidate_pool"][0]["sources"].append(update["url"])
            with self.assertRaises(ContractError):
                rank_bundle(pool, packet)

    def test_self_reported_grade_source_and_verified_do_not_raise_scores(self):
        pool, packet = pool_bundle()
        candidate = deepcopy(pool["candidate_pool"][0])
        candidate["sources"] = ["https://example.com/facts"]
        for item in candidate["evidence_items"]:
            item["url"] = candidate["sources"][0]
        candidate["evidence_grade"] = "C"
        candidate["discovery_source"] = "unknown"
        baseline = score_candidate(candidate, packet)
        candidate["evidence_grade"] = "A"
        candidate["discovery_source"] = "official MusicBrainz"
        for item in candidate["evidence_items"]:
            item.update(source_class="musicbrainz", verification_result="verified")
        self.assertEqual(source_evidence_grade(candidate), "C")
        self.assertEqual(score_candidate(candidate, packet), baseline)

    def test_unverified_output_is_always_a_draft(self):
        pool, packet = pool_bundle()
        for candidate in pool["candidate_pool"]:
            for item in candidate["evidence_items"]:
                item["verification_result"] = "verified"
        ranked = rank_bundle(pool, packet)
        self.assertEqual(ranked["publication_status"], "draft")
        text = render_report(ranked, packet)
        self.assertIn("研究草稿", text)
        self.assertIn("不是正式推荐", text)
        forged = deepcopy(ranked)
        forged["publication_status"] = "published"
        with self.assertRaises(ContractError):
            validate_recommendation_bundle(forged, packet)
        pool["publication_status"] = "published"
        with self.assertRaises(ContractError):
            rank_bundle(pool, packet)


class PreparedContextTests(unittest.TestCase):
    def test_unachievable_budget_never_calls_agent_or_writes_context(self):
        for budget in (100, 0, -1, True):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, packet = pool_bundle()
                write_json(root / "analysis.json", packet)
                with patch("agent_runner.run_external_agent") as agent:
                    with self.assertRaises(ContractError):
                        run_agent(root / "analysis.json", prompt_path=root / "prompt.md",
                                  output_path=root / "bundle.json", report_output_path=root / "report.txt",
                                  command="fixture", mock=False, timeout=1,
                                  context_budget=budget)
                    agent.assert_not_called()
                for name in ("prompt.md", "agent_context_manifest.json", "bundle.json", "report.txt"):
                    self.assertFalse((root / name).exists())

    def test_prepared_prompt_budget_and_directory_are_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slots = root / "custom slots"
            slots.mkdir()
            slot = slots / "artist_profile.md"
            slot.write_text("original custom requirement " * 100, encoding="utf-8")
            pool, packet = pool_bundle()
            write_json(root / "analysis.json", packet)
            budget = len(build_agent_prompt(packet, slots)) + 100
            prepared, _ = prepare_agent_context(packet, root / "prompt.md", prompt_dir=slots, context_budget=budget)
            before_prompt = (root / "prompt.md").read_bytes()
            before_manifest = (root / "agent_context_manifest.json").read_bytes()
            slot.write_text("changed after preparation", encoding="utf-8")
            with patch("agent_runner.run_external_agent", return_value=pool) as agent:
                summary = run_agent(root / "analysis.json", prompt_path=root / "prompt.md",
                                    output_path=root / "bundle.json", report_output_path=root / "report.txt",
                                    command="fixture", mock=False, timeout=1)
            self.assertEqual(agent.call_args.args[1], prepared)
            self.assertEqual(summary["context_budget"], budget)
            self.assertEqual((root / "prompt.md").read_bytes(), before_prompt)
            self.assertEqual((root / "agent_context_manifest.json").read_bytes(), before_manifest)

    def test_conflicting_config_stale_packet_and_modified_prompt_are_rejected(self):
        for failure in ("budget", "directory", "packet", "prompt", "missing_manifest"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, packet = pool_bundle()
                prepare_agent_context(packet, root / "prompt.md", context_budget=100000)
                options = {}
                if failure == "budget":
                    options["context_budget"] = 200000
                elif failure == "directory":
                    options["prompt_dir"] = root
                elif failure == "packet":
                    packet["source_playlist_name"] = "different current input"
                elif failure == "prompt":
                    with (root / "prompt.md").open("a", encoding="utf-8") as stream:
                        stream.write("modified")
                else:
                    (root / "agent_context_manifest.json").unlink()
                write_json(root / "analysis.json", packet)
                with patch("agent_runner.run_external_agent") as agent:
                    with self.assertRaises(ContractError):
                        run_agent(root / "analysis.json", prompt_path=root / "prompt.md",
                                  output_path=root / "bundle.json", report_output_path=root / "report.txt",
                                  command="fixture", mock=False, timeout=1, **options)
                    agent.assert_not_called()

    def test_failed_preparation_preserves_existing_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, packet = pool_bundle()
            prepare_agent_context(packet, root / "prompt.md")
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with self.assertRaises(ContractError):
                prepare_agent_context(packet, root / "prompt.md", context_budget=100)
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)


class AppleExportCliTests(unittest.TestCase):
    def test_expected_count_is_forwarded_to_node(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "playlist.csv"
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps({
                "status": "exported", "tracks": 2, "completeness_status": "confirmed",
            }), stderr="")
            with patch("shutil.which", return_value="node"), patch("subprocess.run", return_value=result) as run:
                self.assertEqual(invoke_cli(["export-apple-playlist", "--url", "https://music.apple.com/playlist/fixture",
                                             "--output", output, "--expected-count", "2"]), 0)
            self.assertEqual(run.call_args.args[0][-1], "2")
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")

    def test_invalid_count_is_rejected_before_node(self):
        for count in ("0", "-1", "9007199254740992"):
            with patch("shutil.which", return_value="node"), patch("subprocess.run") as run:
                with self.assertRaises(ContractError):
                    invoke_cli(["export-apple-playlist", "--url", "https://music.apple.com/playlist/fixture",
                                "--expected-count", count])
                run.assert_not_called()

    def test_export_timeout_is_a_contract_error(self):
        with patch("shutil.which", return_value="node"), patch("subprocess.run", side_effect=subprocess.TimeoutExpired("node", 600)):
            with self.assertRaises(ContractError):
                invoke_cli(["export-apple-playlist", "--url", "https://music.apple.com/playlist/fixture"])


class SnapshotIdentityTests(unittest.TestCase):
    def test_qq_second_page_changes_snapshot_identity_and_audit_hash(self):
        first = deepcopy(QQ_SAMPLE_PAGE)
        first["req_1"]["data"].update(hasmore=1, total_song_num=3)
        first["req_1"]["data"]["songlist"] = first["req_1"]["data"]["songlist"][:2]
        second = deepcopy(first)
        second["req_1"]["data"].update(hasmore=0)
        second["req_1"]["data"]["songlist"] = second["req_1"]["data"]["songlist"][:1]
        results = []
        for identifier in ("page-two-original", "page-two-changed", "page-two-original"):
            second["req_1"]["data"]["songlist"][0]["mid"] = identifier
            pages = [json.dumps(item).encode("utf-8") for item in (first, second)]
            with patch("source_adapters._fetch_qq_playlist_page", side_effect=pages):
                result = QQPublicPlaylistReader().read(None, platform="qq_music", playlist_id="123", playlist_name="fixture")
            self.assertEqual(result["reader_status"], "complete")
            self.assertEqual(result["reader"]["fetched_pages"], 2)
            self.assertEqual(len(result["reader"]["page_sha256"]), 2)
            results.append(result)
        self.assertNotEqual(results[0]["snapshot_id"], results[1]["snapshot_id"])
        self.assertNotEqual(results[0]["input_sha256"], results[1]["input_sha256"])
        self.assertEqual(results[0]["input_sha256"], results[2]["input_sha256"])
        self.assertEqual(results[0]["reader"]["page_sha256"][0], results[1]["reader"]["page_sha256"][0])
        self.assertNotEqual(results[0]["reader"]["page_sha256"][1], results[1]["reader"]["page_sha256"][1])

    def snapshot(self, root):
        snapshot = build_snapshot(ROOT / "tests/fixtures/playlist_sample.json", reader_name="local_json",
                                  platform="apple_music", playlist_id="fixture", playlist_name="fixture")
        snapshot["captured_at"] = "2026-01-10T23:00:00Z"
        write_json(root / "snapshot.json", snapshot)
        return snapshot

    def analyze(self, root, **options):
        return analyze_and_validate(root / "snapshot.json", preferred_path=root / "none.txt",
                                    relation_path=ROOT / "relations/artist_relations.json", output_path=root / "analysis.json",
                                    style_profile_path=ROOT / "styles/artist_style_profiles.example.json", **options)

    def test_same_snapshot_reanalysis_across_dates_keeps_identity_and_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.snapshot(root)
            with patch("musician_analyzer.utc_now", return_value="2026-01-10T23:00:00Z"):
                first = self.analyze(root)
            with patch("musician_analyzer.utc_now", return_value="2026-09-01T00:00:00Z"):
                second = self.analyze(root)
            self.assertEqual(first["as_of_date"], "2026-01-10")
            self.assertNotEqual(first["generated_at"], second["generated_at"])
            self.assertEqual(first["analysis_id"], second["analysis_id"])
            pool, _ = pool_bundle(first, copies=5)
            pool_bundle(second, copies=5)
            for item in pool["candidate_pool"]:
                item["analysis_refs"] = [first["analysis_ref_ids"][0]]
            ranked = rank_bundle(pool, first)
            self.assertEqual(ranked, rank_bundle(pool, second))
            self.assertEqual(ranked, rank_bundle(ranked, second))

    def test_explicit_scoring_date_changes_identity_and_novelty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.snapshot(root)
            first = self.analyze(root)
            second = self.analyze(root, as_of_date="2026-09-01")
            self.assertNotEqual(first["analysis_id"], second["analysis_id"])
            pool, _ = pool_bundle(first, copies=5)
            candidate = pool["candidate_pool"][0]
            self.assertNotEqual(score_candidate(candidate, first)["features"]["novelty"],
                                score_candidate(candidate, second)["features"]["novelty"])
            with self.assertRaises(ContractError):
                rank_bundle(pool, second)

    def test_snapshot_scoring_date_uses_utc_and_includes_platform_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self.snapshot(root)
            first = self.analyze(root)
            snapshot["captured_at"] = "2026-01-11T07:00:00+08:00"
            write_json(root / "snapshot.json", snapshot)
            equivalent = self.analyze(root)
            self.assertEqual(equivalent["as_of_date"], "2026-01-10")
            self.assertEqual(first["analysis_id"], equivalent["analysis_id"])
            snapshot["tracks"][0]["platform_track_id"] = "changed-id"
            write_json(root / "snapshot.json", snapshot)
            self.assertNotEqual(first["analysis_id"], self.analyze(root)["analysis_id"])

    def test_missing_or_invalid_reference_date_cannot_fall_back_to_wall_clock(self):
        from contracts import validate_analysis_packet
        for value in (None, "2026-02-30", "20260901", "2026-09-01T00:00:00Z"):
            pool, packet = pool_bundle()
            packet["as_of_date"] = value
            with self.assertRaises(ContractError):
                validate_analysis_packet(packet)
            with self.assertRaises(ContractError):
                score_candidate(pool["candidate_pool"][0], packet)


if __name__ == "__main__":
    unittest.main()
