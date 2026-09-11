#!/usr/bin/env python3
"""TEST ONLY: synthetic taste-summary executor, no music research or network calls.

Reads the taste/artist summary prompt from stdin, extracts the program-computed
listing and style table, and emits a contract-valid ``taste_summary_result``.
All scene names are synthetic markers; they are not music facts.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

AXES = [
    "heaviness", "aggression", "atmosphere", "electronic_presence",
    "pop_accessibility", "rhythmic_density", "vocal_harshness", "emotional_intensity",
]


def _extract(pattern: str, text: str, group: int = 1) -> list[str]:
    return re.findall(pattern, text, flags=re.MULTILINE)


def compose(prompt: str) -> dict:
    request_id = (_extract(r"原样填写：([^>]+)>", prompt) or ["test-request"])[0]
    ids = _extract(r"原样填写：([^>]+)>", prompt)
    request_id = ids[0] if ids else "test-request"
    snapshot_id = ids[1] if len(ids) > 1 else "test-snapshot"
    style_refs = _extract(r"^(style:[a-z0-9_.]+)\|", prompt)
    track_rows = _extract(r"^歌名:(.+?);歌手:(.+)$", prompt)
    artist_rows = _extract(r"^歌手:(.+?);曲目数:(\d+)$", prompt)
    mode = "taste_summary" if track_rows else "artist_summary"
    now = datetime.now(timezone.utc).isoformat()
    if not style_refs:
        raise SystemExit("STYLE REFS 表缺失")

    artists: list[str] = []
    if track_rows:
        seen = {}
        for _, singer in track_rows:
            for name in singer.split("/"):
                name = name.strip()
                if name:
                    seen[name] = seen.get(name, 0) + 1
        artists = [name for name, _ in sorted(seen.items(), key=lambda item: -item[1])]
    else:
        artists = [f"{name.strip()}:{int(count)}" and name.strip() for name, count in artist_rows]

    if not artists:
        raise SystemExit("清单为空，无法生成合成摘要")

    clusters = [
        {"artist": name, "scene": f"TEST_ONLY 合成场景 {index + 1}", "confidence": "low",
         "style_refs": [style_refs[index % len(style_refs)], style_refs[(index + 1) % len(style_refs)]],
         "reference_url": f"https://example.org/atlas-test/artist/{index + 1}",
         **({"layer": "core" if index < max(1, len(artists) // 10) else ("active" if index < max(2, len(artists) // 3) else "longtail")} if mode == "artist_summary" else {})}
        for index, name in enumerate(artists)
    ]
    tags = [
        {"tag": ref, "weight": round(100 / 5, 2), "matched_artists": artists[index::5] or artists[:1]}
        for index, ref in enumerate(style_refs[:5])
    ]
    bundle = {
        "schema_version": "2.0",
        "bundle_type": "taste_summary_result",
        "request_id": request_id,
        "source_snapshot_id": snapshot_id,
        "generated_at": now,
        "analysis_mode": mode,
        "knowledge_basis": {
            "model_internal": "TEST_ONLY 合成知识声明，非真实音乐知识。",
            "web_verified": "TEST_ONLY 未联网。",
            "inference": "TEST_ONLY 合成推演。",
        },
        "artist_clusters": clusters,
        "style_tags": tags,
        "taste_profile": {
            "dominant_styles": style_refs[:2],
            "secondary_styles": style_refs[2:4],
            "exploration_appetite": "medium",
            "mood_axes": {axis: 45 for axis in AXES},
        },
        "editorial_review": {
            "headline": "TEST_ONLY 合成锐评标题",
            "review": "TEST_ONLY：这是合成执行器生成的占位锐评，不代表任何真实品味判断。",
            "inner_world": "TEST_ONLY：这是合成执行器生成的占位解析，不是心理学描述。",
            "humor_notes": [{"note": "TEST_ONLY 幽默推演占位。", "speculation": True}],
        },
        "limitations": ["TEST_ONLY 合成执行器，无真实研究。"],
        "uncertainties": ["TEST_ONLY 全部场景归属均为合成标记。"],
    }
    if mode == "taste_summary":
        titles = [title.strip() for title, _ in track_rows]
        unique_titles = list(dict.fromkeys(titles))
        if len(unique_titles) < 3:
            unique_titles = (unique_titles * 3)[:3]
        bundle["semantic_themes"] = [
            {"theme": "TEST_ONLY 主题一", "tracks": unique_titles[:3], "note": "合成主题。"},
            {"theme": "TEST_ONLY 主题二", "tracks": unique_titles[-3:], "note": "合成主题。"},
        ]
    return bundle


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    data = json.dumps(compose(sys.stdin.read()), ensure_ascii=False)
    sys.stdout.write(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
