#!/usr/bin/env python3
"""TEST ONLY: run the current web workflow with deterministic provider fixtures."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import lastfm_pipeline
import metadata_verify
import relationship_sources


class FixtureLastFM:
    def __init__(self, cache_dir, seconds=90):
        self.events = []
        self.local = SimpleNamespace(retrieved_at="2026-09-16T00:00:00Z")

    def call(self, method, **params):
        self.local.retrieved_at = "2026-09-16T00:00:00Z"
        self.events.append({"method": method, "status": "fixture", "retrieved_at": self.local.retrieved_at})
        if method.endswith("getTopTags"):
            return {"toptags": {"tag": [{"name": "rock"}, {"name": "electronic"}]}}
        if method == "artist.getSimilar":
            seed = str(params.get("artist") or "Anchor").replace(" ", "-")
            return {"similarartists": {"artist": [
                {"name": f"Fixture-{seed}-{index}", "match": str(1 - index / 10)} for index in range(1, 5)
            ]}}
        if method == "artist.getTopTracks":
            artist = str(params.get("artist") or "Fixture Artist")
            return {"toptracks": {"track": [{"name": f"Fixture Track {index}"} for index in range(1, 4)]}}
        return {}



def fixture_collect_relationships(seed_artists, _client, **_kwargs):
    artists = {}
    for index, seed in enumerate(seed_artists):
        person = f"Fixture Member {index + 1}"
        project = f"Fixture Side Project {index + 1}"
        relation_url = f"https://musicbrainz.org/artist/fixture-seed-{index + 1}"
        person_url = f"https://musicbrainz.org/artist/fixture-person-{index + 1}"
        project_url = f"https://musicbrainz.org/artist/fixture-project-{index + 1}"
        artists[seed] = {
            "canonical_name": seed, "entity_type": "band", "research_origin": "public_catalog",
            "members": [{"name": person, "role": "lead vocals", "status": "active",
                         "confidence": "high", "cross_checked": True,
                         "sources": [relation_url, person_url]}],
            "lead_vocalists": [{"name": person, "role": "lead vocals", "status": "active",
                                "confidence": "high", "sources": [relation_url, person_url]}],
            "collaborators": [],
            "related_projects": [{"name": project, "person": person, "relation": "shared_member",
                                  "role": "lead vocals", "status": "active", "confidence": "high",
                                  "cross_checked": True,
                                  "sources": [relation_url, person_url, project_url]}],
            "sources": [relation_url, person_url, project_url],
        }
    return {"schema_version": "2.0", "catalog_type": "live_public_relations",
            "generated_at": "2026-09-17T00:00:00Z", "seed_artists": seed_artists,
            "artists": artists, "unresolved_artists": [], "requests": [{"status": "fixture"}]}


class FixtureRelationshipClient:
    def __init__(self, *_args, **_kwargs):
        self.events = []


def fixture_verify_many(requested, concurrency=4):
    facts = []
    for index, (title, artist) in enumerate(requested, 1):
        facts.append({"source": "netease", "title": title, "artist": artist,
                      "album": f"Fixture Album {index}", "cover": None,
                      "platform_track_id": f"fixture-{index}-{abs(hash((title, artist))) % 1000000}",
                      "url": f"https://music.163.com/song?id=fixture-{index}",
                      "retrieved_at": "2026-09-16T00:00:00Z"})
    return facts


lastfm_pipeline.LastFM = FixtureLastFM
metadata_verify.verify_many = fixture_verify_many
relationship_sources.RelationshipClient = FixtureRelationshipClient
relationship_sources.collect_relationships = fixture_collect_relationships

from web_workflow import main  # noqa: E402 - patch provider dependencies before importing the CLI


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
