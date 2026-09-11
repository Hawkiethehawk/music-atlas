#!/usr/bin/env python3
"""Test-only Agent: consumes stdin and emits a contract-valid sample bundle."""

from __future__ import annotations

import json
import sys


def main() -> int:
    prompt = sys.stdin.read()
    marker = "```json\n"
    start = prompt.rfind(marker)
    if start < 0:
        raise SystemExit("analysis packet marker missing")
    start += len(marker)
    end = prompt.find("\n```", start)
    packet = json.loads(prompt[start:end])
    policy = packet["recommendation_policy"]
    refs = packet["analysis_ref_ids"]
    style_refs = packet["style_analysis"]["active_style_refs"]
    if not style_refs:
        raise SystemExit("active style refs missing")
    candidate_types = [
        "artist_continuation",
        "musician_relation",
        "style_neighbor",
        "exploration",
    ]
    candidate_pool = []
    anchors = [item["artist"] for item in packet["primary_distribution"]]
    projects = sorted({item["name"] for entity in packet["entities"] for item in entity.get("related_projects", [])
                       if item.get("sources") and item.get("confidence") in {"high", "medium"}})
    if not projects:
        # 品味摘要模式没有关系研究：跳过 musician_relation 候选，而非退出。
        candidate_types = [item for item in candidate_types if item != "musician_relation"]
    for index in range(int(policy["candidate_pool_min"])):
        number = index + 1 + (packet.get("research_request", {}).get("round", 1) - 1) * int(policy["candidate_pool_min"])
        source_url = f"https://musicbrainz.org/recording/00000000-0000-4000-8000-{number:012d}"
        candidate_type = candidate_types[index % len(candidate_types)]
        artist = (anchors[(index // 4) % len(anchors)] if candidate_type == "artist_continuation"
                  else projects[(index // 4) % len(projects)] if candidate_type == "musician_relation"
                  else f"Candidate Artist {number}")
        axes = packet["style_analysis"]["style_axes"]
        if candidate_type == "exploration":
            axes = dict.fromkeys(axes, 0 if sum(axes.values()) / len(axes) >= 50 else 100)
        evidence_items = [
            {"claim_type": "track_identity", "claim": "测试歌曲身份", "url": source_url},
            {"claim_type": "style", "claim": "测试细分风格", "url": source_url},
        ]
        if candidate_type == "musician_relation":
            evidence_items.append(
                {"claim_type": "relation", "claim": "测试音乐人关系", "url": source_url}
            )
        candidate_pool.append(
            {
                "canonical_track_id": f"musicbrainz:candidate-fixture-{number}",
                "title": f"Candidate Fixture {number}",
                "artist": artist,
                "project": f"Candidate Project {number}",
                "release_date": "2026-01-01",
                "candidate_type": candidate_type,
                "analysis_refs": [refs[0]],
                "style_refs": [style_refs[0]],
                "style_mix": [{"style_ref": style_refs[0], "role": "primary", "weight": 1.0}],
                "style_axes": axes,
                "style_confidence": "high",
                "relation_path": ["当前偏好分布", "关系证据", f"Candidate Project {number}"],
                "evidence_grade": "A",
                "evidence_items": evidence_items,
                "discovery_source": "MusicBrainz",
                "sources": [source_url],
                "platform_links": {"youtube": f"https://www.youtube.com/watch?v=candidate{number}"},
            }
        )
    print(
        json.dumps(
            {
                "schema_version": "2.0",
                "bundle_type": "recommendation_bundle",
                "bundle_stage": "candidate_pool",
                "status": "ready",
                "analysis_id": packet["analysis_id"],
                "generated_at": "2026-01-01T00:00:00Z",
                "candidate_pool": candidate_pool,
                "recommendations": [],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
