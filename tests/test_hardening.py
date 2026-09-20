from __future__ import annotations

# 测试夹具曲目是合成数据，平台上不存在；关闭平台元数据核验，
# 核验逻辑本身由 tests/test_metadata_verify.py 与专门用例覆盖。
import os as _atlas_os
_atlas_os.environ.setdefault("ATLAS_METADATA_VERIFY", "off")

import io
import itertools
import json
import os
import random
import shlex
import subprocess
import sys
import tempfile
import unittest
import venv
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_runner import run_agent, run_external_agent
from contracts import ContractError, PROGRAM_RANKING_FIELDS, normalized_name, read_json, validate_recommendation_bundle, write_json
from evidence import audit_bundle_evidence, suggest_evidence_grade, verify_and_check_items, verify_evidence_item
from musician_analyzer import DEFAULT_POLICY
from recommender import SelectionSearchBudgetExceeded, rank_bundle, rank_candidates
from source_adapters import build_snapshot
from test_workflow import candidate_fixture, minimal_analysis_packet
from workflow import build_parser


TYPES = ("artist_continuation", "musician_relation", "style_neighbor", "exploration")


def pool_bundle(packet=None, copies=2):
    packet = packet or minimal_analysis_packet()
    pool = [deepcopy(candidate_fixture(packet, index, kind))
            for index, kind in enumerate(TYPES * copies, 1)]
    return {
        "schema_version": "2.0", "bundle_type": "recommendation_bundle",
        "bundle_stage": "candidate_pool", "status": "ready", "analysis_id": packet["analysis_id"],
        "generated_at": "2026-01-01T00:00:00Z", "candidate_pool": pool, "recommendations": [],
    }, packet


def invoke_cli(argv):
    args = build_parser().parse_args([str(value) for value in argv])
    with redirect_stdout(io.StringIO()):
        return args.func(args)


class RankingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.pool, self.packet = pool_bundle()
        self.ranked = rank_bundle(self.pool, self.packet)

    def test_ranked_output_is_repeatable_without_mutating_input(self):
        original = deepcopy(self.pool)
        self.assertEqual(self.ranked, rank_bundle(self.pool, self.packet))
        self.assertEqual(self.ranked, rank_bundle(self.ranked, self.packet))
        self.assertEqual(original, self.pool)

    def test_candidate_shortage_returns_available_recommendations(self):
        packet = deepcopy(self.packet)
        packet["recommendation_policy"]["candidate_pool_min"] = 1
        pool = deepcopy(self.pool)
        pool["candidate_pool"] = [pool["candidate_pool"][0]]
        ranked = rank_bundle(pool, packet)
        self.assertEqual(len(ranked["recommendations"]), 1)
        self.assertEqual(ranked["ranking"]["selected_count"], 1)
        validate_recommendation_bundle(ranked, packet)

    def test_ranked_track_facts_must_match_candidate_pool(self):
        for field in ("title", "artist", "project", "release_date", "explanation", "sources", "style_axes"):
            with self.subTest(field=field):
                forged = deepcopy(self.ranked)
                forged["recommendations"][0][field] = "forged"
                with self.assertRaises(ContractError):
                    validate_recommendation_bundle(forged, self.packet)

    def test_zeroed_scores_pass_structure_but_fail_recalculation(self):
        forged = deepcopy(self.ranked)
        for candidate in forged["recommendations"]:
            candidate["ranking_score"] = 0
            for field in ("score_breakdown", "score_features"):
                candidate[field] = dict.fromkeys(candidate[field], 0)
        validate_recommendation_bundle(forged, self.packet)
        with self.assertRaises(ContractError):
            rank_bundle(forged, self.packet)

    def test_alternative_selection_and_order_are_rejected(self):
        reversed_pool = deepcopy(self.pool)
        reversed_pool["candidate_pool"].reverse()
        alternative = rank_bundle(reversed_pool, self.packet)
        alternative["candidate_pool"] = deepcopy(self.pool["candidate_pool"])
        reversed_order = deepcopy(self.ranked)
        reversed_order["recommendations"].reverse()
        reversed_order["ranking"]["selected_canonical_track_ids"].reverse()
        for forged in (alternative, reversed_order):
            validate_recommendation_bundle(forged, self.packet)
            with self.assertRaises(ContractError):
                rank_bundle(forged, self.packet)

    def test_ranking_manifest_and_selection_metadata_are_recomputed(self):
        forged = deepcopy(self.ranked)
        forged["ranking"]["weights"]["style_fit"] = 0
        with self.assertRaises(ContractError):
            rank_bundle(forged, self.packet)
        for field in ("selection_rank", "selection_adjusted_score", "sequence_energy", "sequence_position"):
            forged = deepcopy(self.ranked)
            forged["recommendations"][0][field] = -1
            with self.assertRaises(ContractError):
                rank_bundle(forged, self.packet)

    def test_candidate_cannot_supply_any_program_owned_field(self):
        for field in PROGRAM_RANKING_FIELDS:
            with self.subTest(field=field):
                forged = deepcopy(self.pool)
                forged["candidate_pool"][0][field] = 0
                with self.assertRaises(ContractError):
                    validate_recommendation_bundle(forged, self.packet)

    def test_agent_cannot_submit_even_an_authentic_ranked_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "analysis.json", self.packet)
            with patch("agent_runner.run_external_agent", return_value=self.ranked):
                with self.assertRaises(ContractError):
                    run_agent(root / "analysis.json", prompt_path=root / "prompt.md",
                              output_path=root / "bundle.json", report_output_path=root / "report.txt",
                              command="fixture", mock=False, timeout=10)
            self.assertFalse((root / "bundle.json").exists())
            self.assertFalse((root / "report.txt").exists())


