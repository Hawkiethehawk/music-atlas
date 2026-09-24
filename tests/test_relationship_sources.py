from __future__ import annotations
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from unittest import mock

from musician_analyzer import _entity
from contracts import normalized_name
from relationship_sources import RelationshipClient, collect_relationships, select_island_seeds


class FakeRelationshipClient:
    def __init__(self):
        self.events = [{"status": "fixture"}]

    def resolve_artist(self, name):
        return {
            "mbid": "band-id", "qid": "Q1", "discogs_id": "10",
            "url": "https://musicbrainz.org/artist/band-id",
            "record": {
                "name": name, "type": "Group",
                "relations": [
                    {"type": "member of band", "direction": "backward", "ended": False,
                     "begin": "2000", "end": None, "attributes": ["lead vocals", "original"],
                     "artist": {"id": "person-id", "name": "Shared Person"}},
                ],
            },
        }

    def discogs_artist(self, artist_id):
        if str(artist_id) == "10":
            return {"members": [{"id": 20, "name": "Shared Person", "active": True}]}
        if str(artist_id) == "20":
            return {"groups": [{"id": 10, "name": "Seed Band", "active": True},
                               {"id": 30, "name": "Side Project", "active": True}]}
        return None

    def musicbrainz_artist(self, mbid):
        self.assert_equal = mbid
        return {
            "mbid": mbid, "discogs_id": "20", "url": "https://musicbrainz.org/artist/person-id",
            "record": {"name": "Shared Person", "type": "Person", "relations": [
                {"type": "member of band", "direction": "forward", "ended": False,
                 "begin": None, "end": None, "attributes": ["lead vocals"],
                 "artist": {"id": "band-id", "name": "Seed Band"}},
                {"type": "member of band", "direction": "forward", "ended": False,
                 "begin": "2010", "end": None, "attributes": ["lead vocals"],
                 "artist": {"id": "side-id", "name": "Side Project"}},
            ]},
        }


class RelationshipSourceTests(unittest.TestCase):
    def test_independent_seeds_overlap_without_reordering_results(self):
        class SlowClient(FakeRelationshipClient):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.peak = 0
                self.lock = threading.Lock()

            def resolve_artist(self, name):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                time.sleep(0.04 if name == "First" else 0.01)
                with self.lock:
                    self.active -= 1
                return super().resolve_artist(name)

        client = SlowClient()
        seeds = ["First", "Second", "Third", "Fourth"]
        catalog = collect_relationships(seeds, client, workers=3)
        self.assertGreaterEqual(client.peak, 2)
        self.assertEqual(list(catalog["artists"]), seeds)
        self.assertEqual(catalog["seed_artists"], seeds)
        self.assertEqual(catalog["unresolved_artists"], [])

    def test_selects_one_distinct_artist_per_interest_island(self):
        packet = {
            "favorite_tracks": [
                {"artist": "A"}, {"artist": "A"}, {"artist": "B"},
                {"artist": "B"}, {"artist": "C"}, {"artist": "D"},
            ],
            "primary_distribution": [
                {"artist": "A"}, {"artist": "B"}, {"artist": "C"}, {"artist": "D"},
            ],
            "agent_islands": [
                {"record_ids": [0, 1, 2]}, {"record_ids": [2, 3, 4]}, {"record_ids": [4, 5]},
            ],
        }
        self.assertEqual(select_island_seeds(packet), ["A", "B", "C", "D"], "每岛最多 2 位代表艺人")
        self.assertEqual(select_island_seeds(packet, limit=3, per_island=1), ["A", "B", "C"], "旧语义：每岛 1 位")

    def test_collects_members_and_cross_checked_shared_projects(self):
        client = FakeRelationshipClient()
        catalog = collect_relationships(["Seed Band"], client)
        entry = catalog["artists"]["Seed Band"]
        self.assertEqual(entry["members"][0]["name"], "Shared Person")
        self.assertTrue(entry["members"][0]["cross_checked"])
        self.assertEqual(entry["lead_vocalists"][0]["name"], "Shared Person")
        self.assertEqual(entry["related_projects"][0]["name"], "Side Project")
        self.assertEqual(entry["related_projects"][0]["person"], "Shared Person")
        self.assertEqual(entry["related_projects"][0]["confidence"], "high")
        self.assertTrue(entry["related_projects"][0]["cross_checked"])

    def test_analyzer_preserves_generic_relationship_fields(self):
        client = FakeRelationshipClient()
        raw = collect_relationships(["Seed Band"], client)["artists"]["Seed Band"]
        entity, refs = _entity("Seed Band", primary_count=2, credited_count=2,
                               preferred=False, catalog={normalized_name("Seed Band"): raw})
        self.assertEqual(entity["relation_status"], "confirmed")
        self.assertEqual(entity["members"][0]["name"], "Shared Person")
        self.assertEqual(entity["related_projects"][0]["name"], "Side Project")
        self.assertIn("person:sharedperson", refs)
        self.assertIn("project:sideproject", refs)


