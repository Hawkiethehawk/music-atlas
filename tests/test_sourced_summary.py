"""Source-backed summaries never promote artist context to song evidence."""

import json
import tempfile
import unittest
from pathlib import Path

from contracts import ContractError, require_analysis_coverage, validate_analysis_packet
from agent_prompt import build_agent_prompt
from lastfm_pipeline import _tag_query, validate_knowledge
from musician_analyzer import apply_public_style_evidence, load_style_taxonomy
from source_adapters import build_snapshot
from taste_summary import attach_sourced_editorial, build_sourced_summary_packet


ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "styles" / "style_taxonomy.json"


def snapshot_with(count, directory):
    tracks = [{"title": f"Song {i}", "artist": f"Artist {i % 12}",
               "artists": [f"Artist {i % 12}"], "album": f"Album {i % 12}"}
              for i in range(count)]
    path = directory / "playlist.json"
    path.write_text(json.dumps({"declared_track_count": count, "tracks": tracks}), encoding="utf-8")
    return build_snapshot(path, reader_name="local_json", platform="local",
                          playlist_id="test", playlist_name="test")


def layer(scope, track, style_ref=None):
    artist, title, album = track["artist"], track["title"], track["album"]
    _, _, url, subject = _tag_query(scope, artist, title=title, album=album)
    tags = [{"tag": "Rock", "style_ref": style_ref}] if style_ref else []
    return {"scope": scope, "subject": subject, "source": "lastfm", "url": url,
            "retrieved_at": "2026-09-24T00:00:00Z", "identity_status": "request_only",
            "status": "supported" if tags else "no_style_tags", "tags": tags, "raw_tags": []}


def records_for(packet, style_ref, artist_only=False):
    records = []
    for index, track in enumerate(packet["favorite_tracks"]):
        if artist_only:
            layers = [layer("artist", track, style_ref if index % 12 < 4 else None)]
        else:
            layers = [layer("track", track, style_ref if index < 110 else None),
                      layer("album", track, style_ref if 110 <= index < 155 else None),
                      layer("artist", track, style_ref if 155 <= index < 190 else None)]
        best = next((item for item in layers if item["status"] == "supported"), None)
        records.append({"track_key": track["track_key"], "evidence": layers,
                        "scope": best["scope"] if best else "unknown",
                        "tags": best["tags"] if best else [],
                        "url": best["url"] if best else None,
                        "retrieved_at": best["retrieved_at"] if best else None})
    return {"provider": "lastfm", "axis_policy": "removed", "records": records,
            "scope": "artist" if artist_only else "all", "requests": []}


class SourcedSummaryTest(unittest.TestCase):
    def packet(self, count, directory):
        snapshot = snapshot_with(count, directory)
        shell = build_sourced_summary_packet(snapshot, load_style_taxonomy(TAXONOMY),
                                              taxonomy_path=TAXONOMY)
        return shell

    def test_210_tracks_keep_three_layers_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            shell = self.packet(210, Path(tmp))
            ref = shell["style_analysis"]["known_style_refs"][0]
            packet = apply_public_style_evidence(shell, records_for(shell, ref))
            result = attach_sourced_editorial(packet)
            validate_knowledge(result)
            validate_analysis_packet(result)
            require_analysis_coverage(result)
            coverage = result["style_analysis"]["source_coverage"]
            self.assertEqual((coverage["track_evidence_count"], coverage["album_background_count"],
                              coverage["artist_background_count"]), (110, 45, 35))
            self.assertEqual(result["style_analysis"]["classified_track_count"], 110)
            self.assertEqual(result["track_style_assignments"][125]["classification_status"], "unclassified")
            self.assertEqual(result["track_style_assignments"][125]["applied_scope"], "album_background")
            self.assertEqual(sum(len(i["record_ids"]) for i in result["agent_islands"]), 210)
            self.assertNotIn("style_axes", result["style_analysis"])
            self.assertIn("艺人背景", result["overall_summary"])
            self.assertEqual(result["taste_summary"]["knowledge_basis"]["model_internal"], "未采用")

    def test_default_step_three_prompt_uses_source_contract_without_axes(self):
        with tempfile.TemporaryDirectory() as tmp:
            shell = self.packet(210, Path(tmp))
            ref = shell["style_analysis"]["known_style_refs"][0]
            packet = apply_public_style_evidence(shell, records_for(shell, ref))
            prompt = build_agent_prompt(packet)
            payload = json.loads(prompt.rsplit("\n```json\n", 1)[1].rsplit("\n```", 1)[0])
            self.assertEqual(payload["style_analysis"]["evidence_model"], "sourced_tags_v1")
            self.assertNotIn("style_axes", payload["style_analysis"])
            self.assertNotIn("八维 style_axes", prompt)
            self.assertNotIn("听感轴匹配", prompt)
            self.assertIn("来源模型只使用上述六项", prompt)
            self.assertIn("显式历史 catalog", prompt)

    def test_thousand_tracks_use_artists_only_and_reject_low_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            shell = self.packet(1000, Path(tmp))
            ref = shell["style_analysis"]["known_style_refs"][0]
            packet = apply_public_style_evidence(shell, records_for(shell, ref, artist_only=True))
            result = attach_sourced_editorial(packet)
            validate_knowledge(result)
            require_analysis_coverage(result)
            self.assertEqual(result["style_analysis"]["classified_track_count"], 0)
            self.assertEqual(result["style_analysis"]["source_coverage"]["weighted_artist_track_count"], 336)
            self.assertNotIn("Song", result["overall_summary"])
            self.assertLess(sum(len(i["record_ids"]) for i in result["agent_islands"]), 1000)
            empty = apply_public_style_evidence(shell, records_for(shell, "", artist_only=True))
            with self.assertRaisesRegex(ContractError, "不得发布为 completed"):
                require_analysis_coverage(empty)

    def test_original_thousand_song_list_stays_artist_only_after_half_selection(self):
        from web_workflow import _apply_track_limit

        with tempfile.TemporaryDirectory() as tmp:
            full = snapshot_with(1917, Path(tmp))
            half = _apply_track_limit(full, 959, percentile=0.5)
            packet = build_sourced_summary_packet(
                half, load_style_taxonomy(TAXONOMY), taxonomy_path=TAXONOMY,
                source_playlist_track_count=1917,
            )
            self.assertEqual(packet["analysis_mode"], "artist_summary")
            self.assertEqual(packet["source_playlist_track_count"], 1917)
            self.assertEqual(packet["source_track_count"], 959)
            self.assertEqual(packet["style_analysis"]["source_coverage"]["mode"], "artist_only")


if __name__ == "__main__":
    unittest.main()
