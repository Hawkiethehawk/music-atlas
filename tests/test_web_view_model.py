from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from contracts import ContractError
import web_view_model


def _definitions() -> dict[str, dict[str, str]]:
    return {
        "style:metalcore.traditional": {"label": "传统金属核"},
        "style:metalcore.modern_alternative": {"label": "现代另类金属核"},
        "style:metalcore.progressive_djent": {"label": "渐进/ djent 金属核"},
        "style:post_rock": {"label": "后摇"},
        "style:shoegaze": {"label": "shoegaze"},
        "style:nu_metal": {"label": "nu metal"},
    }


def _profile(interest_id: str, style_mix: list[dict[str, object]]) -> dict[str, object]:
    return {
        "interest_id": interest_id,
        "track_count": 3,
        "style_mix": style_mix,
        "style_axes": {},
        "representative_tracks": [{"track_key": "k", "title": "曲目", "artist": "艺人"}],
        "share": 0.5,
    }


def _analysis(profiles: list[dict[str, object]], definitions: dict[str, dict[str, str]] | None = None) -> dict:
    source = _definitions() if definitions is None else definitions
    return {
        "style_analysis": {
            "interest_profiles": profiles,
            "style_definitions": [{"style_ref": ref, **body} for ref, body in source.items()],
        }
    }


class ShortStyleLabelTests(unittest.TestCase):
    def test_strips_english_and_separators(self) -> None:
        self.assertEqual(web_view_model._short_style_label("渐进/ djent 金属核"), "渐进金属核")

    def test_keeps_tail_core_word_when_too_long(self) -> None:
        self.assertEqual(web_view_model._short_style_label("现代另类金属核"), "另类金属核")

    def test_returns_empty_for_english_only_labels(self) -> None:
        for label in ("nu metal", "screamo", "R&B", "lo-fi", "cloud rap"):
            with self.subTest(label=label):
                self.assertEqual(web_view_model._short_style_label(label), "")

    def test_keeps_short_label_unchanged(self) -> None:
        self.assertEqual(web_view_model._short_style_label("后摇"), "后摇")

    def test_handles_missing_label(self) -> None:
        for value in (None, "", 0, []):
            with self.subTest(value=value):
                self.assertEqual(web_view_model._short_style_label(value), "")


class DeriveInterestNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definitions = {key: value for key, value in _definitions().items()}

    def test_prefers_highest_weight_eligible_style(self) -> None:
        profile = _profile("interest-a", [
            {"style_ref": "style:post_rock", "weight": 0.9},          # 2 字，不合格
            {"style_ref": "style:metalcore.traditional", "weight": 0.3},
        ])

        self.assertEqual(web_view_model._derive_interest_name(profile, self.definitions, set()), "传统金属核")

    def test_truncates_overlong_style_label(self) -> None:
        profile = _profile("interest-a", [
            {"style_ref": "style:metalcore.modern_alternative", "weight": 0.7},
        ])

        name = web_view_model._derive_interest_name(profile, self.definitions, set())

        self.assertEqual(name, "另类金属核")
        self.assertEqual(len(name), web_view_model.INTEREST_NAME_MAX_CHARS)

    def test_skips_names_already_used(self) -> None:
        profile = _profile("interest-a", [
            {"style_ref": "style:metalcore.modern_alternative", "weight": 0.9},
            {"style_ref": "style:metalcore.traditional", "weight": 0.5},
        ])

        self.assertEqual(
            web_view_model._derive_interest_name(profile, self.definitions, {"另类金属核"}),
            "传统金属核",
        )

    def test_returns_empty_when_nothing_is_eligible(self) -> None:
        profile = _profile("interest-a", [
            {"style_ref": "style:shoegaze", "weight": 1.0},
            {"style_ref": "style:nu_metal", "weight": 0.5},
            {"style_ref": "style:post_rock", "weight": 0.4},
        ])

        self.assertEqual(web_view_model._derive_interest_name(profile, self.definitions, set()), "")

    def test_ignores_unknown_style_refs(self) -> None:
        profile = _profile("interest-a", [{"style_ref": "style:does_not_exist", "weight": 1.0}])

        self.assertEqual(web_view_model._derive_interest_name(profile, self.definitions, set()), "")


