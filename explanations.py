"""Explain only program-selected tracks using current-packet evidence and scores."""

from __future__ import annotations

from typing import Any

from preference_model import match_interest


def explain_selected(candidate: dict[str, Any], packet: dict[str, Any]) -> dict[str, str]:
    interest = match_interest(candidate, packet)
    route = candidate["resolved_route"]
    examples = interest["representative_tracks"][:2] if interest else []
    basis = "本次收藏中的" + "、".join(f"《{item['title']}》({item['artist']})" for item in examples) if examples else "本次明确配置的艺人偏好"
    origin = "Agent 研究路径：" if route["verification_scope"] == "current_packet_agent_relation" else "本次艺人路径：" if route["candidate_type"] == "artist_continuation" else "目录路径："
    relation = (origin + " → ".join(route["path"]) + "；按来源区分已核验事实与待核验关系") if route["path"] else "未确认音乐人关系，仅依据公开标签或相似艺人线索发现"
    evidence = candidate.get("style_evidence") or {}
    scope = {"track": "单曲标签", "album": "所属专辑背景", "artist": "艺人整体倾向"}.get(evidence.get("scope"))
    style_sources = [item.get("url") for item in candidate.get("evidence_items", [])
                     if isinstance(item, dict) and item.get("claim_type") == "style" and item.get("url") in (candidate.get("sources") or [])]
    if evidence.get("url") in (candidate.get("sources") or []) and evidence.get("status") == "supported":
        style_sources.append(evidence["url"])
    fit = (f"资料层级：{scope}；来源：{evidence['url']}" if scope and evidence.get("url") in style_sources
           else f"公开风格来源：{style_sources[0]}" if style_sources else "缺少可核查的单曲风格资料")
    labels = {item["style_ref"]: item["label"] for item in packet["style_analysis"]["style_definitions"]}
    refs = candidate.get("style_refs") or [tag.get("style_ref") for tag in evidence.get("tags", [])
                                             if isinstance(tag, dict) and tag.get("style_ref")]
    styles = "、".join(labels.get(ref, ref) for ref in dict.fromkeys(refs)) or "风格资料不足"
    scores = candidate.get("score_features") or {}
    style_fit = (f"{styles}；没有可用的风格关联评分"
                 if not style_sources or "style_fit" not in scores
                 else f"{styles}；公开风格资料关联分 {scores['style_fit']:.1f}（非喜欢概率，不能证明单曲听感）")
    novelty = f"未命中本次收藏；程序判定为 {route['candidate_type']}，新鲜度仅相对本次输入"
    # 只建议如何亲耳比较已知路径，不声称尚未听到的乐器、段落或听感。
    listening_tips = {
        "artist_continuation": "建议与歌单里同艺人的已有曲目对照聆听，再判断两首作品的差异。",
        "musician_relation": "建议与关联路径上的作品对照聆听，再判断具体听感是否相近。",
        "style_neighbor": "建议与相似艺人线索的起点作品对照聆听，验证实际风格距离。",
        "exploration": "建议与歌单中熟悉的作品并排聆听，判断这个探索方向是否适合自己。",
    }
    listening_tip = listening_tips.get(route["candidate_type"], "建议与歌单中熟悉的作品对照聆听，再判断实际差异。")
    return {"preference_basis": basis, "artist_relation": relation, "music_fit": fit, "style_fit": style_fit,
            "novelty": novelty, "listening_tip": listening_tip,
            "text": f"承接{basis}。{relation}。{fit}。{style_fit}。{novelty}。曲目身份来自本次公开平台记录。"}
