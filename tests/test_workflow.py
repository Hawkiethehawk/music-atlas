from __future__ import annotations

import json
from copy import deepcopy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_prompt import build_agent_prompt
from channels import render_for_channel
from contracts import (
    ContractError,
    target_counts,
    track_key,
    validate_analysis_packet,
    validate_playlist_snapshot,
    validate_recommendation_bundle,
)
from musician_analyzer import DEFAULT_POLICY, analyze_snapshot, build_overlap_style_distribution
from recommender import rank_bundle, rank_candidates, score_candidate
from source_adapters import LocalJsonReader, build_snapshot


def minimal_analysis_packet() -> dict:
    favorite_key = track_key("Loved", "Band")
    axes = {
        "heaviness": 40,
        "aggression": 30,
        "atmosphere": 50,
        "electronic_presence": 20,
        "pop_accessibility": 60,
        "rhythmic_density": 30,
        "vocal_harshness": 10,
        "emotional_intensity": 70,
    }
    style_ref = "style:alternative_rock"
    style_mix = [{"style_ref": style_ref, "role": "primary", "weight": 1.0}]
    return {
        "schema_version": "2.0",
        "packet_type": "musician_analysis",
        "analysis_id": "analysis-test",
        "source_snapshot_id": "snapshot-test",
        "source_track_count": 1,
        "favorite_track_keys": [favorite_key],
        "favorite_tracks": [{"title": "Loved", "artist": "Band", "track_key": favorite_key}],
        "primary_distribution": [{"rank": 1, "artist": "Band", "entity_ref": "artist:band", "count": 1}],
        "credited_distribution": [{"rank": 1, "artist": "Band", "entity_ref": "artist:band", "count": 1}],
        "entities": [
            {
                "entity_ref": "artist:band",
                "name": "Band",
                "entity_type": "band",
                "primary_track_count": 1,
                "credited_track_count": 1,
                "is_preferred": False,
                "relation_status": "unmapped",
                "lead_vocalists": [],
                "related_projects": [],
                "sources": [],
                "analysis_refs": ["artist:band"],
            }
        ],
        "preferred_artists": [],
        "analysis_ref_ids": ["artist:band", style_ref],
        "track_style_assignments": [
            {
                "position": 1,
                "track_key": favorite_key,
                "title": "Loved",
                "artist": "Band",
                "classification_status": "classified",
                "confidence": "high",
                "primary_style_ref": style_ref,
                "style_refs": [style_ref],
                "style_mix": style_mix,
                "style_axes": axes,
                "applied_scope": "artist_profile",
                "rationale": "该样本使用测试风格画像。",
            }
        ],
        "style_analysis": {
            "taxonomy_version": "1.0",
            "taxonomy_sha256": "taxonomy-test",
            "profile_catalog_sha256": "profile-test",
            "profile_catalog_mode": "explicit",
            "known_style_refs": [style_ref],
            "active_style_refs": [style_ref],
            "frequency_basis": "primary_artist_track_count_from_current_snapshot",
            "axis_definitions": {
                "heaviness": "重量感",
                "aggression": "攻击性",
                "atmosphere": "氛围感",
                "electronic_presence": "电子存在感",
                "pop_accessibility": "流行可及性",
                "rhythmic_density": "节奏密度",
                "vocal_harshness": "人声极端度",
                "emotional_intensity": "情绪强度",
            },
            "style_definitions": [
                {
                    "style_id": "alternative_rock",
                    "style_ref": style_ref,
                    "label": "另类摇滚",
                    "parent": "rock",
                    "definition": "测试风格定义。",
                    "boundary": "不扩展到未证实风格。",
                }
            ],
            "artist_profile_count": 1,
            "core_artist_profile_count": 1,
            "classified_track_count": 1,
            "unclassified_track_count": 0,
            "style_distribution": [
                {
                    "rank": 1,
                    "style_ref": style_ref,
                    "style_id": "alternative_rock",
                    "label": "另类摇滚",
                    "count": 1,
                    "share": 1.0,
                    "classified_share": 1.0,
                }
            ],
            "dominant_style_mix": [
                {
                    "rank": 1,
                    "style_ref": style_ref,
                    "style_id": "alternative_rock",
                    "label": "另类摇滚",
                    "count": 1,
                    "share": 1.0,
                    "classified_share": 1.0,
                    "weight": 1.0,
                }
            ],
            "style_axes": axes,
            "artist_profiles": [
                {
                    "artist": "Band",
                    "entity_ref": "artist:band",
                    "primary_track_count": 1,
                    "credited_track_count": 1,
                    "is_core_artist": True,
                    "classification_status": "classified",
                    "confidence": "high",
                    "primary_style_ref": style_ref,
                    "style_mix": style_mix,
                    "style_axes": axes,
                    "summary": "测试画像。",
                    "boundaries": ["不扩展到未证实风格。"],
                    "sources": ["https://example.com/style"],
                    "release_override_count": 0,
                }
            ],
            "profile_coverage": {
                "required_artist_count": 1,
                "classified_artist_count": 1,
                "unclassified_artist_count": 0,
                "degraded": False,
            },
        },
        "recommendation_policy": {
            **deepcopy(DEFAULT_POLICY),
            "min_recommendations": 4,
            "max_recommendations": 4,
            "target_recommendations": 4,
            "max_per_artist": 1,
            "max_per_project": 1,
            "min_projects": 4,
            "candidate_pool_min": 4,
            "recall_mix": [
                {"candidate_type": "artist_continuation", "target_ratio": 0.25},
                {"candidate_type": "musician_relation", "target_ratio": 0.25},
                {"candidate_type": "style_neighbor", "target_ratio": 0.25},
                {"candidate_type": "exploration", "target_ratio": 0.25},
            ],
        },
    }


