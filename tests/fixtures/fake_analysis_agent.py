#!/usr/bin/env python3
"""TEST ONLY: synthetic style/relation facts, no music research or network calls."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone


def make_result(payload: dict, *, unclassified: bool = False) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    style_ref = payload["style_definitions"][0]["style_ref"]

    def evidence(kind):
        return [{"claim_type": kind, "claim": "TEST_ONLY synthetic evidence, not a music fact",
                 "url": "https://example.org/atlas-test/" + kind, "retrieved_at": now}]

    profiles = []
    for track in payload["tracks"]:
        profiles.append({
            "position": track["position"], "track_key": track["track_key"],
            "classification_status": "unclassified" if unclassified else "classified",
            "scope": "unknown" if unclassified else "track", "confidence": "low" if unclassified else "medium",
            "style_mix": [] if unclassified else [{"style_ref": style_ref, "role": "primary", "weight": 1}],
            "style_axes": dict.fromkeys(payload["axis_definitions"], None if unclassified else 45),
            "summary": "TEST_ONLY fixture. Not researched and not an actual music assessment.",
            "evidence_items": [] if unclassified else evidence("style"),
        })
    relations = []
    for artist in payload["relation_artists"]:
        relations.append({"artist": artist, "entity_type": "unknown" if unclassified else "band",
                          "lead_vocalists": [] if unclassified else [{"name": "Fixture Vocalist " + artist,
                              "role": "lead vocals", "status": "current", "confidence": "medium", "evidence_items": evidence("relation")}],
                          "related_projects": [] if unclassified else [{"name": "Fixture Project " + artist,
                              "person": "Fixture Vocalist " + artist, "relation": "TEST_ONLY shared vocalist",
                              "confidence": "medium", "evidence_items": evidence("relation")}]})
    return {"schema_version": "2.0", "bundle_type": "musician_research_result", "request_id": payload["request_id"],
            "source_snapshot_id": payload["source_snapshot_id"], "generated_at": now,
            "track_profiles": profiles, "artist_relations": relations}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unclassified", action="store_true")
    args = parser.parse_args()
    payload = json.loads(sys.stdin.read().rsplit("\n```json\n", 1)[1].rsplit("\n```", 1)[0])
    print(json.dumps(make_result(payload, unclassified=args.unclassified), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
