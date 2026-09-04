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
    style_refs = packet["style_analysis"]["known_style_refs"]
    if not style_refs:
        raise SystemExit("active style refs missing")
    candidate_types = (
        "artist_continuation",
        "musician_relation",
        "style_neighbor",
        "exploration",
    )
    candidate_pool = []
    for index in range(int(policy["candidate_pool_min"])):
        number = index + 1
        source_url = f"https://musicbrainz.org/recording/candidate-fixture-{number}"
        candidate_type = candidate_types[index % len(candidate_types)]
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
                "artist": f"Candidate Artist {number}",
                "project": f"Candidate Project {number}",
                "release_date": "2026-01-01",
                "candidate_type": candidate_type,
                "analysis_refs": [refs[0]],
                "style_refs": [style_refs[0]],
                "style_mix": [{"style_ref": style_refs[0], "role": "primary", "weight": 1.0}],
                "style_axes": packet["style_analysis"]["style_axes"],
                "style_confidence": "high",
                "relation_path": ["当前偏好分布", "关系证据", f"Candidate Project {number}"],
                "evidence_grade": "A",
                "evidence_items": evidence_items,
                "discovery_source": "MusicBrainz",
                "explanation": {
                    "preference_basis": "该样本只引用分析包中的当前偏好分布",
                    "artist_relation": "该样本只用于验证候选关系字段",
                    "music_fit": "该样本只用于验证候选匹配说明字段",
                    "style_fit": "该样本只用于验证具体细分风格引用和匹配说明字段",
                    "novelty": "该样本标题不在当前喜欢歌曲清单中",
                    "text": "这是一条用于本地候选池和排序验收的完整逐首说明，不代表真实音乐推荐。",
                },
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
