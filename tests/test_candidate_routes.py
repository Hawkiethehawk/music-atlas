import unittest

from candidate_routes import resolve_candidate_route


class CandidateRouteEvidenceTests(unittest.TestCase):
    def test_public_style_tags_can_identify_novel_style(self):
        packet = {
            "primary_distribution": [], "entities": [],
            "style_analysis": {"active_style_refs": []},
            "source_tags": {"records": [{"status": "supported", "tags": [{"style_ref": "style:rock"}]}]},
        }
        url = "https://www.last.fm/music/Different%20Artist"
        candidate = {
            "artist": "Different Artist", "style_refs": [], "sources": [url],
            "style_evidence": {"status": "supported", "scope": "artist", "url": url,
                               "retrieved_at": "2026-09-24T00:00:00Z",
                               "tags": [{"style_ref": "style:industrial_metal"}]},
        }
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "exploration")
        candidate["style_evidence"]["tags"][0]["style_ref"] = "style:rock"
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")

    def test_missing_source_tags_do_not_create_novelty(self):
        packet = {"primary_distribution": [], "entities": [], "style_analysis": {"active_style_refs": []}}
        candidate = {"artist": "Different Artist", "style_evidence": {"tags": [{"style_ref": "style:industrial_metal"}]}}
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")

    def test_public_raw_tags_without_refs_identify_only_evidenced_novelty(self):
        packet = {
            "primary_distribution": [], "entities": [],
            "style_analysis": {"active_style_refs": []},
            "source_tags": {"records": [{"status": "supported", "tags": [
                {"tag": "Rock", "style_ref": None},
                {"tag": "Electronic", "style_ref": None},
            ]}]},
        }
        url = "https://www.last.fm/music/Different%20Artist"
        candidate = {"artist": "Different Artist", "sources": [url],
                     "style_evidence": {"status": "supported", "scope": "artist", "url": url,
                                        "retrieved_at": "2026-09-24T00:00:00Z", "tags": [
                            {"tag": "rock", "style_ref": None},
                            {"tag": "INDUSTRIAL METAL", "style_ref": "style:industrial_metal"},
                        ]}}
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "exploration")
        candidate["style_evidence"]["tags"] = [{"tag": "ROCK", "style_ref": None}]
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")
        candidate["style_evidence"]["tags"] = []
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")

    def test_evidenced_distant_similar_artist_keeps_exploration_route(self):
        packet = {"primary_distribution": [], "entities": [], "style_analysis": {"active_style_refs": []}}
        source = "https://www.last.fm/music/Anchor/+similar"
        candidate = {
            "artist": "Neighbor", "sources": [source],
            "provider_similarity": {"seed": "Anchor", "artist": "Neighbor", "rank": 3, "url": source},
        }
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "exploration")
        candidate["sources"] = []
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")
        candidate["sources"] = [source]
        candidate["provider_similarity"]["rank"] = 1
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")
        candidate["provider_similarity"]["rank"] = 3
        candidate["provider_similarity"]["url"] = "https://www.last.fm/music/Other/+similar"
        candidate["sources"].append(candidate["provider_similarity"]["url"])
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")

    def test_verified_first_neighbor_with_new_tag_stays_style_neighbor(self):
        source = "https://www.last.fm/music/Anchor/+similar"
        packet = {"primary_distribution": [], "entities": [],
                  "style_analysis": {"active_style_refs": ["style:rock"]}}
        candidate = {"artist": "Neighbor", "sources": [source],
                     "style_evidence": {"tags": [{"style_ref": "style:metal"}]},
                     "provider_similarity": {"seed": "Anchor", "artist": "Neighbor",
                                             "rank": 1, "match": "1", "url": source}}
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")
        candidate["provider_similarity"]["match"] = "bad"
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")

    def test_summary_mode_keeps_first_two_public_neighbors_as_style_neighbors(self):
        source = "https://www.last.fm/music/Anchor/+similar"
        packet = {"analysis_mode": "taste_summary", "primary_distribution": [], "entities": [],
                  "style_analysis": {"active_style_refs": []}}
        candidate = {"artist": "Neighbor", "sources": [source],
                     "provider_similarity": {"seed": "Anchor", "artist": "Neighbor",
                                             "rank": 2, "match": "0.9", "url": source}}
        self.assertEqual(resolve_candidate_route(candidate, packet)["candidate_type"], "style_neighbor")
