#!/usr/bin/env python3
"""Ranked RecommendationBundle 的内部纯文本报告。

发送途径（微信 / OpenClaw / Pi wechatbot）已从本仓库移除：本模块只生成供流程审计
与日志使用的文本报告，**不发送任何消息**，也不包含任何渠道专属格式。

写出报告同时是输出门禁：程序重排序（``rank_bundle``）与逐条推荐的证据可用性
（``require_usable_evidence``）都必须先通过，才会得到报告文本。
"""

from __future__ import annotations

from typing import Any

from evidence import require_usable_evidence
from recommender import rank_bundle


def _link_lines(links: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for platform, link in links.items():
        if isinstance(link, str) and link.strip():
            lines.append(f"{platform}: {link.strip()}")
    return lines


def render_report(
    bundle: dict[str, Any],
    analysis: dict[str, Any],
    *,
    title: str = "本周音乐推荐",
    include_audit_links: bool = False,
) -> str:
    """Render the internal report and enforce the recommendation gate."""

    bundle = rank_bundle(bundle, analysis)
    lines = [title, f"分析包：{analysis['analysis_id']}", f"偏好清单歌曲数：{analysis['source_track_count']}", ""]
    if bundle["status"] == "insufficient_evidence":
        lines.extend(["本周没有足够的公开证据生成推荐。", bundle["message"]])
        return "\n".join(lines)
    lines[0] = f"{title}（研究草稿）"
    lines.extend(["公开事实尚未核验，本结果不是正式推荐。", ""])
    for index, recommendation in enumerate(bundle["recommendations"], 1):
        require_usable_evidence(recommendation)
        album = recommendation.get("album")
        album_suffix = f"《{album}》" if isinstance(album, str) and album.strip() else ""
        lines.append(f"{index}. {recommendation['title']} - {recommendation['artist']} {album_suffix}".rstrip())
        if "ranking_score" in recommendation:
            lines.append(
                f"   综合分：{float(recommendation['ranking_score']):.1f}；"
                f"召回：{recommendation.get('candidate_type', 'unknown')}"
            )
        explanation = recommendation["program_explanation"]
        lines.append(f"   风格匹配：{explanation['style_fit']}")
        lines.append(f"   {explanation['text']}")
        if include_audit_links:
            links = _link_lines(recommendation.get("platform_links", {}))
            if links:
                lines.extend(f"   {line}" for line in links)
            sources = recommendation.get("sources", [])
            if isinstance(sources, list) and sources:
                lines.append("   依据：" + "；".join(str(source) for source in sources[:2]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
