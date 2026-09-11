from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests/fixtures"))

from agent_prompt import build_agent_input, build_agent_prompt
from analysis_agent import execute_analysis_research, prepare_analysis_research
from analysis_contracts import build_research_requests, validate_research_bundle, validate_research_result
from candidate_routes import resolve_candidate_route
from contracts import ContractError, STYLE_AXIS_IDS, read_json, sha256_path, stable_hash, track_key, validate_analysis_packet, write_json
from fake_analysis_agent import make_result
from musician_analyzer import analyze_and_validate, load_style_taxonomy
from source_adapters import build_snapshot
from workflow import build_parser


TAXONOMY = ROOT / "styles/style_taxonomy.json"


def payload_from_prompt(prompt):
    return json.loads(prompt.rsplit("\n```json\n", 1)[1].rsplit("\n```", 1)[0])


def fixture_execute(command, prompt, **kwargs):
    return make_result(payload_from_prompt(prompt))


def process_command(*arguments):
    parts = [sys.executable, *map(str, arguments)]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


class AnalysisAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="atlas analysis ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = build_snapshot(ROOT / "tests/fixtures/playlist_sample.json", reader_name="local_json",
                                       platform="apple_music", playlist_id="sample", playlist_name="fixture")
        self.snapshot_path = self.root / "snapshot.json"
        write_json(self.snapshot_path, self.snapshot)
        self.directory = self.root / "research"
        self.taxonomy = load_style_taxonomy(TAXONOMY)
        self.taxonomy_hash = sha256_path(TAXONOMY)
        self.requests = build_research_requests(self.snapshot, self.taxonomy, self.taxonomy_hash, 2)

    def result(self, request=None, **kwargs):
        return make_result({**(request or self.requests[0]),
                            "style_definitions": list(self.taxonomy["styles"].values()),
                            "axis_definitions": self.taxonomy["axis_definitions"]}, **kwargs)

    def execute(self, **kwargs):
        options = {"command": "test-fixture", "execute": fixture_execute, **kwargs}
        return execute_analysis_research(self.snapshot_path, TAXONOMY, self.directory, **options)

    def analyze(self, bundle):
        return analyze_and_validate(self.snapshot_path, preferred_path=self.root / "absent-preferred.txt",
                                    relation_path=self.root / "absent-relations.json",
                                    style_profile_path=self.root / "absent-profiles.json",
                                    output_path=self.root / "analysis.json", research_bundle_path=bundle)

    def cli(self, *arguments, expected=0):
        completed = subprocess.run([sys.executable, str(ROOT / "workflow.py"), *map(str, arguments)],
                                   cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                   capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        return json.loads(completed.stdout) if completed.stdout.strip() else None

    def test_batches_cover_positions_and_regenerate_track_identity(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["tracks"][0]["position"] = 999
        snapshot["tracks"][0]["track_key"] = "forged"
        requests = build_research_requests(snapshot, self.taxonomy, self.taxonomy_hash, 2)
        tracks = [track for request in requests for track in request["tracks"]]
        self.assertEqual(sorted(track["position"] for track in tracks), [1, 2, 3])
        for track in tracks:
            self.assertEqual(track["track_key"], track_key(track["title"], track["artist"]))
        artists = [name for request in requests for name in request["relation_artists"]]
        self.assertEqual(len(artists), len(set(artists)))
        self.assertIn("Mike Shinoda", artists)

    def test_normalized_artist_relations_are_assigned_once(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["tracks"][1]["artist"] = "imminence!"
        snapshot["tracks"][1]["artists"] = ["imminence!"]
        requests = build_research_requests(snapshot, self.taxonomy, self.taxonomy_hash, 1)
        artists = [name.casefold() for request in requests for name in request["relation_artists"]]
        self.assertEqual(sum(name.rstrip("!") == "imminence" for name in artists), 1)
        write_json(self.snapshot_path, snapshot)
        packet = self.analyze(self.execute())
        self.assertEqual(packet["style_analysis"]["classified_track_count"], 3)
        self.assertEqual(packet["primary_distribution"][0]["count"], 2)
        self.assertTrue(any("source_track_key" in row for row in packet["track_style_assignments"]))
        self.assertEqual(packet["favorite_track_keys"], [row["track_key"] for row in packet["track_style_assignments"]])

    def test_invalid_budgets_and_incomplete_snapshot_do_not_prepare(self):
        for batch_size in (0, 51, True, 1.5):
            with self.subTest(batch_size=batch_size), self.assertRaises(ContractError):
                prepare_analysis_research(self.snapshot_path, TAXONOMY, self.directory, batch_size=batch_size)
        for context_budget in (0, True, -1, 1):
            with self.subTest(context_budget=context_budget), self.assertRaises(ContractError):
                prepare_analysis_research(self.snapshot_path, TAXONOMY, self.directory, context_budget=context_budget)
        self.assertFalse(self.directory.exists())
        self.snapshot["reader_status"] = "incomplete"
        write_json(self.snapshot_path, self.snapshot)
        with self.assertRaises(ContractError):
            self.execute()
        self.assertFalse(self.directory.exists())

    def test_result_requires_exact_track_coverage_and_identity(self):
        for change in ("missing", "duplicate", "extra", "identity", "position", "request"):
            value = self.result()
            if change == "missing":
                value["track_profiles"].pop()
            elif change == "duplicate":
                value["track_profiles"][1] = deepcopy(value["track_profiles"][0])
            elif change == "extra":
                value["track_profiles"].append(deepcopy(value["track_profiles"][0]))
            elif change == "identity":
                value["track_profiles"][0]["track_key"] = "another-track"
            elif change == "position":
                value["track_profiles"][0]["position"] = True
            else:
                value["request_id"] = "other-request"
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_research_result(value, self.requests[0], self.taxonomy)

    def test_agent_cannot_submit_aggregate_scoring_or_policy_fields(self):
        for key in ("source_track_count", "style_distribution", "interest_profiles", "ranking_score", "recommendation_policy"):
            for level in ("result", "profile", "style", "evidence", "relation"):
                value = self.result()
                target = {"result": value, "profile": value["track_profiles"][0],
                          "style": value["track_profiles"][0]["style_mix"][0],
                          "evidence": value["track_profiles"][0]["evidence_items"][0],
                          "relation": value["artist_relations"][0]}[level]
                target[key] = 100
                with self.subTest(key=key, level=level), self.assertRaises(ContractError):
                    validate_research_result(value, self.requests[0], self.taxonomy)

    def test_classified_profiles_need_valid_scope_style_and_axes(self):
        mutations = [("scope", "inferred"), ("confidence", "certain"), ("style_mix", []),
                     ("style_axes", dict.fromkeys(STYLE_AXIS_IDS)),
                     ("style_axes", dict.fromkeys(STYLE_AXIS_IDS, float("nan"))),
                     ("style_axes", {**dict.fromkeys(STYLE_AXIS_IDS, 50), "extra": 2})]
        for field, changed in mutations:
            value = self.result()
            value["track_profiles"][0][field] = changed
            with self.subTest(field=field, changed=changed), self.assertRaises(ContractError):
                validate_research_result(value, self.requests[0], self.taxonomy)
        request = deepcopy(self.requests[0])
        request["tracks"][0]["album"] = ""
        value = self.result(request)
        value["track_profiles"][0]["scope"] = "release"
        with self.assertRaises(ContractError):
            validate_research_result(value, request, self.taxonomy)

    def test_unknown_is_explicit_and_cannot_smuggle_axes_or_evidence(self):
        value = self.result(unclassified=True)
        self.assertEqual(validate_research_result(value, self.requests[0], self.taxonomy), value)
        for field, changed in (("style_axes", dict.fromkeys(STYLE_AXIS_IDS, 0)), ("scope", "artist"),
                               ("confidence", "medium"), ("evidence_items", self.result()["track_profiles"][0]["evidence_items"])):
            altered = deepcopy(value)
            altered["track_profiles"][0][field] = changed
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_research_result(altered, self.requests[0], self.taxonomy)

    def test_relations_require_targets_unique_endpoints_and_evidence(self):
        for change in ("missing_artist", "duplicate_artist", "duplicate_endpoint", "no_evidence", "wrong_claim", "invalid_status"):
            value = self.result()
            if change == "missing_artist":
                value["artist_relations"].pop()
            elif change == "duplicate_artist":
                value["artist_relations"][1] = deepcopy(value["artist_relations"][0])
            else:
                relation = value["artist_relations"][0]["lead_vocalists"][0]
                if change == "duplicate_endpoint":
                    value["artist_relations"][0]["lead_vocalists"].append(deepcopy(relation))
                elif change == "no_evidence":
                    relation["evidence_items"] = []
                elif change == "wrong_claim":
                    relation["evidence_items"][0]["claim_type"] = "style"
                else:
                    relation["status"] = "confirmed"
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_research_result(value, self.requests[0], self.taxonomy)

    def test_evidence_rejects_forbidden_negative_stale_future_and_invalid(self):
        changes = [("url", "https://music.apple.com/us/artist/sample/1"),
                   ("url", "https://open.spotify.com/collection/tracks"),
                   ("url", "https://musicbrainz.org/artist/not-a-valid-identifier"),
                   ("url", "file:///private/notes"), ("retrieved_at", "not-a-date"),
                   ("retrieved_at", "2020-01-01T00:00:00Z"),
                   ("retrieved_at", (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()),
                   ("verification_result", "contradictory"), ("verification_result", "inaccessible"),
                   ("verification_result", "stale"), ("claim_type", "release")]
        for field, changed in changes:
            value = self.result()
            value["track_profiles"][0]["evidence_items"][0][field] = changed
            with self.subTest(field=field, changed=changed), self.assertRaises(ContractError):
                validate_research_result(value, self.requests[0], self.taxonomy)
        value = self.result()
        value["track_profiles"][0]["evidence_items"] = []
        with self.assertRaises(ContractError):
            validate_research_result(value, self.requests[0], self.taxonomy)

    def test_bundle_is_bound_to_full_snapshot_taxonomy_and_batch_order(self):
        bundle = read_json(self.execute(batch_size=2))
        for field, changed in (("snapshot_sha256", "wrong"), ("taxonomy_sha256", "wrong"),
                               ("publication_status", "published"), ("batch_size", 3),
                               ("batches", bundle["batches"][:1]), ("batches", list(reversed(bundle["batches"]))),
                               ("batches", bundle["batches"] + [bundle["batches"][0]])):
            altered = {**bundle, field: changed}
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_research_bundle(altered, self.snapshot, self.taxonomy, self.taxonomy_hash)
        snapshot = deepcopy(self.snapshot)
        snapshot["captured_at"] = "2026-01-01T00:00:00Z"
        self.assertEqual(snapshot["snapshot_id"], self.snapshot["snapshot_id"])
        with self.assertRaises(ContractError):
            validate_research_bundle(bundle, snapshot, self.taxonomy, self.taxonomy_hash)

    def test_prepare_inherits_configuration_and_rejects_changes(self):
        first = prepare_analysis_research(self.snapshot_path, TAXONOMY, self.directory, batch_size=2, context_budget=90000)
        second = prepare_analysis_research(self.snapshot_path, TAXONOMY, self.directory)
        self.assertEqual(first, second)
        execute = Mock(side_effect=fixture_execute)
        self.execute(execute=execute)
        self.assertEqual(execute.call_count, 2)
        with self.assertRaises(ContractError):
            self.execute(batch_size=3)
        with self.assertRaises(ContractError):
            self.execute(context_budget=80000)

    def test_tampered_prompt_and_manifest_never_call_agent(self):
        for change in ("prompt", "manifest", "extra_batch"):
            directory = self.root / change
            prepare_analysis_research(self.snapshot_path, TAXONOMY, directory)
            if change == "prompt":
                (directory / "batch-001.md").write_text("tampered", encoding="utf-8")
            elif change == "manifest":
                value = read_json(directory / "manifest.json")
                value["batches"][0]["result_file"] = "../snapshot.json"
                write_json(directory / "manifest.json", value)
            else:
                write_json(directory / "batch-999.result.json", {})
            execute = Mock()
            with self.subTest(change=change), self.assertRaises(ContractError):
                execute_analysis_research(self.snapshot_path, TAXONOMY, directory, command="fixture", execute=execute)
            execute.assert_not_called()

    def test_nonempty_directory_and_input_parent_cannot_be_overwritten(self):
        self.directory.mkdir()
        write_json(self.directory / "keep.json", {"keep": True})
        with self.assertRaises(ContractError):
            self.execute()
        before = self.snapshot_path.read_bytes()
        with self.assertRaises(ContractError):
            prepare_analysis_research(self.snapshot_path, TAXONOMY, self.root)
        self.assertEqual(before, self.snapshot_path.read_bytes())

    def test_completed_batches_are_reused_without_external_calls(self):
        bundle_path = self.execute(batch_size=2)
        before = bundle_path.read_bytes()
        batch_before = (self.directory / "batch-001.result.json").read_bytes()
        execute = Mock()
        self.execute(execute=execute)
        execute.assert_not_called()
        self.assertEqual(bundle_path.read_bytes(), before)
        self.assertEqual((self.directory / "batch-001.result.json").read_bytes(), batch_before)
        self.assertTrue(all(item["reused_result"] for item in read_json(self.directory / "research_report.json")["batches"]))

    def test_failed_batch_reports_status_and_resume_only_runs_remaining(self):
        calls = 0

        def fail_second(command, prompt, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ContractError("fixture process failed")
            return fixture_execute(command, prompt, **kwargs)

        with self.assertRaises(ContractError):
            self.execute(batch_size=2, execute=fail_second)
        report = read_json(self.directory / "research_report.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["completed_batch_count"], 1)
        self.assertEqual(report["batches"][-1]["status"], "failed")
        self.assertIn("elapsed_ms", report["batches"][-1])
        self.assertFalse((self.directory / "research_bundle.json").exists())
        execute = Mock(side_effect=fixture_execute)
        self.execute(execute=execute)
        self.assertEqual(execute.call_count, 1)

    def test_total_timeout_including_last_batch_does_not_publish(self):
        clock = [0.0]

        def slow(command, prompt, **kwargs):
            clock[0] += 1.1
            return fixture_execute(command, prompt, **kwargs)

        with patch("analysis_agent.time.perf_counter", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(ContractError, "超时"):
                self.execute(timeout=1, execute=slow)
        report = read_json(self.directory / "research_report.json")
        self.assertEqual(report["completed_batch_count"], 0)
        self.assertFalse((self.directory / "research_bundle.json").exists())

    def test_all_batches_share_a_decreasing_timeout(self):
        clock, limits = [0.0], []

        def measured(command, prompt, *, timeout):
            limits.append(timeout)
            clock[0] += 2
            return fixture_execute(command, prompt)

        with patch("analysis_agent.time.perf_counter", side_effect=lambda: clock[0]):
            self.execute(batch_size=1, timeout=6, execute=measured)
        self.assertEqual(limits, [6, 4, 2])

    def test_parallel_analysis_uses_bounded_workers_and_keeps_manifest_order(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def parallel_execute(command, prompt, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.03)
                return fixture_execute(command, prompt, **kwargs)
            finally:
                with lock:
                    active -= 1

        bundle_path = self.execute(batch_size=1, parallelism=5, execute=parallel_execute)
        report = read_json(self.directory / "research_report.json")
        bundle = read_json(bundle_path)
        self.assertEqual(report["parallelism"], 5)
        self.assertEqual(report["completed_batch_count"], 3)
        self.assertEqual(report["batches"], sorted(report["batches"], key=lambda item: item["worker_index"]))
        self.assertEqual(report["batches"][0]["worker_count"], 3)
        self.assertGreaterEqual(maximum, 2)
        self.assertLessEqual(maximum, 5)
        self.assertEqual(sorted(item["position"] for result in bundle["batches"] for item in result["track_profiles"]), [1, 2, 3])

    def test_parallel_analysis_reports_batch_progress_without_changing_result_order(self):
        progress = []
        bundle_path = self.execute(batch_size=1, parallelism=2, progress=progress.append)
        starts = [event for event in progress if event["event"] == "task_started"]
        completed = [event for event in progress if event["event"] == "task_completed"]
        self.assertEqual(progress[0]["event"], "stage_detail")
        self.assertEqual(len(starts), 3)
        self.assertEqual(len(completed), 3)
        self.assertEqual({event["parallel_slots"] for event in starts}, {2})
        self.assertEqual(sorted(event["task_index"] for event in completed), [1, 2, 3])
        self.assertEqual(completed[-1]["track_total"], 3)
        manifest = read_json(self.directory / "manifest.json")
        self.assertEqual(
            [result["request_id"] for result in read_json(bundle_path)["batches"]],
            [record["request_id"] for record in manifest["batches"]],
        )

    def test_output_size_limit_and_failed_reimport_preserve_previous_bundle(self):
        def huge(command, prompt, **kwargs):
            return {"overflow": "x" * 500001}

        with self.assertRaisesRegex(ContractError, "字符上限"):
            self.execute(execute=huge)
        bundle_path = self.execute()
        before = bundle_path.read_bytes()
        result = read_json(self.directory / "batch-001.result.json")
        result["track_profiles"].pop()
        write_json(self.directory / "batch-001.result.json", result)
        with self.assertRaises(ContractError):
            self.execute(command=None)
        self.assertEqual(before, bundle_path.read_bytes())
        self.assertEqual(read_json(self.directory / "research_report.json")["status"], "failed")

    def test_imported_results_make_reproducible_analysis_without_catalog_reads(self):
        manifest = prepare_analysis_research(self.snapshot_path, TAXONOMY, self.directory, batch_size=2)
        for record in manifest["batches"]:
            prompt = (self.directory / record["prompt_file"]).read_text(encoding="utf-8")
            write_json(self.directory / record["result_file"], fixture_execute("", prompt))
        execute = Mock()
        bundle_path = self.execute(command=None, execute=execute)
        execute.assert_not_called()
        with patch("musician_analyzer.load_relationship_catalog") as relations, \
                patch("musician_analyzer.load_style_profile_catalog") as profiles, \
                patch("musician_analyzer.load_preferred_artists") as preferred:
            first = self.analyze(bundle_path)
            second = self.analyze(bundle_path)
            relations.assert_not_called()
            profiles.assert_not_called()
            preferred.assert_not_called()
        self.assertEqual(first["analysis_id"], second["analysis_id"])
        self.assertEqual(first["style_analysis"], second["style_analysis"])
        self.assertEqual(first["preferred_artists"], [])
        self.assertEqual(first["style_analysis"]["classified_track_count"], 3)
        self.assertEqual(first["analysis_research"]["bundle_sha256"], stable_hash(read_json(bundle_path)))

    def test_agent_verified_claims_remain_unverified_in_packet_and_routes(self):
        def self_verified(command, prompt, **kwargs):
            value = fixture_execute(command, prompt, **kwargs)
            for row in value["track_profiles"]:
                row["evidence_items"][0]["verification_result"] = "verified"
            for row in value["artist_relations"]:
                for fact in row["related_projects"]:
                    fact["evidence_items"][0]["verification_result"] = "verified"
            return value

        packet = self.analyze(self.execute(execute=self_verified))
        for assignment in packet["track_style_assignments"]:
            self.assertEqual(assignment["evidence_items"][0]["verification_result"], "unverified")
        entity = next(row for row in packet["entities"] if row["primary_track_count"])
        self.assertEqual(entity["relation_status"], "researched")
        relation = entity["related_projects"][0]
        self.assertEqual(relation["evidence_items"][0]["verification_result"], "unverified")
        route = resolve_candidate_route({"artist": relation["name"]}, packet)
        self.assertEqual(route["verification_scope"], "current_packet_agent_relation")
        payload = build_agent_input(packet)
        self.assertEqual(len(payload["track_style_exceptions"]), 3)
        self.assertIn(entity, payload["entities"])
        self.assertNotIn("input_manifest", payload)
        self.assertNotIn(str(self.snapshot_path), build_agent_prompt(packet))
        self.assertTrue(all(item["evidence_items"] for item in payload["track_style_exceptions"]))

    def test_packet_cannot_promote_agent_facts_or_replace_their_sources(self):
        packet = self.analyze(self.execute())
        for change in ("profile_verified", "profile_sources", "relation_verified", "relation_sources", "confirmed", "origin"):
            altered = deepcopy(packet)
            assignment = altered["track_style_assignments"][0]
            entity = altered["entities"][0]
            if change == "profile_verified":
                assignment["evidence_items"][0]["verification_result"] = "verified"
            elif change == "profile_sources":
                assignment["sources"] = ["https://example.org/forged"]
            elif change == "relation_verified":
                entity["related_projects"][0]["evidence_items"][0]["verification_result"] = "verified"
            elif change == "relation_sources":
                entity["related_projects"][0]["sources"] = ["https://example.org/forged"]
            elif change == "confirmed":
                entity["relation_status"] = "confirmed"
            else:
                entity.pop("research_origin")
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_analysis_packet(altered)

    def test_mixed_unknown_and_scopes_are_preserved(self):
        def mixed(command, prompt, **kwargs):
            payload = payload_from_prompt(prompt)
            value = make_result(payload)
            value["track_profiles"][0]["scope"] = "artist"
            value["track_profiles"][1]["scope"] = "release"
            value["track_profiles"][2] = make_result(payload, unclassified=True)["track_profiles"][2]
            return value

        packet = self.analyze(self.execute(execute=mixed))
        self.assertEqual(packet["style_analysis"]["classified_track_count"], 2)
        self.assertEqual(set(packet["style_analysis"]["profile_coverage"]["assignment_scopes"]),
                         {"agent_artist", "agent_release", "agent_unknown"})
        self.assertEqual(len(build_agent_input(packet)["track_style_exceptions"]), 2)

    def test_all_unknown_remains_null_and_blocks_recommendation_preparation(self):
        def unknown(command, prompt, **kwargs):
            return make_result(payload_from_prompt(prompt), unclassified=True)

        packet = self.analyze(self.execute(execute=unknown))
        self.assertEqual(packet["style_analysis"]["style_axes"], dict.fromkeys(STYLE_AXIS_IDS))
        self.assertEqual(packet["style_analysis"]["unclassified_track_count"], 3)
        with self.assertRaisesRegex(ContractError, "画像覆盖不足"):
            build_agent_prompt(packet)

    def test_cli_default_prepares_instead_of_reading_example_catalog(self):
        root = self.root / "cli"
        summary = self.cli("run", "--input", ROOT / "tests/fixtures/playlist_sample.json", "--runtime-dir", root)
        self.assertEqual(summary["status"], "analysis_agent_required")
        self.assertEqual(summary["source_track_count"], 3)
        self.assertFalse((root / "musician_analysis.json").exists())
        self.assertFalse((root / "agent_prompt.md").exists())
        self.assertFalse(read_json(root / "pipeline_manifest.json")["analysis_written"])
        self.assertEqual(len(read_json(root / "analysis_research/manifest.json")["batches"]), 1)

    def test_restarting_run_cannot_overwrite_a_research_bound_snapshot(self):
        root = self.root / "cli"
        arguments = ["run", "--input", ROOT / "tests/fixtures/playlist_sample.json", "--runtime-dir", root]
        self.cli(*arguments)
        before = (root / "snapshot.json").read_bytes()
        self.cli(*arguments, expected=2)
        self.assertEqual((root / "snapshot.json").read_bytes(), before)

    def test_missing_import_result_and_invalid_snapshot_date_fail_closed(self):
        with self.assertRaises(ContractError):
            self.execute(command=None)
        self.assertFalse((self.directory / "research_bundle.json").exists())
        self.snapshot["captured_at"] = "not-a-date"
        write_json(self.snapshot_path, self.snapshot)
        execute = Mock()
        with self.assertRaises(ContractError):
            self.execute(execute=execute)
        execute.assert_not_called()

    def test_cli_full_agent_pipeline_produces_ten_drafts_and_strict_audit_fails(self):
        root = self.root / "cli"
        command = process_command(ROOT / "tests/fixtures/fake_analysis_agent.py")
        summary = self.cli("run", "--input", ROOT / "tests/fixtures/playlist_sample.json", "--runtime-dir", root,
                           "--analysis-command", command, "--analysis-batch-size", 2, "--as-of-date", "2026-09-06")
        self.assertEqual(summary["profile_catalog_mode"], "agent_research")
        self.assertEqual(summary["classified_track_count"], 3)
        analysis = root / "musician_analysis.json"
        bundle = root / "recommendation_bundle.json"
        self.cli("agent", "--analysis", analysis, "--prompt", root / "agent_prompt.md", "--output", bundle,
                 "--report-output", root / "report.txt", "--command", process_command(ROOT / "tests/fixtures/fake_agent.py"))
        self.assertEqual(len(read_json(bundle)["recommendations"]), 10)
        self.assertEqual(read_json(bundle)["publication_status"], "draft")
        ranked = root / "reranked.json"
        self.cli("validate", "--analysis", analysis, "--bundle", bundle, "--ranked-output", ranked,
                 "--output", root / "validated.txt")
        self.assertEqual(read_json(bundle), read_json(ranked))
        summary = self.cli("validate", "--analysis", analysis, "--bundle", bundle, "--ranked-output", root / "blocked.json",
                           "--output", root / "blocked.txt", "--evidence-audit", root / "audit.json", expected=2)
        self.assertEqual(summary["status"], "evidence_audit_failed")
        self.assertFalse((root / "blocked.json").exists())

    def test_cli_analyze_outputs_stay_together_and_import_inherits_batches(self):
        root = self.root / "cli"
        output = root / "analysis.json"
        self.cli("analyze", "--snapshot", self.snapshot_path, "--output", output,
                 "--analysis-batch-size", 2, "--analysis-context-budget", 90000)
        directory = root / "analysis_research"
        manifest = read_json(directory / "manifest.json")
        for record in manifest["batches"]:
            write_json(directory / record["result_file"], fixture_execute("", (directory / record["prompt_file"]).read_text(encoding="utf-8")))
        self.cli("analyze", "--snapshot", self.snapshot_path, "--output", output, "--import-analysis-results")
        self.assertTrue((root / "musician_analysis.md").is_file())
        self.assertTrue((root / "analysis_manifest.json").is_file())
        self.assertEqual(read_json(output)["analysis_research"]["batch_count"], 2)

    def test_cli_unknown_analysis_is_saved_but_recommendation_is_blocked(self):
        root = self.root / "cli"
        self.cli("run", "--input", ROOT / "tests/fixtures/playlist_sample.json", "--runtime-dir", root,
                 "--analysis-command", process_command(ROOT / "tests/fixtures/fake_analysis_agent.py", "--unclassified"), expected=2)
        self.assertTrue((root / "musician_analysis.json").exists())
        self.assertEqual(read_json(root / "coverage_report.json")["classified_track_share"], 0)
        self.assertFalse((root / "agent_prompt.md").exists())

    def test_cli_external_command_failure_preserves_existing_analysis(self):
        output = self.root / "analysis.json"
        output.write_text("preserve prior analysis", encoding="utf-8")
        self.cli("analyze", "--snapshot", self.snapshot_path, "--output", output,
                 "--analysis-command", process_command("-c", "import sys; sys.exit(4)"), expected=2)
        self.assertEqual(output.read_text(encoding="utf-8"), "preserve prior analysis")
        report = read_json(self.root / "analysis_research/research_report.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["batches"][0]["status"], "failed")

    def test_invalid_config_and_path_collisions_fail_before_external_calls(self):
        output = self.root / "analysis.json"
        policy = self.root / "policy.json"
        write_json(policy, {"unexpected_policy_field": 2})
        cases = [["--as-of-date", "2026-02-30"], ["--policy-file", str(policy)],
                 ["--style-profiles", str(self.root / "absent.json")], ["--preferred", "preferred_artists.txt"],
                 ["--output", str(self.snapshot_path)], ["--markdown", str(self.snapshot_path)],
                 ["--manifest", str(self.snapshot_path)], ["--manifest", str(output)],
                 ["--analysis-research-dir", str(self.root)],
                 ["--output", str(self.root / "analysis_research/research_bundle.json"),
                  "--analysis-research-dir", str(self.root / "analysis_research")]]
        before = self.snapshot_path.read_bytes()
        for extra in cases:
            args = build_parser().parse_args(["analyze", "--snapshot", str(self.snapshot_path), "--output", str(output),
                                              "--analysis-command", "fixture", *extra])
            with self.subTest(extra=extra), patch("analysis_agent.execute_analysis_research") as unused, \
                    patch("workflow.execute_analysis_research") as execute:
                with self.assertRaises(ContractError):
                    args.func(args)
                execute.assert_not_called()
                unused.assert_not_called()
        self.assertEqual(self.snapshot_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
