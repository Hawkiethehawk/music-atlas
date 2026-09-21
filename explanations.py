"""Explain only program-selected tracks using current-packet evidence and scores."""

from __future__ import annotations

from typing import Any

from musician_analyzer import STYLE_AXIS_LABELS
from preference_model import match_interest


def explain_selected(candidate: dict[str, Any], packet: dict[str, Any]) -> dict[str, str]:
    interest = match_interest(candidate, packet)
    route = candidate["resolved_route"]
    examples = interest["representative_tracks"][:2] if interest else []
    basis = "本次收藏中的" + "、".join(f"《{item['title']}》({item['artist']})" for item in examples) if examples else "本次明确配置的艺人偏好"
    origin = "Agent 研究路径：" if route["verification_scope"] == "current_packet_agent_relation" else "本次艺人路径：" if route["candidate_type"] == "artist_continuation" else "目录路径："
    relation = (origin + " → ".join(route["path"]) + "；尚未在线核验") if route["path"] else "未确认音乐人关系，本曲按描述性风格/听感发现"
    axes = interest["style_axes"] if interest else {}
    candidate_axes = candidate.get("style_axes", {})
    closest = sorted(
        [axis for axis in axes if axes[axis] is not None and candidate_axes.get(axis) is not None],
        key=lambda axis: (abs(axes[axis] - candidate_axes[axis]), axis),
    )[:3]
    fit = "；".join(f"{STYLE_AXIS_LABELS[axis]}：候选 {(candidate.get('style_axes') or {}).get(axis, 0):.0f} / 兴趣组 {axes[axis]:.0f}" for axis in closest)
    fit = "描述性画像估计，非音频实测：" + (fit or "听感依据不足")
    labels = {item["style_ref"]: item["label"] for item in packet["style_analysis"]["style_definitions"]}
    styles = "、".join(labels.get(ref, ref) for ref in candidate.get("style_refs") or []) or "风格资料不足"
    scores = candidate.get("score_features") or {}
    style_fit = (f"{styles}；风格与八轴资料不足，已从本首评分中排除"
                 if candidate.get("style_status") == "unclassified" or "style_fit" not in scores
                 else f"{styles}；风格匹配 {scores['style_fit']:.1f}，听感匹配 {scores.get('axis_fit', 0.0):.1f}（均非喜欢概率）")
    novelty = f"未命中本次收藏；程序判定为 {route['candidate_type']}，新鲜度仅相对本次输入"
    return {"preference_basis": basis, "artist_relation": relation, "music_fit": fit, "style_fit": style_fit,
            "novelty": novelty, "text": f"承接{basis}。{relation}。{fit}。{style_fit}。{novelty}。曲目身份来自本次公开平台记录。"}
