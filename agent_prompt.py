#!/usr/bin/env python3
"""Render the isolated Step 3 Agent context."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from contracts import ContractError, SCHEMA_VERSION, sha256_path, validate_analysis_packet, write_json


DEFAULT_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
PROMPT_SLOT_FILES = (
    "artist_profile.md",
    "style_taxonomy.md",
    "track_style_rationale.md",
    "candidate_recall.md",
    "candidate_ranking.md",
    "recommendation_explanation.md",
    "playlist_sequence.md",
)


AGENT_INSTRUCTIONS = """你是 Music Atlas 候选研究 Agent。唯一偏好输入是下方 MusicianAnalysisPacket。

硬性边界：
1. 不读取平台登录态、原始歌单、历史推荐、上一轮结果或个性化推荐页面。
2. 只提交公开资料支持的候选事实，不提交评分；七维分数、配额、去重、多样性和顺序全部由程序计算。
3. Apple Music 只能作为跳转链接。候选证据使用官方页面、MusicBrainz、Wikidata、Last.fm、ListenBrainz、Bandcamp、公开 YouTube 或 Spotify 页面。
4. 每个候选必须同时具备歌曲身份和风格证据；音乐人关系候选还必须具备关系证据。证据不足时返回 insufficient_evidence。
5. 风格必须使用 known_style_refs；允许使用不在 active_style_refs 中的新风格，由程序计算其与当前画像的距离。
6. 每位艺人独立判断，不能使用宽泛“摇滚”兜底，也不能为 Bad Omens 设置特殊逻辑。

候选池必须达到 recommendation_policy.candidate_pool_min，并覆盖 recall_mix 的四种 candidate_type。候选不得命中 favorite_track_keys 或相同 platform_track_id。style_mix 权重合计为 1；style_axes 必须填写八个 0 到 100 的听感轴。canonical_track_id 使用可稳定审计的外部标识，例如 musicbrainz:recording-id。

每条 evidence_items 包含 claim_type、claim、url；claim_type 只能是 track_identity、style、relation、release。evidence_items 中的 URL 也必须列入 sources。evidence_grade 只能是 A、B、C，style_confidence 只能是 high、medium、low。

只输出一个 JSON 对象，不要 Markdown。ready 输出格式：
{
  "schema_version": "2.0",
  "bundle_type": "recommendation_bundle",
  "bundle_stage": "candidate_pool",
  "status": "ready",
  "analysis_id": "等于 packet.analysis_id",
  "generated_at": "ISO-8601",
  "candidate_pool": [{
    "canonical_track_id": "musicbrainz:recording-id",
    "platform_track_id": "可选平台歌曲 ID",
    "title": "歌曲名",
    "artist": "艺人名",
    "project": "发行项目",
    "release_date": "可选 YYYY-MM-DD",
    "candidate_type": "artist_continuation | musician_relation | style_neighbor | exploration",
    "analysis_refs": ["packet.analysis_ref_ids 中的引用"],
    "relation_path": ["当前偏好锚点", "关系或风格路径", "候选歌曲"],
    "style_refs": ["style:..."],
    "style_mix": [{"style_ref": "style:...", "role": "primary", "weight": 1.0}],
    "style_axes": {"heaviness": 0, "aggression": 0, "atmosphere": 0, "electronic_presence": 0, "pop_accessibility": 0, "rhythmic_density": 0, "vocal_harshness": 0, "emotional_intensity": 0},
    "style_confidence": "high | medium | low",
    "evidence_grade": "A | B | C",
    "evidence_items": [{"claim_type": "track_identity", "claim": "事实", "url": "https://..."}, {"claim_type": "style", "claim": "事实", "url": "https://..."}],
    "discovery_source": "公开来源类型",
    "explanation": {"preference_basis": "具体偏好依据", "artist_relation": "关系或风格路径", "music_fit": "可核验音乐特征", "style_fit": "细分风格与听感匹配", "novelty": "相对当前清单的新鲜点", "text": "不少于 24 字的完整中文说明"},
    "sources": ["https://..."],
    "platform_links": {"apple_music": "https://..."}
  }],
  "recommendations": []
}

insufficient_evidence 时 bundle_stage 使用 final，candidate_pool 和 recommendations 均为空，并提供 message。

