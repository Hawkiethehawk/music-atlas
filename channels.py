#!/usr/bin/env python3
"""Platform-neutral RecommendationBundle rendering and output adapters."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Protocol

from contracts import ContractError
from evidence import require_usable_evidence
from recommender import rank_bundle


_CANDIDATE_TYPE_LABELS = {
    "artist_continuation": "同艺人延伸",
    "musician_relation": "关联音乐人",
    "style_neighbor": "风格邻近",
    "exploration": "探索方向",
}
_MUSIC_FIT_PATTERN = re.compile(r"([^：:；;]+)：候选\s*([0-9]+(?:\.[0-9]+)?)")
_WEIXIN_HARD_BREAK = "  \n"


class OutputAdapter(Protocol):
    name: str

    def render(self, bundle: dict[str, Any], analysis: dict[str, Any]) -> str:
        ...


def _link_lines(links: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for platform, link in links.items():
        if isinstance(link, str) and link.strip():
            lines.append(f"{platform}: {link.strip()}")
    return lines


def render_text(
    bundle: dict[str, Any],
    analysis: dict[str, Any],
    *,
    title: str = "本周音乐推荐",
    include_audit_links: bool = False,
) -> str:
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


def _weixin_level(value: float) -> str:
    if value >= 75:
        return "很高"
    if value >= 60:
        return "偏高"
    if value >= 45:
        return "中等"
    if value >= 30:
        return "偏低"
    return "较低"


def _weixin_style(explanation: dict[str, Any]) -> str:
    style_fit = str(explanation.get("style_fit", "")).strip()
    return style_fit.split("；", 1)[0].strip() or "未标注风格"


def _weixin_route(recommendation: dict[str, Any], explanation: dict[str, Any]) -> str | None:
    candidate_type = recommendation.get("candidate_type")
    if candidate_type == "artist_continuation":
        return f"同艺人延伸：{recommendation['artist']}"
    if candidate_type != "musician_relation":
        return None

    relation = str(explanation.get("artist_relation", "")).strip()
    relation = relation.split("；", 1)[0].strip()
    for prefix in ("Agent 研究路径：", "本次艺人路径：", "目录路径："):
        if relation.startswith(prefix):
            path = relation[len(prefix):].strip()
            if path:
                return f"关联路径：{path}"
    return None


def _weixin_music_fit(explanation: dict[str, Any]) -> str:
    music_fit = str(explanation.get("music_fit", "")).strip()
    matches = _MUSIC_FIT_PATTERN.findall(music_fit)
    if not matches:
        return "描述性听感信息不足"
    parts = [f"{label.strip()}{_weixin_level(float(value))}" for label, value in matches[:3]]
    return "、".join(parts)


def _weixin_platform_label(platform: Any) -> str:
    label = str(platform).strip()
    return {"spotify": "Spotify", "apple_music": "Apple Music"}.get(label.lower(), label)


def _weixin_reason(recommendation: dict[str, Any], explanation: dict[str, Any]) -> str:
    style = _weixin_style(explanation)
    candidate_type = recommendation.get("candidate_type")

    if candidate_type == "artist_continuation":
        return f"沿 {recommendation['artist']} 的同艺人路径继续探索，延续{style}方向。"
    if candidate_type == "musician_relation":
        return f"通过关联音乐人路径拓展{style}的相近听感。"
    if candidate_type == "style_neighbor":
        return f"从{style}的邻近方向切入，增加新的听感变化。"
    return f"以{style}作为探索支线，扩大整体风格覆盖。"


def _weixin_join_lines(lines: list[str]) -> str:
    """Join visible Weixin lines with a Markdown hard break."""

    return _WEIXIN_HARD_BREAK.join(lines)


def render_weixin_text(bundle: dict[str, Any], analysis: dict[str, Any]) -> str:
    """Render a reader-facing Weixin report without exposing internal scoring fields."""

    bundle = rank_bundle(bundle, analysis)
    blocks = [
        _weixin_join_lines([
            "🎧 本周音乐推荐｜研究草稿",
            f"基于 {analysis['source_track_count']} 首收藏｜精选 {len(bundle.get('recommendations', []))} 首",
        ])
    ]
    if bundle["status"] == "insufficient_evidence":
        blocks.extend([
            f"状态：暂时没有足够的公开证据生成推荐｜说明：{bundle['message']}",
            f"研究记录：{analysis['analysis_id']}",
        ])
        return "\n\n".join(blocks).rstrip() + "\n"

    type_counts = Counter(item.get("candidate_type") for item in bundle["recommendations"])
    route_summary_labels = {
        "artist_continuation": "同艺人",
        "musician_relation": "关联",
        "style_neighbor": "风格",
        "exploration": "探索",
    }
    route_summary = "｜".join(
        f"{route_summary_labels[candidate_type]} {type_counts.get(candidate_type, 0)}"
        for candidate_type in _CANDIDATE_TYPE_LABELS
        if type_counts.get(candidate_type, 0)
    )
    blocks.append(_weixin_join_lines([
        "状态：研究草稿｜公开事实待独立核验",
        "说明：听感为描述性画像，非音频实测；顺序不代表喜欢概率。",
        f"路线：{route_summary}",
    ]))

    for index, recommendation in enumerate(bundle["recommendations"], 1):
        require_usable_evidence(recommendation)
        explanation = recommendation["program_explanation"]
        type_label = _CANDIDATE_TYPE_LABELS.get(recommendation.get("candidate_type"), "风格探索")
        style = _weixin_style(explanation)
        item_title = f"{index}. {recommendation['title']} — {recommendation['artist']}"
        item_fields = [
            f"• 路线：{type_label}｜风格：{style}",
            f"• 推荐理由：{_weixin_reason(recommendation, explanation)}",
            f"• 听感倾向：{_weixin_music_fit(explanation)}",
        ]
        route = _weixin_route(recommendation, explanation)
        if route and recommendation.get("candidate_type") == "musician_relation":
            item_fields.append(f"• {route}")
        links = recommendation.get("platform_links", {})
        if isinstance(links, dict):
            for platform, link in links.items():
                if isinstance(link, str) and link.strip():
                    item_fields.append(f"• 试听：{_weixin_platform_label(platform)} {link.strip()}")
                    break
        blocks.append("\n\n".join([item_title, _weixin_join_lines(item_fields)]))

    blocks.extend([
        _weixin_join_lines([
            f"研究记录：{analysis['analysis_id']}｜偏好清单 {analysis['source_track_count']} 首",
            "公开事实尚未独立核验；以上是研究草稿，不是正式推荐。",
        ]),
    ])
    return "\n\n".join(blocks).rstrip() + "\n"


class WeixinTextAdapter:
    """Hermes 微信渠道使用的纯文本适配器，不负责发送。"""

    name = "weixin"

    def render(self, bundle: dict[str, Any], analysis: dict[str, Any]) -> str:
        return render_weixin_text(bundle, analysis)


class FeishuTextAdapter:
    name = "feishu"

    def render(self, bundle: dict[str, Any], analysis: dict[str, Any]) -> str:
        return render_text(bundle, analysis, title="本周音乐推荐")


class TelegramTextAdapter:
    name = "telegram"

    def render(self, bundle: dict[str, Any], analysis: dict[str, Any]) -> str:
        return render_text(bundle, analysis, title="Weekly Music Recommendations")


ADAPTERS: dict[str, type[OutputAdapter]] = {
    "weixin": WeixinTextAdapter,
    "feishu": FeishuTextAdapter,
    "telegram": TelegramTextAdapter,
}


def render_for_channel(channel: str, bundle: dict[str, Any], analysis: dict[str, Any]) -> str:
    adapter_type = ADAPTERS.get(channel)
    if adapter_type is None:
        raise ContractError(f"不支持的输出渠道：{channel}")
    return adapter_type().render(bundle, analysis)