class ConstrainedSelectionTests(unittest.TestCase):
    def test_disabling_transition_preference_matches_zero_transition_weight(self):
        pool, packet = pool_bundle()
        for index, candidate in enumerate(pool["candidate_pool"]):
            candidate["style_axes"] = dict.fromkeys(candidate["style_axes"], index * 13)
        packet["recommendation_policy"]["sequence_policy"]["prefer_adjacent_transitions"] = False
        packet["recommendation_policy"]["sequence_policy"]["allow_familiar_anchor"] = False
        selected, _ = rank_candidates(pool["candidate_pool"], packet, limit=4)
        packet["recommendation_policy"]["sequence_policy"]["prefer_adjacent_transitions"] = True
        policy = packet["recommendation_policy"]["sequence_policy"]
        policy["transition_weight"] = 0
        policy["arc_weight"] = 7 / 9
        policy["ranking_weight"] = 2 / 9
        expected, _ = rank_candidates(pool["candidate_pool"], packet, limit=4)
        self.assertEqual([item["canonical_track_id"] for item in selected],
                         [item["canonical_track_id"] for item in expected])

    def test_twenty_candidates_reserve_artist_capacity_for_relation_quota(self):
        packet = minimal_analysis_packet()
        packet["recommendation_policy"] = deepcopy(DEFAULT_POLICY)
        candidates = [deepcopy(candidate_fixture(packet, index + 1, kind))
                      for index, kind in enumerate(kind for kind in TYPES for _ in range(5))]
        for index in (0, 1, 5, 6, 7, 8):
            candidates[index]["artist"] = "Shared Artist"
            candidates[index]["project"] = "Shared Project"
        for index in (2, 3, 4):
            candidates[index]["style_confidence"] = "low"
        original = deepcopy(candidates)
        selected, manifest = rank_candidates(candidates, packet, limit=10)
        self.assertEqual(len(selected), 10)
        self.assertEqual(Counter(item["candidate_type"] for item in selected), Counter(manifest["target_counts"]))
        shared = [item for item in selected if item["artist"] == "Shared Artist"]
        self.assertEqual(len(shared), 2)
        self.assertTrue(all(item["candidate_type"] == "musician_relation" for item in shared))
        self.assertEqual(original, candidates)

    def test_infeasibility_and_search_budget_exhaustion_are_distinct(self):
        pool, packet = pool_bundle()
        with self.assertRaises(SelectionSearchBudgetExceeded):
            rank_candidates(pool["candidate_pool"], packet, limit=4, search_budget=1)
        for candidate in pool["candidate_pool"]:
            candidate["artist"] = "Only Artist"
        with self.assertRaises(ContractError) as raised:
            rank_candidates(pool["candidate_pool"], packet, limit=4)
        self.assertNotIsInstance(raised.exception, SelectionSearchBudgetExceeded)

    def test_zero_quota_is_supported(self):
        pool, packet = pool_bundle()
        packet["recommendation_policy"]["recall_mix"] = [
            {"candidate_type": kind, "target_ratio": ratio}
            for kind, ratio in zip(TYPES, (0.5, 0.25, 0.25, 0))
        ]
        ranked = rank_bundle(pool, packet)
        validate_recommendation_bundle(ranked, packet)
        self.assertNotIn("exploration", [item["candidate_type"] for item in ranked["recommendations"]])

    def test_small_search_matches_exhaustive_feasibility(self):
        rng = random.Random(17)
        for _ in range(25):
            pool, packet = pool_bundle(copies=3)
            candidates = pool["candidate_pool"]
            for candidate in candidates:
                candidate["artist"] = f"Artist {rng.randrange(6)}"
                candidate["project"] = f"Project {rng.randrange(6)}"
            possible = any(
                len({item["candidate_type"] for item in subset}) == 4
                and len({normalized_name(item["artist"]) for item in subset}) == 4
                and len({normalized_name(item["project"]) for item in subset}) == 4
                for subset in itertools.combinations(candidates, 4)
            )
            if possible:
                selected, _ = rank_candidates(candidates, packet, limit=4)
                self.assertEqual(len(selected), 4)
            else:
                with self.assertRaises(ContractError) as raised:
                    rank_candidates(candidates, packet, limit=4)
                self.assertNotIsInstance(raised.exception, SelectionSearchBudgetExceeded)


