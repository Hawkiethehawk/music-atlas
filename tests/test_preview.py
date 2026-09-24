import unittest
from unittest.mock import patch

from contracts import track_key
from preview import preview_overall_description, preview_recommendations


class PreviewRecommendationsTests(unittest.TestCase):
    def test_initial_description_distinguishes_full_playlist_from_analyzed_sample(self):
        snapshot = {"tracks": [{"artist": "Anchor"}, {"artist": "Anchor"}, {"artist": "Other"}]}
        text = preview_overall_description(snapshot, 12)
        self.assertIn("原歌单共 12 首，本次分析前 3 首", text)
        self.assertIn("2 位艺人", text)
        self.assertIn("正式核验后补齐", text)

    def test_provisional_rows_use_real_platform_evidence_and_respect_exclusions(self):
        snapshot = {
            "snapshot_id": "current", "tracks": [
                {"title": "Favorite", "artist": "Anchor", "platform_track_id": "original"},
                {"title": "Second", "artist": "Other", "platform_track_id": "second"},
            ],
        }
        history = {"track_keys": {track_key("Old", "Other")},
                   "canonical_track_ids": {"platform:netease:old"}}

        def candidate(title, artist, platform_id):
            return {
                "canonical_track_id": f"platform:netease:{platform_id}",
                "platform_track_id": platform_id, "title": title, "artist": artist,
                "metadata_verified": {"url": f"https://music.163.com/song?id={platform_id}", "source": "netease"},
            }

        rows = [candidate("Favorite", "Anchor", "different-id"),
                candidate("New A", "Anchor", "a"), candidate("New B", "Anchor", "b"),
                candidate("Old", "Other", "old"), candidate("New C", "Other", "c")]
        captured = []

        def discover(packet, **kwargs):
            captured.append(packet)
            return rows, {}

        with patch("preview.discover_platform_candidates", side_effect=discover):
            selected = preview_recommendations(snapshot, snapshot["tracks"], history, count=3)

        self.assertEqual([item["title"] for item in selected], ["New A", "New C", "New B"])
        self.assertEqual(len(captured), 1)
        self.assertIn(track_key("Favorite", "Anchor"), captured[0]["playlist_exclusion"]["track_keys"])
        self.assertTrue(all(item["url"].startswith("https://") for item in selected))


if __name__ == "__main__":
    unittest.main()
