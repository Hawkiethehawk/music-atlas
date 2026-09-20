#!/usr/bin/env python3
"""Render the isolated Step 3 Skill context.

The legacy module and function names are retained for compatibility; the
prompt itself is model- and provider-neutral.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from contracts import ContractError, SCHEMA_VERSION, read_json, require_analysis_coverage, sha256_path, stable_hash, validate_analysis_packet, write_json


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


RECOMMENDATION_SKILL_INSTRUCTIONS = """你是 Music Atlas 候选研究 Skill。唯一偏好输入是下方 MusicianAnalysisPacket。

硬性边界：
1. 不读取平台登录态、原始歌单、历史推荐、上一轮结果或个性化推荐页面。
2. 只提交公开资料支持的候选事实，不提交评分；七维分数、配额、去重、多样性和顺序全部由程序计算。
3. Apple Music 只能作为跳转链接。候选证据使用官方页面、MusicBrainz、Wikidata、Last.fm、ListenBrainz、Bandcamp、公开 YouTube 或 Spotify 页面。
4. 每个候选必须同时具备歌曲身份和风格证据；音乐人关系候选还必须具备关系证据。证据不足时返回 insufficient_evidence。
5. 风格必须使用 known_style_refs；允许使用不在 active_style_refs 中的新风格，由程序计算其与当前画像的距离。
6. 每位艺人独立判断，不能使用宽泛“摇滚”兜底，也不能为 Bad Omens 设置特殊逻辑。
7. 只研究结构化候选，不为全部候选撰写最终推荐说明；程序最多选出 target_recommendations 首（候选不足时按可用数量收缩）后，根据兴趣组、关系与实际评分生成说明。
8. candidate_type 由程序复核：艺人延伸必须匹配当前艺人，音乐人关系必须匹配当前分析包中的项目艺人；其余候选按与最近兴趣组的风格/听感距离分类。researched 关系来自 Step 2 研究 Skill，仍待独立核验；不可用随意引用冒充关系。
9. 按 interest_profiles 分组分别研究，避免只选整体平均听感。若有 research_request，只补其指定的缺额/约束，遵守剩余数量预算和去重清单；这不是历史偏好输入。若 research_request.parallel_worker 存在，这是并行分片：只返回 requested_type_counts 指定类型、最多 candidate_budget 首；不同分片之间可能出现重复，程序会统一去重。

首轮候选池至少达到 recommendation_policy.candidate_pool_min；候选充足时尽量覆盖 recall_mix 的 candidate_type，候选不足时不为凑齐类型虚构结果。补充轮以 research_request 为准，不必重复首轮的最低数量或全部类型。候选不得命中 favorite_track_keys 或相同 platform_track_id。style_mix 权重合计为 1；style_axes 必须填写八个 0 到 100 的听感轴。canonical_track_id 使用可稳定审计的外部标识，例如 musicbrainz:recording-id。

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
    "sources": ["https://..."],
    "platform_links": {"apple_music": "https://..."}
  }],
  "recommendations": []
}

insufficient_evidence 时 bundle_stage 使用 final，candidate_pool 和 recommendations 均为空，并提供 message。

