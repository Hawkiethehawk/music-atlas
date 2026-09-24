"""New source packets never build a temporary estimated listening-axis model."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from musician_analyzer import analyze_snapshot, load_style_taxonomy
from source_adapters import build_snapshot


ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "styles" / "style_taxonomy.json"


class PublicSourceNoAxesTests(unittest.TestCase):
    def test_taxonomy_has_no_axes_but_legacy_catalog_remains_readable(self):
        self.assertNotIn("axes", json.loads(TAXONOMY.read_text(encoding="utf-8")))
        taxonomy = load_style_taxonomy(TAXONOMY)
        self.assertEqual(len(taxonomy["axis_definitions"]), 8)
        self.assertTrue(taxonomy["known_style_refs"])

    def test_public_packet_skips_legacy_style_construction_entirely(self):
        fixture = ROOT / "tests" / "fixtures" / "playlist_sample.json"
        snapshot = build_snapshot(fixture, reader_name="local_json", platform="apple_music",
                                  playlist_id="public-only", playlist_name="public-only")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            with (patch("musician_analyzer.load_style_profile_catalog", side_effect=AssertionError("catalog")),
                  patch("musician_analyzer._style_assignment", side_effect=AssertionError("assignment")),
                  patch("musician_analyzer._style_profile_output", side_effect=AssertionError("profile")),
                  patch("musician_analyzer._aggregate_style_axes", side_effect=AssertionError("axes"))):
                packet = analyze_snapshot(
                    snapshot_path, preferred_path=root / "preferred.txt",
                    relation_path=root / "relations.json", output_path=root / "analysis.json",
                    style_taxonomy_path=TAXONOMY,
                    style_profile_path=root / "absent-style-catalog.json",
                    analysis_mode="public_facts_only",
                )
            self.assertEqual(packet["style_analysis"]["evidence_model"], "sourced_tags_v1")
            self.assertEqual(packet["style_analysis"]["profile_catalog_mode"], "research")
            self.assertNotIn("axis_definitions", packet["style_analysis"])
            self.assertNotIn("style_axes", packet["style_analysis"])
            self.assertTrue(all("style_axes" not in row for row in packet["track_style_assignments"]))
            self.assertTrue(all("style_axes" not in row for row in packet["style_analysis"]["artist_profiles"]))
            self.assertEqual(packet["source_track_count"], snapshot["track_count"])


if __name__ == "__main__":
    unittest.main()
