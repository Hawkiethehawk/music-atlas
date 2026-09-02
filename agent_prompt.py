#!/usr/bin/env python3
"""Render the isolated Step 3 Agent context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contracts import ContractError, validate_analysis_packet, write_json


AGENT_INSTRUCTIONS = """你是每周音乐推荐 Agent。你的唯一用户偏好输入是下方的 MusicianAnalysisPacket。

硬性边界：
1. 不读取 Apple Music、网易云登录状态、原始歌单、历史推荐、上一次运行结果或任何个性化推荐页面。
2. 只可使用 MusicianAnalysisPacket 中已经给出的艺人分布、主唱、关联项目、喜欢歌曲清单和关系来源来解释偏好。
3. 候选歌曲必须通过公开外部资料核验。允许使用官方艺人/唱片公司页面、MusicBrainz、Wikidata、Last.fm、ListenBrainz、Bandcamp、公开 YouTube 页面或 Spotify 公开页面。
4. Apple Music 只能作为最终跳转链接，不能作为候选发现、排序依据或推荐说明来源；网易云个性化推荐同样禁止。
5. 如果一首歌无法同时证明“为什么符合当前偏好”和“歌曲/发行事实来源”，就不要推荐。
6. 不要把关系目录中的艺人关系扩写成未被来源支持的事实。关系不确定时放弃该候选。

选曲规则：
- 目标数量使用 packet.recommendation_policy 中的动态范围。
- 同一艺人不超过 max_per_artist，同一项目不超过 max_per_project，尽量覆盖 min_projects 个项目。
- 排除 packet.favorite_track_keys 中的当前喜爱歌曲；只在本次包内去重，不读取或承诺跨运行去重。
- 候选优先来自喜欢艺人的新发行、喜欢艺人的主唱/前主唱/关联项目，以及由这些关系自然延伸出的公开相关艺人。

输出规则：
- 只输出一个合法 JSON 对象，不要 Markdown，不要代码围栏，不要额外说明。
- 对每首歌曲都必须填写 title、artist、project、analysis_refs、relation_path、discovery_source、explanation、sources 和 platform_links。
- explanation 必须分别填写 preference_basis、artist_relation、music_fit、novelty、text；text 是面向用户的完整中文说明，不能使用“感觉你会喜欢”这类无依据表述。
- 每首歌曲的 sources 至少包含一个公开外部 HTTP(S) 来源，不能是 music.apple.com，也不能是任何个性化推荐页面。
- analysis_refs 只能引用 packet.analysis_ref_ids 中的值；relation_path 至少包含“喜欢的艺人/分布”“音乐人关系”“推荐项目或歌曲”三段含义。
- bundle.status 为 ready 时推荐数量必须符合 packet 的动态范围；证据不足时使用 insufficient_evidence，并返回空 recommendations 和明确 message。

JSON 顶层格式：
{
  "schema_version": "1.0",
  "bundle_type": "recommendation_bundle",
  "status": "ready",
  "analysis_id": "必须等于 packet.analysis_id",
  "generated_at": "ISO-8601",
  "recommendations": [
    {
      "title": "歌曲名",
      "artist": "艺人名",
      "project": "发行该歌曲的项目/乐队",
      "analysis_refs": ["artist:...", "person:...", "project:..."],
      "relation_path": ["喜欢的艺人/分布", "主唱或关联项目", "推荐歌曲"],
      "discovery_source": "公开来源类型",
      "explanation": {
        "preference_basis": "引用 packet 中的具体偏好依据",
        "artist_relation": "说明主唱、前乐队、side project 或相关艺人关系",
        "music_fit": "说明这首歌的可核验匹配点",
        "novelty": "说明它不在当前喜欢清单中及新鲜度依据",
        "text": "完整中文推荐说明"
      },
      "sources": ["https://example.com/public-source"],
      "platform_links": {"apple_music": "https://..."}
    }
  ]
}

下面是唯一允许使用的分析包：
"""


def build_agent_input(packet: dict[str, Any]) -> dict[str, Any]:
    """Return the Step 2-only projection needed by the Agent.

    Platform links and input manifests are intentionally omitted. They are not
    needed for candidate research and would make it easier to mistake a source
    playlist link for a recommendation source.
    """

    validate_analysis_packet(packet)
    return {
        "schema_version": packet["schema_version"],
        "packet_type": packet["packet_type"],
        "analysis_id": packet["analysis_id"],
        "source_snapshot_id": packet["source_snapshot_id"],
        "source_platform": packet.get("source_platform", ""),
        "source_playlist_name": packet.get("source_playlist_name", ""),
        "source_track_count": packet["source_track_count"],
        "favorite_track_keys": packet["favorite_track_keys"],
        "favorite_tracks": [
            {
                key: track[key]
                for key in ("position", "title", "artist", "artists", "album", "track_key")
                if key in track
            }
            for track in packet["favorite_tracks"]
        ],
        "primary_distribution": packet["primary_distribution"],
        "credited_distribution": packet["credited_distribution"],
        "preferred_artists": packet["preferred_artists"],
        "entities": packet["entities"],
        "analysis_ref_ids": packet["analysis_ref_ids"],
        "recommendation_policy": packet["recommendation_policy"],
    }


def build_agent_prompt(packet: dict[str, Any]) -> str:
    validate_analysis_packet(packet)
    encoded = json.dumps(build_agent_input(packet), ensure_ascii=False, indent=2)
    return AGENT_INSTRUCTIONS + "\n```json\n" + encoded + "\n```\n"


def build_agent_prompt_from_file(analysis_path: Path, output_path: Path | None = None) -> str:
    from contracts import read_json

    value = read_json(analysis_path)
    if not isinstance(value, dict):
        raise ContractError(f"分析包不是 JSON 对象：{analysis_path}")
    prompt = build_agent_prompt(value)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(prompt, encoding="utf-8")
    return prompt


def write_agent_context_manifest(packet: dict[str, Any], output_path: Path) -> None:
    validate_analysis_packet(packet)
    write_json(
        output_path,
        {
            "schema_version": "1.0",
            "manifest_type": "agent_context_manifest",
            "analysis_id": packet["analysis_id"],
            "source_snapshot_id": packet["source_snapshot_id"],
            "source_track_count": packet["source_track_count"],
            "allowed_input": "MusicianAnalysisPacket only",
            "forbidden_inputs": [
                "raw PlaylistSnapshot",
                "platform login profiles",
                "recommendation_history",
                "previous Agent output",
                "personalized platform recommendations",
            ],
        },
    )