模型、供应商、SDK 和工具由外部执行环境决定；不要在结果中声明或要求特定模型。下面是唯一允许使用的分析包：
"""

# Compatibility name used by existing prompt manifests and tests.
AGENT_INSTRUCTIONS = RECOMMENDATION_SKILL_INSTRUCTIONS


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


_COMPACT_PAYLOAD_FIELDS = {
    "sources",
    "evidence_items",
    "field_provenance",
    "rationale",
    "boundaries",
    "verification_scope",
    "research_origin",
}


def _strip_research_detail(value: Any) -> Any:
    """Keep preference conclusions while removing duplicated Step 2 evidence."""

    if isinstance(value, list):
        return [_strip_research_detail(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_research_detail(item)
            for key, item in value.items()
            if key not in _COMPACT_PAYLOAD_FIELDS
        }
    return value


def _compact_agent_payload(payload: dict[str, Any], *, aggressive: bool = False) -> dict[str, Any]:
    """Return a bounded Step 3 projection without altering its decision inputs."""

    compact = _strip_research_detail(deepcopy(payload))
    compact.pop("analysis_research", None)
    style_analysis = compact.get("style_analysis")
    if isinstance(style_analysis, dict):
        # interest_profiles already expose the grouped preference model used by
        # candidate research; the raw assignment expansion is redundant here.
        style_analysis.pop("interest_model", None)
    if not aggressive:
        return compact

    compact.pop("track_style_exceptions", None)
    if isinstance(style_analysis, dict):
        style_analysis.pop("artist_profiles", None)
        style_analysis.pop("style_definitions", None)
        style_analysis.pop("overlap_style_distribution", None)
    entities = compact.get("entities")
    if isinstance(entities, list):
        compact_entities = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            compact_entity = {
                key: entity[key]
                for key in (
                    "entity_ref",
                    "name",
                    "entity_type",
                    "primary_track_count",
                    "credited_track_count",
                    "is_preferred",
                    "relation_status",
                )
                if key in entity
            }
            for field, keys in (
                ("lead_vocalists", ("name", "role", "status")),
                ("related_projects", ("name", "person", "relation")),
            ):
                facts = entity.get(field)
                if isinstance(facts, list):
                    compact_entity[field] = [
                        {key: fact[key] for key in keys if isinstance(fact, dict) and key in fact}
                        for fact in facts
                    ]
            compact_entities.append(compact_entity)
        compact["entities"] = sorted(
            compact_entities,
            key=lambda entity: (
                not bool(entity.get("is_preferred")),
                -int(entity.get("primary_track_count", 0)),
                -int(entity.get("credited_track_count", 0)),
                str(entity.get("name", "")).casefold(),
            ),
        )[:24]
    return compact


def apply_context_budget(
    prompt: str,
    context_budget: int | None,
    *,
    allow_payload_compaction: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Deterministically fit a prompt into a character budget.

    The instruction block is never truncated. The JSON payload may be
    deterministically compacted for initial context preparation, while callers
    extending an already prepared prompt can disable that behavior. Optional
    editable prompt slots are dropped in slot order and the drop is recorded.
    Returns the resulting prompt plus a budget report.
    """

    if context_budget is not None and (
        isinstance(context_budget, bool) or not isinstance(context_budget, int) or context_budget <= 0
    ):
        raise ContractError("context_budget 必须是正整数字符数")
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
        raise ContractError("提示词超预算且无法识别其结构；未调用 Agent")
    body, payload = prompt[:json_index], prompt[json_index:]
    slot_section = body[len(AGENT_INSTRUCTIONS):]
    minimum = len(AGENT_INSTRUCTIONS) + len(payload)
    payload_compacted = False
    original_payload_characters = len(payload)
    if minimum > context_budget and not allow_payload_compaction:
        raise ContractError(f"提示词固定指令和分析载荷至少需要 {minimum} 字符，超过预算 {context_budget}；未调用 Agent")
    if minimum > context_budget:
        try:
            payload_value = json.loads(payload[len(marker):].rsplit("\n```", 1)[0])
        except json.JSONDecodeError as exc:
            raise ContractError("提示词固定载荷不是合法 JSON；未调用 Agent") from exc
        if not isinstance(payload_value, dict):
            raise ContractError("提示词固定载荷必须是 JSON 对象；未调用 Agent")
        for aggressive in (False, True):
            compact_value = _compact_agent_payload(payload_value, aggressive=aggressive)
            compact_payload = marker + json.dumps(compact_value, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
            compact_minimum = len(AGENT_INSTRUCTIONS) + len(compact_payload)
            if compact_minimum <= context_budget:
                payload = compact_payload
                minimum = compact_minimum
                payload_compacted = True
                break
        if minimum > context_budget:
            raise ContractError(f"提示词固定指令和紧凑分析载荷至少需要 {minimum} 字符，超过预算 {context_budget}；未调用 Agent")
    available = context_budget - minimum
    parts = slot_section.split("\n## Prompt Slot: ")
    preamble = parts[0] if len(parts[0]) <= available else ""
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
    report = {
        **prompt_size_telemetry(rebuilt),
        "context_budget": context_budget,
        "budget_exceeded": len(rebuilt) > context_budget,
        "original_budget_exceeded": True,
        "truncated_slots": truncated_slots,
        "original_characters": telemetry["prompt_characters"],
    }
    if payload_compacted:
        report.update(
            payload_compacted=True,
            original_payload_characters=original_payload_characters,
            compact_payload_characters=len(payload),
        )
    return rebuilt, report


def build_agent_input(packet: dict[str, Any]) -> dict[str, Any]:
    """Return the Step 2-only projection needed by the Agent.

    Platform links and input manifests are intentionally omitted. They are not
    needed for candidate research and would make it easier to mistake a source
    playlist link for a recommendation source.
    """

    validate_analysis_packet(packet)
    require_analysis_coverage(packet)
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
                "style_mix",
                "style_axes",
                "rationale",
                "sources",
                "evidence_items",
                "field_provenance",
            )
            if key in assignment
        }
        for assignment in packet["track_style_assignments"]
        if assignment.get("applied_scope") in {"release_override", "agent_release", "agent_track"}
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
                "sources",
            )
            if key in profile
        }
        for profile in style_analysis["artist_profiles"]
    ]
    compact_style_analysis = {
        key: style_analysis[key]
        for key in (
            "taxonomy_version",
            "profile_catalog_mode",
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
            "interest_model",
            "interest_profiles",
        )
        if key in style_analysis
    }
    compact_style_analysis["artist_profiles"] = compact_profiles
    confirmed_entities = [
        entity for entity in packet["entities"] if entity.get("relation_status") in {"confirmed", "researched"}
    ]
    return {
        "schema_version": packet["schema_version"],
        "packet_type": packet["packet_type"],
        "analysis_id": packet["analysis_id"],
        "as_of_date": packet["as_of_date"],
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
        "analysis_research": packet.get("analysis_research"),
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
        "prompt_input_sha256": stable_hash(build_agent_input(packet)),
        "instruction_sha256": hashlib.sha256(AGENT_INSTRUCTIONS.encode("utf-8")).hexdigest(),
        "run_config": {
            "context_budget": context_budget,
            "prompt_dir": str((prompt_dir or DEFAULT_PROMPT_DIR).resolve()),
        },
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
        manifest["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if context_budget is not None:
        manifest["context_budget"] = context_budget
    if budget_report is not None:
        manifest["budget_report"] = budget_report
    write_json(output_path, manifest)


def prepare_agent_context(
    packet: dict[str, Any], prompt_path: Path, *, manifest_path: Path | None = None,
    prompt_dir: Path | None = None, context_budget: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Write a context only after its complete payload fits the requested budget."""

    prompt, report = apply_context_budget(build_agent_prompt(packet, prompt_dir), context_budget)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    write_agent_context_manifest(
        packet, manifest_path or prompt_path.with_name("agent_context_manifest.json"), prompt_dir,
        prompt=prompt, context_budget=context_budget, budget_report=report,
    )
    return prompt, report


def load_or_prepare_agent_context(
    packet: dict[str, Any], prompt_path: Path, *, manifest_path: Path | None = None,
    prompt_dir: Path | None = None, context_budget: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Reuse the exact prepared context; configuration changes require preparation."""

    manifest_path = manifest_path or prompt_path.with_name("agent_context_manifest.json")
    if not prompt_path.exists() and not manifest_path.exists():
        prepare_agent_context(packet, prompt_path, manifest_path=manifest_path,
                              prompt_dir=prompt_dir, context_budget=context_budget)
    if not prompt_path.is_file() or not manifest_path.is_file():
        raise ContractError("Agent 上下文或 manifest 缺失；请重新执行 prepare-agent")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("manifest_type") != "agent_context_manifest":
        raise ContractError("Agent context manifest 无效；请重新执行 prepare-agent")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "analysis_id": packet["analysis_id"],
        "prompt_input_sha256": stable_hash(build_agent_input(packet)),
        "instruction_sha256": hashlib.sha256(AGENT_INSTRUCTIONS.encode("utf-8")).hexdigest(),
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ContractError("准备的上下文不属于当前分析或缺少有效摘要；请重新执行 prepare-agent")
    config = manifest.get("run_config")
    if not isinstance(config, dict) or set(config) != {"context_budget", "prompt_dir"}:
        raise ContractError("准备的上下文缺少运行配置；请重新执行 prepare-agent")
    if not isinstance(config["prompt_dir"], str) or not config["prompt_dir"]:
        raise ContractError("准备的 prompt_dir 无效；请重新执行 prepare-agent")
    if context_budget is not None and context_budget != config["context_budget"]:
        raise ContractError("context_budget 与准备阶段不同；请重新执行 prepare-agent")
    if prompt_dir is not None and prompt_dir.resolve() != Path(config["prompt_dir"]).resolve():
        raise ContractError("prompt_dir 与准备阶段不同；请重新执行 prepare-agent")
    prompt = prompt_path.read_text(encoding="utf-8")
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != manifest.get("prompt_sha256"):
        raise ContractError("准备的提示词已被修改；请重新执行 prepare-agent")
    # Reuse must not silently truncate or regenerate the prepared artifact.
    checked, _ = apply_context_budget(prompt, config["context_budget"])
    if checked != prompt:
        raise ContractError("准备的提示词超过记录预算；请重新执行 prepare-agent")
    report = manifest.get("budget_report")
    if not isinstance(report, dict) or report.get("context_budget") != config["context_budget"]:
        raise ContractError("准备的预算报告与运行配置不一致；请重新执行 prepare-agent")
    return prompt, manifest


# Provider-neutral public names. Keep the Agent aliases above so existing
# runtime artifacts and integrations can be migrated without a hard cutover.
build_skill_input = build_agent_input
build_skill_prompt = build_agent_prompt
prepare_skill_context = prepare_agent_context
load_or_prepare_skill_context = load_or_prepare_agent_context
