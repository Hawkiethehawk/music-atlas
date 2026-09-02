#!/usr/bin/env python3
"""Platform-neutral RecommendationBundle rendering and output adapters."""

from __future__ import annotations

from typing import Any, Protocol

from contracts import ContractError, validate_recommendation_bundle


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


def render_text(bundle: dict[str, Any], analysis: dict[str, Any], *, title: str = "本周音乐推荐") -> str:
    validate_recommendation_bundle(bundle, analysis)
    lines = [title, f"分析包：{analysis['analysis_id']}", f"偏好清单歌曲数：{analysis['source_track_count']}", ""]
    if bundle["status"] == "insufficient_evidence":
        lines.extend(["本周没有足够的公开证据生成推荐。", bundle["message"]])
        return "\n".join(lines)
    for index, recommendation in enumerate(bundle["recommendations"], 1):
        album = recommendation.get("album")
        album_suffix = f"《{album}》" if isinstance(album, str) and album.strip() else ""
        lines.append(f"{index}. {recommendation['title']} - {recommendation['artist']} {album_suffix}".rstrip())
        lines.append(f"   {recommendation['explanation']['text']}")
        links = _link_lines(recommendation.get("platform_links", {}))
        if links:
            lines.extend(f"   {line}" for line in links)
        sources = recommendation.get("sources", [])
        if isinstance(sources, list) and sources:
            lines.append("   依据：" + "；".join(str(source) for source in sources[:2]))
        lines.append("")
    lines.append("说明来源为公开资料；Apple Music 仅用于跳转，不参与候选发现和排序。")
    return "\n".join(lines).rstrip() + "\n"


class WeixinTextAdapter:
    """Hermes 微信渠道使用的纯文本适配器，不负责发送。"""

    name = "weixin"

    def render(self, bundle: dict[str, Any], analysis: dict[str, Any]) -> str:
        return render_text(bundle, analysis, title="本周音乐推荐")


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
