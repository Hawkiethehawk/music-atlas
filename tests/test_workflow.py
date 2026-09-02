from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_prompt import build_agent_prompt
from contracts import (
    ContractError,
    track_key,
    validate_analysis_packet,
    validate_playlist_snapshot,
    validate_recommendation_bundle,
)
from musician_analyzer import analyze_snapshot
from source_adapters import LocalJsonReader, build_snapshot


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

    def test_agent_prompt_is_built_from_analysis_packet(self) -> None:
        packet = {
            "schema_version": "1.0",
            "packet_type": "musician_analysis",
            "analysis_id": "analysis-test",
            "source_snapshot_id": "snapshot-test",
            "source_track_count": 1,
            "favorite_track_keys": [track_key("Loved", "Band")],
            "favorite_tracks": [{"title": "Loved", "artist": "Band", "track_key": track_key("Loved", "Band")}],
            "primary_distribution": [{"rank": 1, "artist": "Band", "entity_ref": "artist:band", "count": 1}],
            "credited_distribution": [{"rank": 1, "artist": "Band", "entity_ref": "artist:band", "count": 1}],
            "entities": [],
            "preferred_artists": [],
            "analysis_ref_ids": ["artist:band"],
            "recommendation_policy": {
                "min_recommendations": 1,
                "max_recommendations": 1,
                "max_per_artist": 1,
                "max_per_project": 1,
                "min_projects": 1,
            },
        }
        prompt = build_agent_prompt(packet)
        self.assertIn('"analysis_id": "analysis-test"', prompt)
        self.assertIn("MusicianAnalysisPacket", prompt)
        self.assertIn("不读取 Apple Music、网易云登录状态", prompt)

    def test_recommendation_bundle_requires_external_evidence_and_explanation(self) -> None:
        packet = {
            "schema_version": "1.0",
            "packet_type": "musician_analysis",
            "analysis_id": "analysis-test",
            "source_snapshot_id": "snapshot-test",
            "source_track_count": 1,
            "favorite_track_keys": [track_key("Loved", "Band")],
            "favorite_tracks": [{"title": "Loved", "artist": "Band", "track_key": track_key("Loved", "Band")}],
            "primary_distribution": [{"rank": 1, "artist": "Band", "entity_ref": "artist:band", "count": 1}],
            "credited_distribution": [{"rank": 1, "artist": "Band", "entity_ref": "artist:band", "count": 1}],
            "entities": [],
            "preferred_artists": [],
            "analysis_ref_ids": ["artist:band", "project:related"],
            "recommendation_policy": {
                "min_recommendations": 1,
                "max_recommendations": 1,
                "max_per_artist": 1,
                "max_per_project": 1,
                "min_projects": 1,
            },
        }
        recommendation = {
            "title": "New Song",
            "artist": "Related Artist",
            "project": "Related",
            "analysis_refs": ["artist:band", "project:related"],
            "relation_path": ["Band", "Related", "New Song"],
            "discovery_source": "MusicBrainz",
            "explanation": {
                "preference_basis": "来自 Band 的当前偏好分布",
                "artist_relation": "Related 与 Band 存在可核验的音乐人关系",
                "music_fit": "公开资料显示该作品保留相近的重型编曲",
                "novelty": "该曲不在本次喜欢歌曲清单中",
                "text": "这首歌来自与当前偏好存在明确关系的项目，公开资料也支持其风格匹配，并且不在本次喜欢清单中。",
            },
            "sources": ["https://musicbrainz.org/recording/test"],
            "platform_links": {"apple_music": "https://music.apple.com/us/song/new/2001"},
        }
        bundle = {
            "schema_version": "1.0",
            "bundle_type": "recommendation_bundle",
            "status": "ready",
            "analysis_id": "analysis-test",
            "generated_at": "2026-01-01T00:00:00Z",
            "recommendations": [recommendation],
        }
        validate_recommendation_bundle(bundle, packet)
        recommendation["sources"] = ["https://music.apple.com/us/song/personalized/2002"]
        with self.assertRaises(ContractError):
            validate_recommendation_bundle(bundle, packet)


if __name__ == "__main__":
    unittest.main()