class EvidenceAuditBoundaryTests(unittest.TestCase):
    def test_all_declared_verdicts_remain_unaccepted_offline(self):
        for verdict in ("verified", "unverified", "stale", "contradictory", "inaccessible", "duplicated"):
            with self.subTest(verdict=verdict):
                items, result = verify_and_check_items([{
                    "claim_type": "track_identity", "claim": "fixture", "verification_result": verdict,
                    "url": "https://musicbrainz.org/recording/11111111-1111-1111-1111-111111111111",
                }], "A")
                self.assertFalse(result["accepted"])
                self.assertTrue(result["grade_valid"])
                self.assertNotEqual(items[0]["verification_result"], "verified")

    def test_duplicate_does_not_hide_negative_verdict(self):
        item = {"claim_type": "style", "claim": "fixture", "url": "https://example.com/page",
                "verification_result": "contradictory"}
        result = verify_evidence_item(item, seen_urls={item["url"]})
        self.assertTrue(result["duplicate_source"])
        self.assertEqual(result["verification_result"], "contradictory")

    def test_source_grade_is_limited_by_weakest_item(self):
        self.assertEqual(suggest_evidence_grade([
            {"claim_type": "track_identity", "source_class": "musicbrainz"},
            {"claim_type": "style", "source_class": "other"},
        ]), "C")

    def test_naive_and_offset_retrieval_times_and_invalid_dates(self):
        base = {"claim_type": "style", "claim": "fixture", "url": "https://example.com/page"}
        naive = verify_evidence_item({**base, "retrieved_at": "2001-01-01T08:00:00"})
        offset = verify_evidence_item({**base, "retrieved_at": "2001-01-01T16:00:00+08:00"})
        self.assertEqual(naive["verification_result"], "stale")
        self.assertEqual(naive["verification_result"], offset["verification_result"])
        for invalid in ("2026-02-30T00:00:00Z", "not-a-date", "2026-13-01"):
            with self.assertRaises(ContractError):
                verify_evidence_item({**base, "retrieved_at": invalid})
            pool, packet = pool_bundle()
            pool["candidate_pool"][0]["evidence_items"][0]["retrieved_at"] = invalid
            with self.assertRaises(ContractError):
                validate_recommendation_bundle(pool, packet)

    def test_audit_failure_preserves_existing_outputs_and_writes_report(self):
        pool, packet = pool_bundle()
        ranked = rank_bundle(pool, packet)
        self.assertEqual(audit_bundle_evidence(ranked)["accepted_count"], 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "analysis.json", packet)
            write_json(root / "bundle.json", pool)
            (root / "ranked.json").write_text("old ranked", encoding="utf-8")
            (root / "report.txt").write_text("old report", encoding="utf-8")
            argv = ["validate", "--analysis", root / "analysis.json", "--bundle", root / "bundle.json",
                    "--ranked-output", root / "ranked.json", "--output", root / "report.txt",
                    "--evidence-audit", root / "audit.json"]
            self.assertEqual(invoke_cli(argv), 2)
            self.assertEqual(read_json(root / "audit.json")["accepted_count"], 0)
            pool["candidate_pool"][0]["evidence_items"][0]["retrieved_at"] = "invalid"
            write_json(root / "bundle.json", pool)
            with self.assertRaises(ContractError):
                invoke_cli(argv)
            self.assertEqual(read_json(root / "audit.json")["status"], "invalid_contract")
            self.assertEqual((root / "ranked.json").read_text(encoding="utf-8"), "old ranked")
            self.assertEqual((root / "report.txt").read_text(encoding="utf-8"), "old report")