class RelationshipClientConcurrencyTests(unittest.TestCase):
    @staticmethod
    def response():
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, _limit):
                return b'{"artists": []}'

        return Response()

    def test_parallel_requests_never_exceed_shared_request_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = RelationshipClient(Path(tmp), seconds=2, max_requests=3)
            started = []

            def fake_open(_request, timeout):
                started.append(time.monotonic())
                time.sleep(0.02)
                return self.response()

            urls = [f"https://www.wikidata.org/w/api.php?item={index}" for index in range(12)]
            with mock.patch("relationship_sources.urlopen", side_effect=fake_open):
                with ThreadPoolExecutor(max_workers=6) as pool:
                    list(pool.map(client.get, urls))
            self.assertEqual(client.requests, 3)
            self.assertEqual(len(started), 3)
            self.assertEqual(sum(event["status"] == "budget_exhausted" for event in client.events), 9)

    def test_musicbrainz_slots_are_shared_between_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            clients = [RelationshipClient(path / f"cache-{index}", seconds=2, rate_lock_path=path / "rate.lock")
                       for index in range(2)]
            started = []
            starts_lock = threading.Lock()

            def fake_open(_request, timeout):
                with starts_lock:
                    started.append(time.monotonic())
                return self.response()

            urls = [(clients[index % 2], f"https://musicbrainz.org/ws/2/artist/{index}?fmt=json")
                    for index in range(4)]
            # Windows 的短时 sleep 在全量并行负载下可提前约一轮时钟刻度；
            # 拉大模拟间隔，并保留足够高的实际起始间隔断言。
            with mock.patch("relationship_sources.MUSICBRAINZ_INTERVAL", 0.12), \
                 mock.patch("relationship_sources.urlopen", side_effect=fake_open):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(lambda item: item[0].get(item[1]), urls))
            self.assertEqual(len(started), 4)
            self.assertTrue(all(right - left >= 0.08
                                for left, right in zip(sorted(started), sorted(started)[1:])), started)

    def test_retries_respect_budget_and_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = RelationshipClient(Path(tmp) / "cache", seconds=1, max_requests=1)
            with mock.patch("relationship_sources.urlopen", side_effect=URLError("offline")) as fetch:
                self.assertIsNone(client.get("https://api.discogs.com/artists/1"))
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(client.requests, 1)
            self.assertEqual(client.events[-1]["status"], "budget_exhausted")


class RelationshipDiscoveryTests(unittest.TestCase):
    def test_relation_project_becomes_verified_candidate(self):
        from lastfm_pipeline import discover, validate_bundle
        from tests.test_workflow import minimal_analysis_packet

        packet = minimal_analysis_packet()
        packet["playlist_exclusion"] = {"track_keys": set(), "platform_track_ids": set()}
        packet["entities"][0]["relation_status"] = "confirmed"
        packet["entities"][0]["related_projects"] = [{
            "name": "Side Project", "person": "Shared Person", "relation": "shared_member",
            "role": "lead vocals", "status": "active", "confidence": "high", "cross_checked": True,
            "sources": ["https://musicbrainz.org/artist/band", "https://musicbrainz.org/artist/person"],
        }]

        class LastFMFixture:
            def __init__(self):
                self.events = []
                self.local = SimpleNamespace(retrieved_at="2026-09-17T00:00:00Z")

            def call(self, method, **params):
                if method == "artist.getTopTracks":
                    return {"toptracks": {"track": [{"name": "Real Track"}]}}
                if method == "artist.getSimilar":
                    return {"similarartists": {"artist": []}}
                if method.endswith("getTopTags"):
                    return {"toptags": {"tag": [{"name": "rock"}]}}
                return {}

        fact = {"source": "itunes", "title": "Real Track", "artist": "Side Project",
                "album": "Real Album", "platform_track_id": "100", "cover": None,
                "url": "https://music.apple.com/us/song/real/100", "retrieved_at": "2026-09-17T00:00:00Z"}
        with mock.patch("metadata_verify.verify_many", return_value=[fact]):
            candidates, report = discover(packet, LastFMFixture(), max_candidates=5)
        self.assertEqual(report["relation_candidate_count"], 1)
        self.assertEqual(candidates[0]["candidate_type"], "musician_relation")
        self.assertEqual(candidates[0]["provider_relation"]["person"], "Shared Person")
        self.assertEqual(candidates[0]["relation_path"], ["Band", "共享成员 Shared Person", "Side Project", "Real Track"])
        bundle = {"schema_version": "2.0", "bundle_type": "recommendation_bundle",
                  "bundle_stage": "candidate_pool", "status": "ready", "analysis_id": packet["analysis_id"],
                  "generated_at": "2026-09-17T00:00:00Z", "candidate_pool": candidates, "recommendations": []}
        validate_bundle(bundle, packet)


if __name__ == "__main__":
    unittest.main()