下面是唯一允许使用的分析包：
"""


def load_prompt_slots(prompt_dir: Path | None = None) -> list[dict[str, str]]:
    """Load user-editable prompt additions in a deterministic order."""

    directory = Path(prompt_dir) if prompt_dir is not None else DEFAULT_PROMPT_DIR
    slots: list[dict[str, str]] = []
    for filename in PROMPT_SLOT_FILES:
        path = directory / filename
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8").strip()
        if content:
            slots.append({"name": path.stem, "path": str(path), "content": content})
    return slots


def _render_prompt_slot(content: str, packet: dict[str, Any]) -> str:
    policy = packet["recommendation_policy"]
    replacements = {
        "RANKING_WEIGHTS": json.dumps(policy["ranking_weights"], ensure_ascii=False, separators=(",", ":")),
        "RECALL_MIX": json.dumps(policy["recall_mix"], ensure_ascii=False, separators=(",", ":")),
        "SEQUENCE_RULES": json.dumps(policy["sequence_policy"], ensure_ascii=False, separators=(",", ":")),
    }
    for key, value in replacements.items():
        content = content.replace("{{" + key + "}}", value)
    return re.sub(r"\{\{[A-Z0-9_]+\}\}", "无额外要求", content)


def prompt_slot_manifest(prompt_dir: Path | None = None) -> list[dict[str, str]]:
    """Return hashes for prompt additions so a run can be reproduced."""

    return [
        {"name": slot["name"], "path": slot["path"], "sha256": sha256_path(Path(slot["path"]))}
        for slot in load_prompt_slots(prompt_dir)
    ]


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    """Deterministic prompt-size estimate (characters / chars-per-token)."""

    return max(1, len(text) // max(1, chars_per_token))


def prompt_size_telemetry(prompt: str) -> dict[str, int]:
    """Report prompt size without any network or model dependency."""

    return {
        "prompt_characters": len(prompt),
        "prompt_lines": prompt.count("\n") + 1,
        "estimated_tokens": estimate_tokens(prompt),
        "chars_per_token_estimate": 4,
    }


def apply_context_budget(
    prompt: str,
    context_budget: int | None,
) -> tuple[str, dict[str, Any]]:
    """Deterministically truncate prompt slots to fit a character budget.

    The instruction block and the JSON payload are never truncated because
    doing so would break the Agent contract. Only trailing editable prompt
    slots are dropped, in slot order, and the drop is recorded. Returns the
    resulting prompt plus a budget report.
    """

    telemetry = prompt_size_telemetry(prompt)
    if context_budget is None or telemetry["prompt_characters"] <= context_budget:
        return prompt, {
            **telemetry,
            "context_budget": context_budget,
            "budget_exceeded": False,
            "truncated_slots": [],
        }
    marker = "\n```json\n"
    json_index = prompt.rfind(marker)
    if json_index < 0 or not prompt.startswith(AGENT_INSTRUCTIONS):
        return prompt, {
            **telemetry,
            "context_budget": context_budget,
            "budget_exceeded": True,
            "truncated_slots": [],
            "note": "无法确定性截断未知的提示词结构；未做修改。",
        }
    body, payload = prompt[:json_index], prompt[json_index:]
    slot_section = body[len(AGENT_INSTRUCTIONS):]
    available = max(0, context_budget - len(AGENT_INSTRUCTIONS) - len(payload))
    parts = slot_section.split("\n## Prompt Slot: ")
    preamble = parts[0]
    slots_kept: list[str] = []
    truncated_slots: list[str] = []
    kept_size = len(preamble)
    for slot in parts[1:]:
        item = "\n## Prompt Slot: " + slot
        if kept_size + len(item) <= available:
            slots_kept.append(item)
            kept_size += len(item)
        else:
            truncated_slots.append(slot.splitlines()[0])
    rebuilt = AGENT_INSTRUCTIONS + preamble + "".join(slots_kept) + payload
    return rebuilt, {
        **prompt_size_telemetry(rebuilt),
        "context_budget": context_budget,
        "budget_exceeded": telemetry["prompt_characters"] > context_budget,
        "truncated_slots": truncated_slots,
        "original_characters": telemetry["prompt_characters"],
    }


def build_agent_input(packet: dict[str, Any]) -> dict[str, Any]:
    """Return the Step 2-only projection needed by the Agent.

    Platform links and input manifests are intentionally omitted. They are not
    needed for candidate research and would make it easier to mistake a source
    playlist link for a recommendation source.
    """

    validate_analysis_packet(packet)
    track_style_exceptions = [
        {
            key: assignment[key]
            for key in (
                "position",
                "track_key",
                "title",
                "artist",
                "album",
                "classification_status",
                "confidence",
                "primary_style_ref",
                "style_refs",
                "applied_scope",
            )
            if key in assignment
        }
        for assignment in packet["track_style_assignments"]
        if assignment.get("applied_scope") == "release_override"
        or assignment.get("classification_status") == "unclassified"
    ]
    style_analysis = packet["style_analysis"]
    compact_profiles = [
        {
            key: profile[key]
            for key in (
                "artist",
                "entity_ref",
                "primary_track_count",
                "credited_track_count",
                "classification_status",
                "confidence",
                "style_mix",
                "style_axes",
                "summary",
                "boundaries",
            )
            if key in profile
        }
        for profile in style_analysis["artist_profiles"]
    ]
    compact_style_analysis = {
        key: style_analysis[key]
        for key in (
            "taxonomy_version",
            "known_style_refs",
            "active_style_refs",
            "style_definitions",
            "axis_definitions",
            "frequency_basis",
            "classified_track_count",
            "unclassified_track_count",
            "overlap_style_distribution",
            "style_axes",
            "profile_coverage",
        )
        if key in style_analysis
    }
    compact_style_analysis["artist_profiles"] = compact_profiles
    confirmed_entities = [
        entity for entity in packet["entities"] if entity.get("relation_status") == "confirmed"
    ]
    return {
        "schema_version": packet["schema_version"],
        "packet_type": packet["packet_type"],
        "analysis_id": packet["analysis_id"],
        "source_snapshot_id": packet["source_snapshot_id"],
        "source_platform": packet.get("source_platform", ""),
        "source_playlist_name": packet.get("source_playlist_name", ""),
        "source_track_count": packet["source_track_count"],
        "favorite_track_keys": packet["favorite_track_keys"],
        "favorite_platform_track_ids": [
            track["platform_track_id"]
            for track in packet["favorite_tracks"]
            if track.get("platform_track_id")
        ],
        "primary_distribution": packet["primary_distribution"],
        "preferred_artists": packet["preferred_artists"],
        "entities": confirmed_entities,
        "analysis_ref_ids": packet["analysis_ref_ids"],
        "track_style_exceptions": track_style_exceptions,
        "style_analysis": compact_style_analysis,
        "recommendation_policy": packet["recommendation_policy"],
    }


def build_agent_prompt(packet: dict[str, Any], prompt_dir: Path | None = None) -> str:
    validate_analysis_packet(packet)
    encoded = json.dumps(build_agent_input(packet), ensure_ascii=False, separators=(",", ":"))
    prompt = AGENT_INSTRUCTIONS
    slots = load_prompt_slots(prompt_dir)
    if slots:
        prompt += "\n\n以下是可编辑的提示词插槽。它们只能补充判断标准和表达要求，不能改变数量契约、数据边界或输出字段。\n"
        for slot in slots:
            rendered = _render_prompt_slot(slot["content"], packet)
            prompt += f"\n## Prompt Slot: {slot['name']}\n{rendered}\n"
    return prompt + "\n```json\n" + encoded + "\n```\n"


def build_agent_prompt_from_file(
    analysis_path: Path,
    output_path: Path | None = None,
    prompt_dir: Path | None = None,
) -> str:
    from contracts import read_json

    value = read_json(analysis_path)
    if not isinstance(value, dict):
        raise ContractError(f"分析包不是 JSON 对象：{analysis_path}")
    prompt = build_agent_prompt(value, prompt_dir=prompt_dir)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(prompt, encoding="utf-8")
    return prompt


def write_agent_context_manifest(
    packet: dict[str, Any],
    output_path: Path,
    prompt_dir: Path | None = None,
    *,
    prompt: str | None = None,
    context_budget: int | None = None,
    budget_report: dict[str, Any] | None = None,
) -> None:
    validate_analysis_packet(packet)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_type": "agent_context_manifest",
        "analysis_id": packet["analysis_id"],
        "source_snapshot_id": packet["source_snapshot_id"],
        "source_track_count": packet["source_track_count"],
        "allowed_input": "MusicianAnalysisPacket only",
        "prompt_slots": prompt_slot_manifest(prompt_dir),
        "forbidden_inputs": [
            "raw PlaylistSnapshot",
            "platform login profiles",
            "recommendation_history",
            "previous Agent output",
            "personalized platform recommendations",
        ],
    }
    if prompt is not None:
        manifest["prompt_size"] = prompt_size_telemetry(prompt)
    if context_budget is not None:
        manifest["context_budget"] = context_budget
    if budget_report is not None:
        manifest["budget_report"] = budget_report
    write_json(output_path, manifest)
