import time
import unittest

from review import review_candidates, review_groups


def packet():
    return {"playlist_exclusion": {"track_keys": [], "platform_track_ids": []}}


def candidate(index, *, title=None, artist=None):
    title = title or f"Track {index}"
    artist = artist or f"Artist {index}"
    platform_id = f"id-{index}"
    url = f"https://music.apple.com/us/song/{index}"
    fact = {"source": "itunes", "title": title, "artist": artist, "album": f"Album {index}",
            "platform_track_id": platform_id, "url": url}
    return {"canonical_track_id": f"platform:itunes:{platform_id}", "title": title,
            "artist": artist, "project": fact["album"], "platform_track_id": platform_id,
            "metadata_verified": fact, "sources": [url], "candidate_type": "style_neighbor",
            "style_evidence": {}}


class ReviewTests(unittest.TestCase):
    def test_large_pool_is_local_and_under_budget(self):
        started = time.perf_counter()
        report = review_candidates([candidate(i) for i in range(90)], packet())
        elapsed = time.perf_counter() - started
        self.assertIn(report["status"], {"accepted", "completed_with_gaps"})
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["agent_calls"], 0)
        self.assertLess(report["elapsed_ms"], 10000)
        self.assertLess(elapsed, 10)

    def test_identity_mismatch_is_rejected(self):
        item = candidate(1)
        item["metadata_verified"]["artist"] = "Other Artist"
        report = review_candidates([item], packet())
        self.assertEqual(report["status"], "rejected")
        self.assertIn("identity_mismatch:artist", report["entries"][0]["issues"])

    def test_playlist_item_is_rejected(self):
        item = candidate(1)
        p = packet()
        p["playlist_exclusion"]["track_keys"] = ["track 1\x1fartist 1"]
        report = review_candidates([item], p)
        self.assertEqual(report["status"], "rejected")
        self.assertIn("playlist_track_excluded", report["entries"][0]["issues"])

    def test_cross_group_duplicate_is_rejected(self):
        item = candidate(1)
        report = review_groups([{"recommendations": [item]}, {"recommendations": [item]}], packet())
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["duplicate_across_groups"], 1)


if __name__ == "__main__":
    unittest.main()