class InterestEntryNamingTests(unittest.TestCase):
    def test_derives_distinct_names_from_style_mix(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.6}]),
            _profile("interest-b", [{"style_ref": "style:metalcore.modern_alternative", "weight": 0.6}]),
        ])

        entries = web_view_model._interest_entries(analysis, {})

        self.assertEqual([entry["name"] for entry in entries], ["传统金属核", "另类金属核"])

    def test_same_dominant_style_still_yields_distinct_names(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.9}]),
            _profile("interest-b", [
                {"style_ref": "style:metalcore.traditional", "weight": 0.9},
                {"style_ref": "style:metalcore.modern_alternative", "weight": 0.4},
            ]),
        ])

        names = [entry["name"] for entry in web_view_model._interest_entries(analysis, {})]

        self.assertEqual(names, ["传统金属核", "另类金属核"])

    def test_editorial_name_wins_and_keeps_code(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.9}]),
        ])
        editorial = {"interests": {"interest-a": {"name": "手写名字", "code": "07"}}}

        entries = web_view_model._interest_entries(analysis, editorial)

        self.assertEqual(entries[0]["name"], "手写名字")
        self.assertEqual(entries[0]["code"], "07")

    def test_auto_names_step_aside_for_editorial_names(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [
                {"style_ref": "style:metalcore.traditional", "weight": 0.9},
                {"style_ref": "style:metalcore.modern_alternative", "weight": 0.4},
            ]),
            _profile("interest-b", [{"style_ref": "style:metalcore.modern_alternative", "weight": 0.9}]),
        ])
        editorial = {"interests": {"interest-b": {"name": "传统金属核"}}}

        names = [entry["name"] for entry in web_view_model._interest_entries(analysis, editorial)]

        self.assertEqual(names, ["另类金属核", "传统金属核"])

    def test_falls_back_when_no_style_definitions(self) -> None:
        analysis = _analysis(
            [_profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.9}])],
            definitions={},
        )

        entries = web_view_model._interest_entries(analysis, {})

        self.assertEqual(entries[0]["name"], "兴趣组 01")

    def test_every_derived_name_is_three_to_five_characters(self) -> None:
        analysis = _analysis([
            _profile("interest-a", [{"style_ref": "style:metalcore.traditional", "weight": 0.9}]),
            _profile("interest-b", [{"style_ref": "style:metalcore.progressive_djent", "weight": 0.9}]),
            _profile("interest-c", [{"style_ref": "style:metalcore.modern_alternative", "weight": 0.9}]),
        ])

        names = [entry["name"] for entry in web_view_model._interest_entries(analysis, {})]

        for name in names:
            with self.subTest(name=name):
                self.assertGreaterEqual(len(name), web_view_model.INTEREST_NAME_MIN_CHARS)
                self.assertLessEqual(len(name), web_view_model.INTEREST_NAME_MAX_CHARS)
        self.assertEqual(len(set(names)), len(names), "自动名字不应重复")