def candidate_fixture(packet: dict, index: int, candidate_type: str) -> dict:
    style_ref = packet["style_analysis"]["known_style_refs"][0]
    source_url = f"https://musicbrainz.org/recording/test-{index}"
    evidence_items = [
        {"claim_type": "track_identity", "claim": "测试歌曲身份", "url": source_url},
        {"claim_type": "style", "claim": "测试风格归属", "url": source_url},
    ]
    if candidate_type == "musician_relation":
        evidence_items.append(
            {"claim_type": "relation", "claim": "测试音乐人关系", "url": source_url}
        )
    return {
        "canonical_track_id": f"musicbrainz:test-{index}",
        "title": f"Candidate {index}",
        "artist": f"Candidate Artist {index}",
        "project": f"Candidate Project {index}",
        "release_date": "2026-01-01",
        "candidate_type": candidate_type,
        "analysis_refs": ["artist:band"],
        "style_refs": [style_ref],
        "style_mix": [{"style_ref": style_ref, "role": "primary", "weight": 1.0}],
        "style_axes": packet["style_analysis"]["style_axes"],
        "style_confidence": "high",
        "relation_path": ["Band", "测试关系", f"Candidate {index}"],
        "evidence_grade": "A",
        "evidence_items": evidence_items,
        "discovery_source": "MusicBrainz",
        "explanation": {
            "preference_basis": "来自 Band 的当前偏好分布",
            "artist_relation": "候选与 Band 存在测试关系",
            "music_fit": "公开资料支持测试音乐特征",
            "style_fit": "候选与当前细分风格及听感轴相符",
            "novelty": "候选不在本次喜欢歌曲清单中",
            "text": "这是一条用于验证确定性评分、召回配额与歌单顺序的完整测试推荐说明。",
        },
        "sources": [source_url],
        "platform_links": {"youtube": f"https://www.youtube.com/watch?v=test{index}"},
    }


class WorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ROOT / "tests" / "fixtures" / "playlist_sample.json"
        self.relations = ROOT / "relations" / "artist_relations.json"
        self.preferred = ROOT / "preferred_artists.txt"

    def test_count_is_read_from_current_snapshot_metadata(self) -> None:
        payload = json.loads(self.fixture.read_text(encoding="utf-8"))
        expected = len(payload["tracks"])
        snapshot = LocalJsonReader().read(
            self.fixture,
            platform="apple_music",
            playlist_id="sample",
            playlist_name="sample",
        )
        self.assertEqual(snapshot["declared_track_count"], expected)
        self.assertEqual(snapshot["track_count"], expected)
        self.assertEqual(snapshot["reader_status"], "complete")
        validate_playlist_snapshot(snapshot)

    def test_count_mismatch_blocks_step_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "playlist.json"
            path.write_text(
                json.dumps(
                    {
                        "declared_track_count": 1,
                        "tracks": json.loads(self.fixture.read_text(encoding="utf-8"))["tracks"],
                    }
                ),
                encoding="utf-8",
            )
            snapshot = build_snapshot(
                path,
                reader_name="local_json",
                platform="apple_music",
                playlist_id="sample",
                playlist_name="sample",
            )
            self.assertEqual(snapshot["reader_status"], "incomplete")
            with self.assertRaises(ContractError):
                validate_playlist_snapshot(snapshot)

    def test_step_two_emits_dynamic_source_count_and_relationship_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "analysis.json"
            snapshot = build_snapshot(
                self.fixture,
                reader_name="local_json",
                platform="apple_music",
                playlist_id="sample",
                playlist_name="sample",
            )
            snapshot_path = Path(directory) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            packet = analyze_snapshot(
                snapshot_path,
                preferred_path=self.preferred,
                relation_path=self.relations,
                output_path=output,
            )
            validate_analysis_packet(packet)
            self.assertEqual(packet["source_track_count"], len(snapshot["tracks"]))
            deftones = next(entity for entity in packet["entities"] if entity["name"] == "Deftones")
            self.assertEqual(deftones["relation_status"], "confirmed")
            self.assertIn("person:chinomoreno", packet["analysis_ref_ids"])

    def test_step_two_covers_every_current_artist_with_individual_style_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "analysis.json"
            snapshot = build_snapshot(
                ROOT / "input" / "web_favorites.json",
                reader_name="local_json",
                platform="apple_music",
                playlist_id="favorite-songs-web",
                playlist_name="喜爱歌曲",
                declared_count_file=ROOT / "input" / "artist_distribution.json",
            )
            snapshot_path = Path(directory) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            packet = analyze_snapshot(
                snapshot_path,
                preferred_path=self.preferred,
                relation_path=self.relations,
                output_path=output,
            )
            validate_analysis_packet(packet)
            profiles = {item["artist"]: item for item in packet["style_analysis"]["artist_profiles"]}
            self.assertEqual(packet["source_track_count"], len(snapshot["tracks"]))
            self.assertEqual(packet["style_analysis"]["core_artist_profile_count"], len(packet["primary_distribution"]))
            self.assertEqual(packet["style_analysis"]["artist_profile_count"], len(packet["entities"]))
            self.assertTrue(
                all(item["artist"] in profiles for item in packet["primary_distribution"])
            )
            self.assertTrue(
                all(
                    abs(sum(style["weight"] for style in item["style_mix"]) - 1.0) <= 0.001
                    for item in profiles.values()
                    if item["classification_status"] == "classified"
                )
            )
            bad_omens = profiles["Bad Omens"]
            self.assertIn("style:metalcore.modern_alternative", [item["style_ref"] for item in bad_omens["style_mix"]])
            self.assertNotEqual(bad_omens["primary_style_ref"], "style:metalcore.traditional")
            self.assertTrue(packet["style_analysis"]["active_style_refs"])

    def test_collaborators_are_profiled_without_entering_primary_style_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "analysis.json"
            snapshot = build_snapshot(
                ROOT / "input" / "web_favorites.json",
                reader_name="local_json",
                platform="apple_music",
                playlist_id="favorite-songs-web",
                playlist_name="喜爱歌曲",
                declared_count_file=ROOT / "input" / "artist_distribution.json",
            )
            snapshot_path = Path(directory) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            packet = analyze_snapshot(
                snapshot_path,
                preferred_path=self.preferred,
                relation_path=self.relations,
                output_path=output,
            )
            validate_analysis_packet(packet)
            primary_names = {item["artist"] for item in packet["primary_distribution"]}
            profile_by_name = {item["artist"]: item for item in packet["style_analysis"]["artist_profiles"]}
            self.assertNotIn("BEAUTY SCHOOL DROPOUT", primary_names)
            self.assertFalse(profile_by_name["BEAUTY SCHOOL DROPOUT"]["is_core_artist"])
            self.assertFalse(profile_by_name["Travis Barker"]["is_core_artist"])
            self.assertEqual(
                sum(item["count"] for item in packet["style_analysis"]["style_distribution"]),
                packet["style_analysis"]["classified_track_count"],
            )

    def test_unmapped_artist_is_not_automatically_classified_as_rock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            snapshot_path = directory_path / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "snapshot_id": "snapshot-unknown",
                        "platform": "apple_music",
                        "captured_at": "2026-01-01T00:00:00Z",
                        "reader_status": "complete",
                        "declared_track_count": 1,
                        "track_count": 1,
                        "tracks": [{"title": "Unknown Song", "artist": "Unmapped Artist", "artists": ["Unmapped Artist"]}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            packet = analyze_snapshot(
                snapshot_path,
                preferred_path=self.preferred,
                relation_path=self.relations,
                output_path=directory_path / "analysis.json",
            )
            validate_analysis_packet(packet)
            assignment = packet["track_style_assignments"][0]
            profile = packet["style_analysis"]["artist_profiles"][0]
            self.assertEqual(assignment["classification_status"], "unclassified")
            self.assertEqual(assignment["style_refs"], [])
            self.assertEqual(profile["classification_status"], "unclassified")
            self.assertEqual(packet["style_analysis"]["style_distribution"], [])

    def test_agent_prompt_is_built_from_analysis_packet(self) -> None:
        packet = minimal_analysis_packet()
        prompt = build_agent_prompt(packet)
        self.assertIn('"analysis_id":"analysis-test"', prompt)
        self.assertIn("MusicianAnalysisPacket", prompt)
        self.assertIn("不读取平台登录态", prompt)
        self.assertIn("known_style_refs", prompt)
        self.assertIn("style_mix", prompt)
        self.assertIn("Prompt Slot: artist_profile", prompt)
        self.assertNotIn("{{", prompt)

    def test_overlap_style_distribution_allows_repeated_style_membership(self) -> None:
        taxonomy = {
            "styles": {
                "a": {
                    "style_id": "a",
                    "style_ref": "style:a",
                    "label": "风格 A",
                },
                "b": {
                    "style_id": "b",
                    "style_ref": "style:b",
                    "label": "风格 B",
                },
            }
        }
        assignments = [
            {"classification_status": "classified", "style_refs": ["style:a", "style:b"]},
            {"classification_status": "classified", "style_refs": ["style:a"]},
        ]
        result = build_overlap_style_distribution(
            assignments,
            taxonomy,
            source_count=2,
            classified_count=2,
        )
        self.assertEqual([item["count"] for item in result], [2, 1])
        self.assertEqual(sum(item["count"] for item in result), 3)
        self.assertGreater(sum(item["classified_share"] for item in result), 1.0)
        self.assertTrue(all(item["overlap"] is True for item in result))

    def test_hybrid_ranker_returns_score_breakdown_and_diversity_metadata(self) -> None:
        packet = minimal_analysis_packet()
        candidates = [
            candidate_fixture(packet, index, candidate_type)
            for index, candidate_type in enumerate(
                ("artist_continuation", "musician_relation", "style_neighbor", "exploration"),
                1,
            )
        ]
        selected, manifest = rank_candidates(candidates, packet, limit=4)
        self.assertEqual(len(selected), 4)
        self.assertIn("ranking_score", selected[0])
        self.assertEqual(set(selected[0]["score_breakdown"]), {
            "style_fit",
            "axis_fit",
            "relation_fit",
            "frequency_fit",
            "novelty",
            "evidence_quality",
            "public_association",
        })
        self.assertEqual(manifest["selected_count"], 4)
        self.assertEqual(manifest["selected_candidate_types"], target_counts(4, packet))
        self.assertEqual([item["sequence_position"] for item in selected], [1, 2, 3, 4])

    def test_agent_supplied_scores_cannot_change_deterministic_score(self) -> None:
        packet = minimal_analysis_packet()
        candidate = candidate_fixture(packet, 1, "style_neighbor")
        baseline = score_candidate(candidate, packet)
        candidate["score_features"] = {key: 0 for key in baseline["features"]}
        self.assertEqual(score_candidate(candidate, packet), baseline)

    def test_ready_bundle_requires_candidate_pool_and_program_ranking(self) -> None:
        packet = minimal_analysis_packet()
        legacy = {
            "schema_version": "2.0",
            "bundle_type": "recommendation_bundle",
            "bundle_stage": "ranked",
            "status": "ready",
            "analysis_id": "analysis-test",
            "generated_at": "2026-01-01T00:00:00Z",
            "recommendations": [],
        }
        with self.assertRaises(ContractError):
            validate_recommendation_bundle(legacy, packet)

    def test_candidate_pool_rejects_missing_recall_type_and_invalid_grade(self) -> None:
        packet = minimal_analysis_packet()
        bundle = {
            "schema_version": "2.0",
            "bundle_type": "recommendation_bundle",
            "bundle_stage": "candidate_pool",
            "status": "ready",
            "analysis_id": "analysis-test",
            "generated_at": "2026-01-01T00:00:00Z",
            "candidate_pool": [candidate_fixture(packet, index, "exploration") for index in range(1, 5)],
            "recommendations": [],
        }
        with self.assertRaises(ContractError):
            validate_recommendation_bundle(bundle, packet)
        bundle["candidate_pool"] = [
            candidate_fixture(packet, index, candidate_type)
            for index, candidate_type in enumerate(
                ("artist_continuation", "musician_relation", "style_neighbor", "exploration"),
                1,
            )
        ]
        bundle["candidate_pool"][0]["evidence_grade"] = "unknown"
        with self.assertRaises(ContractError):
            validate_recommendation_bundle(bundle, packet)

    def test_exploration_can_use_known_but_inactive_style(self) -> None:
        packet = minimal_analysis_packet()
        active_style_ref = packet["style_analysis"]["active_style_refs"][0]
        exploration_style_ref = "style:ambient_rock"
        packet["style_analysis"]["known_style_refs"].append(exploration_style_ref)
        packet["style_analysis"]["style_definitions"].append(
            {
                "style_id": "ambient_rock",
                "style_ref": exploration_style_ref,
                "label": "氛围摇滚",
                "parent": "rock",
                "definition": "用于探索召回的已知风格。",
                "boundary": "不代表当前偏好已激活该风格。",
            }
        )
        candidates = [
            candidate_fixture(packet, index, candidate_type)
            for index, candidate_type in enumerate(
                ("artist_continuation", "musician_relation", "style_neighbor", "exploration"),
                1,
            )
        ]
        exploration = candidates[-1]
        exploration["style_refs"] = [exploration_style_ref]
        exploration["style_mix"] = [
            {"style_ref": exploration_style_ref, "role": "primary", "weight": 1.0}
        ]
        self.assertNotIn(exploration_style_ref, packet["style_analysis"]["active_style_refs"])
        self.assertIn(active_style_ref, packet["style_analysis"]["active_style_refs"])
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
        validate_recommendation_bundle(bundle, packet)

    def test_related_project_inherits_anchor_frequency(self) -> None:
        packet = minimal_analysis_packet()
        packet["analysis_ref_ids"].extend(["person:vocalist", "project:side"])
        packet["entities"][0].update(
            relation_status="confirmed",
            analysis_refs=["artist:band", "person:vocalist", "project:side"],
            lead_vocalists=[
                {"name": "Vocalist", "sources": ["https://example.com/vocalist"]}
            ],
            related_projects=[
                {"name": "Side", "sources": ["https://example.com/side"]}
            ],
            sources=["https://example.com/vocalist", "https://example.com/side"],
        )
        validate_analysis_packet(packet)
        candidate = candidate_fixture(packet, 1, "musician_relation")
        candidate["analysis_refs"] = ["project:side"]
        score = score_candidate(candidate, packet)
        self.assertGreater(score["features"]["frequency_fit"], 0)
        self.assertGreater(score["features"]["relation_fit"], 80)

    def test_recommendation_bundle_requires_external_evidence_and_explanation(self) -> None:
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
        validate_recommendation_bundle(bundle, packet)
        ranked = rank_bundle(bundle, packet)
        validate_recommendation_bundle(ranked, packet)
        candidates[0]["evidence_items"][0]["url"] = "https://music.apple.com/us/song/personalized/2002"
        candidates[0]["sources"].append("https://music.apple.com/us/song/personalized/2002")
        with self.assertRaises(ContractError):
            validate_recommendation_bundle(bundle, packet)

if __name__ == "__main__":
    unittest.main()