class CsvCountBoundaryTests(unittest.TestCase):
    def test_explicit_count_file_takes_precedence_over_csv_row_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv = root / "playlist.csv"
            csv.write_text("title,artist\nSample,Artist\n", encoding="utf-8")
            count_file = root / "count.json"
            write_json(count_file, {"declared_track_count": 100})
            options = dict(reader_name="csv", platform="apple_music", playlist_id="fixture", playlist_name="fixture")
            incomplete = build_snapshot(csv, **options, declared_count_file=count_file)
            self.assertEqual(incomplete["reader_status"], "incomplete")
            self.assertEqual(incomplete["declared_track_count"], 100)
            self.assertEqual(incomplete["reader"]["declared_count_source"], "declared_count_file")
            explicit = build_snapshot(csv, **options, declared_count=1, declared_count_file=count_file)
            self.assertEqual(explicit["reader_status"], "complete")
            self.assertEqual(explicit["reader"]["declared_count_source"], "argument")
            self.assertEqual(build_snapshot(csv, **options)["reader"]["declared_count_source"], "row_count")
            for payload in ("broken", "{}", '{"count": true}', '{"count": -1}'):
                count_file.write_text(payload, encoding="utf-8")
                with self.assertRaises(ContractError):
                    build_snapshot(csv, **options, declared_count_file=count_file)
            with self.assertRaises(ContractError):
                build_snapshot(csv, **options, declared_count_file=root / "missing.json")


class AgentCommandTests(unittest.TestCase):
    def test_empty_or_invalid_posix_commands_raise_contract_error(self):
        with self.assertRaises(ContractError):
            run_external_agent("  ", "fixture", timeout=1)
        with patch("agent_runner.os.name", "posix"):
            with self.assertRaises(ContractError):
                run_external_agent("'unclosed", "fixture", timeout=1)

    def test_quoted_executable_spaces_c_argument_and_utf8_stdin(self):
        with tempfile.TemporaryDirectory(prefix="atlas agent ") as directory:
            venv.EnvBuilder(with_pip=False).create(directory)
            executable = Path(directory) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            code = "import json,sys; print(json.dumps({'args':sys.argv[1:],'prompt':sys.stdin.read()},ensure_ascii=False))"
            values = ["space in arg", 'embedded "quote"', "C:\\path with space\\", "中文参数"]
            argv = [str(executable), "-c", code, *values]
            command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
            result = run_external_agent(command, "中文提示词\nsecond line", timeout=10)
            self.assertEqual(result["args"], values)
            self.assertEqual(result["prompt"], "中文提示词\nsecond line")

    def test_posix_branch_uses_shlex_without_a_shell(self):
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with patch("agent_runner.os.name", "posix"), patch("agent_runner.subprocess.run", return_value=completed) as run:
            run_external_agent("'/a path/python' -c 'print(1)'", "fixture", timeout=10)
        self.assertEqual(run.call_args.args[0], ["/a path/python", "-c", "print(1)"])
        self.assertFalse(run.call_args.kwargs["shell"])


if __name__ == "__main__":
    unittest.main()