class ExportPayloadValidationTests(unittest.TestCase):
    def test_web_publication_does_not_reuse_stale_audit_or_local_review(self) -> None:
        with TemporaryDirectory() as folder:
            runtime = Path(folder)
            for name in ("snapshot.json", "musician_analysis.json", "recommendation_bundle.json",
                         "evidence_audit.json", "review_report.json"):
                (runtime / name).touch()
            snapshot = {"snapshot_id": "snapshot-1", "track_count": 10}
            analysis = {"analysis_id": "analysis-1"}
            bundle = {"status": "ready", "bundle_stage": "ranked", "publication_status": "draft",
                      "atlas_group_count": 3, "atlas_group_index": 0,
                      "recommendations": [{"title": f"Song {i}", "artist": "Artist",
                                           "style_evidence": {"tags": []}} for i in range(10)]}
            projection = {"status": {"publication": "draft"}, "audit": {}, "review": {},
                          "sources": [{"id": "recommendation", "status": "draft"}, {"id": "evidence"}],
                          "recommendations": bundle["recommendations"], "interests": []}

            def load(path: Path) -> dict:
                if path.name == "evidence_audit.json" or path.name == "review_report.json":
                    raise AssertionError("不能读取前次运行的审计或复核记录")
                return {"snapshot.json": snapshot, "musician_analysis.json": analysis,
                        "recommendation_bundle.json": bundle}[path.name]

            with (patch.object(web_view_model, "read_json", side_effect=load),
                  patch.object(web_view_model, "validate_playlist_snapshot", return_value=snapshot),
                  patch.object(web_view_model, "validate_analysis_packet", return_value=analysis),
                  patch.object(web_view_model, "validate_recommendation_bundle", return_value=bundle),
                  patch.object(web_view_model, "_build_web_payload_from_ranked", return_value=projection),
                  patch.object(web_view_model, "write_json") as write):
                summary = web_view_model.export_web_payload(
                    runtime, runtime / "public.json", publish_web_result=True,
                    review_report_path=runtime / "review_report.json",
                )
            payload = write.call_args.args[1]
            self.assertEqual(summary["publication_status"], "published")
            self.assertEqual(payload["status"]["evidence_audit"], "not_performed")
            self.assertEqual(payload["status"]["review"], "not_performed")
            self.assertEqual([entry["id"] for entry in payload["sources"]], ["recommendation"])
            self.assertEqual(len(payload["sources"][0]["details"]), 10)

    def test_web_publication_requires_three_ranked_ten_track_groups(self) -> None:
        with TemporaryDirectory() as folder:
            runtime = Path(folder)
            for name in ("snapshot.json", "musician_analysis.json", "recommendation_bundle.json"):
                (runtime / name).touch()
            snapshot = {"snapshot_id": "snapshot-1", "track_count": 1}
            analysis = {"analysis_id": "analysis-1"}
            bundle = {"status": "ready", "bundle_stage": "ranked", "publication_status": "draft",
                      "atlas_group_count": 3, "atlas_group_index": 0, "recommendations": []}
            with (patch.object(web_view_model, "read_json", side_effect=[snapshot, analysis, bundle]),
                  patch.object(web_view_model, "validate_playlist_snapshot", return_value=snapshot),
                  patch.object(web_view_model, "validate_analysis_packet", return_value=analysis),
                  patch.object(web_view_model, "validate_recommendation_bundle", return_value=bundle),
                  patch.object(web_view_model, "write_json") as write):
                with self.assertRaisesRegex(ContractError, "每组十首"):
                    web_view_model.export_web_payload(runtime, runtime / "public.json", publish_web_result=True)
                write.assert_not_called()

    def _export(self, bundle: dict, ranked: dict | None = None, *, invalid: bool = False) -> None:
        with TemporaryDirectory() as folder:
            runtime = Path(folder)
            for name in ("snapshot.json", "musician_analysis.json", "recommendation_bundle.json"):
                (runtime / name).touch()
            snapshot = {"snapshot_id": "snapshot-1", "track_count": 1}
            analysis = {"analysis_id": "analysis-1"}
            result = ranked or bundle
            projection = {"recommendations": [], "interests": [], "status": {
                "publication": "draft", "evidence_audit": "not_available", "review": "not_available",
            }}

            def load(path: Path) -> dict:
                return {"snapshot.json": snapshot, "musician_analysis.json": analysis,
                        "recommendation_bundle.json": bundle}[path.name]

            with (patch.object(web_view_model, "read_json", side_effect=load),
                  patch.object(web_view_model, "validate_playlist_snapshot", return_value=snapshot),
                  patch.object(web_view_model, "validate_analysis_packet", return_value=analysis),
                  patch.object(web_view_model, "validate_recommendation_bundle",
                               side_effect=ContractError("deterministic mismatch") if invalid else None,
                               return_value=result) as validate,
                  patch.object(web_view_model, "rank_bundle", return_value=result) as rank,
                  patch.object(web_view_model, "_build_web_payload_from_ranked", return_value=projection) as build,
                  patch.object(web_view_model, "write_json") as write):
                if invalid:
                    with self.assertRaisesRegex(ContractError, "deterministic mismatch"):
                        web_view_model.export_web_payload(runtime, runtime / "public.json")
                    build.assert_not_called()
                    write.assert_not_called()
                else:
                    web_view_model.export_web_payload(runtime, runtime / "public.json")
                    build.assert_called_once_with(snapshot, analysis, result, None, None, None)
                    write.assert_called_once()
                if bundle.get("status") == "ready" and bundle.get("bundle_stage") == "ranked":
                    rank.assert_not_called()
                else:
                    rank.assert_called_once_with(bundle, analysis)
                validate.assert_called_once_with(result, analysis)

    def test_ranked_bundle_is_checked_without_repeated_rank(self) -> None:
        self._export({"status": "ready", "bundle_stage": "ranked"})

    def test_ranked_bundle_tampering_still_blocks_export(self) -> None:
        self._export({"status": "ready", "bundle_stage": "ranked"}, invalid=True)

    def test_candidate_pool_is_ranked_and_checked_before_export(self) -> None:
        self._export({"status": "ready", "bundle_stage": "candidate_pool"},
                     ranked={"status": "ready", "bundle_stage": "ranked"})


class AnalysisCoverageTruthTests(unittest.TestCase):
    def test_island_membership_does_not_inflate_supported_style_coverage(self) -> None:
        analysis = {
            "style_analysis": {
                "classified_track_count": 2,
                "unclassified_track_count": 2,
                "profile_coverage": {"classified_track_share": 0.5, "degraded": True},
            },
            "agent_islands": [
                {"record_ids": [0, 1, 2]},
                {"record_ids": [3]},
            ],
        }
        classified, unclassified, coverage = web_view_model._effective_analysis_coverage(analysis, 4)
        self.assertEqual((classified, unclassified), (2, 2))
        self.assertTrue(coverage["degraded"])
        self.assertEqual(web_view_model._island_assigned_track_count(analysis, 4), 4)
        sources = web_view_model._source_entries(
            {"track_count": 4}, analysis, {"recommendations": []}, None,
        )
        source = next(item for item in sources if item["id"] == "analysis")
        self.assertEqual(source["coverage"], "2/4")
        self.assertEqual(source["islandAssignment"], "4/4")
        self.assertEqual(source["status"], "degraded")

    def test_summary_with_only_island_assignments_shows_zero_supported_styles(self) -> None:
        analysis = {
            "style_analysis": {"classified_track_count": 0, "unclassified_track_count": 3},
            "agent_islands": [{"record_ids": [0, 1, 2, 2, -1, 3]}],
        }
        classified, unclassified, coverage = web_view_model._effective_analysis_coverage(analysis, 3)
        self.assertEqual((classified, unclassified), (0, 3))
        self.assertTrue(coverage["degraded"])
        self.assertEqual(web_view_model._island_assigned_track_count(analysis, 3), 3)


if __name__ == "__main__":
    unittest.main()
