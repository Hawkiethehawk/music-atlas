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
    count = int(policy["min_recommendations"])
    refs = packet["analysis_ref_ids"]
    recommendations = []
    for index in range(count):
        number = index + 1
        recommendations.append(
            {
                "title": f"Contract Fixture {number}",
                "artist": f"Fixture Artist {number}",
                "project": f"Fixture Project {number}",
                "analysis_refs": [refs[0]],
                "relation_path": ["当前偏好分布", "关系证据", f"Fixture Project {number}"],
                "discovery_source": "MusicBrainz",
                "explanation": {
                    "preference_basis": "该样本只引用分析包中的当前偏好分布",
                    "artist_relation": "该样本只用于验证关系字段和引用字段",
                    "music_fit": "该样本只用于验证逐首匹配说明字段",
                    "novelty": "该样本标题不在当前喜欢歌曲清单中",
                    "text": "这是一条用于本地契约验收的完整逐首说明，不代表真实音乐推荐。",
                },
                "sources": [f"https://musicbrainz.org/recording/fixture-{number}"],
                "platform_links": {"youtube": f"https://www.youtube.com/watch?v=fixture{number}"},
            }
        )
    print(
        json.dumps(
            {
                "schema_version": "1.0",
                "bundle_type": "recommendation_bundle",
                "status": "ready",
                "analysis_id": packet["analysis_id"],
                "generated_at": "2026-01-01T00:00:00Z",
                "recommendations": recommendations,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
