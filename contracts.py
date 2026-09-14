#!/usr/bin/env python3
"""Shared contracts and validation helpers for the music playlist workflow.

The workflow runs on demand, not on a fixed schedule; it always analyzes the
whole playlist provided by the current Step 1 snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


SCHEMA_VERSION = "2.0"

STYLE_AXIS_IDS = (
    "heaviness",
    "aggression",
    "atmosphere",
    "electronic_presence",
    "pop_accessibility",
    "rhythmic_density",
    "vocal_harshness",
    "emotional_intensity",
)

STYLE_CLASSIFICATION_STATUSES = {"classified", "unclassified"}

CANDIDATE_TYPES = {
    "artist_continuation",
    "musician_relation",
    "style_neighbor",
    "exploration",
}

RANKING_FEATURE_KEYS = (
    "style_fit",
    "axis_fit",
    "relation_fit",
    "frequency_fit",
    "novelty",
    "evidence_quality",
    "public_association",
)

PROGRAM_RANKING_FIELDS = frozenset({
    "ranking_score", "score_features", "score_breakdown", "selection_rank",
    "selection_adjusted_score", "sequence_position", "sequence_energy", "_source_index",
    "resolved_route",
    "matched_interest_id",
    "program_explanation",
})

EVIDENCE_GRADES = {"A", "B", "C"}
STYLE_CONFIDENCE_LEVELS = {"high", "medium", "low"}
EVIDENCE_CLAIM_TYPES = {"track_identity", "style", "relation", "release"}

FEEDBACK_OUTCOMES = {"saved", "skipped", "replayed", "hidden"}

EVIDENCE_VERIFICATION_STATUSES = {
    "verified",
    "unverified",
    "stale",
    "contradictory",
    "inaccessible",
    "duplicated",
}

DEFAULT_RECALL_MIX = (
    ("artist_continuation", 0.30),
    ("musician_relation", 0.25),
    ("style_neighbor", 0.30),
    ("exploration", 0.15),
)

# 大歌单品味摘要模式（taste_summary/artist_summary）没有逐曲关系研究，
# 召回配额与候选校验在此模式下不含 musician_relation。
TASTE_MODES = ("taste_summary", "artist_summary")


class ContractError(ValueError):
    """Raised when a pipeline artifact does not satisfy its contract."""


def recall_mix_ratios(packet: dict[str, Any]) -> list[tuple[str, float]]:
    """Resolve the normalized candidate-type mix from policy or default."""

    raw = packet.get("recommendation_policy", {}).get("recall_mix")
    if not isinstance(raw, list):
        return list(DEFAULT_RECALL_MIX)
    result = [
        (str(item["candidate_type"]), float(item["target_ratio"]))
        for item in raw
        if isinstance(item, dict) and "candidate_type" in item and "target_ratio" in item
    ]
    total = sum(ratio for _, ratio in result)
    if not result or total <= 0:
        raise ContractError("recall_mix 无法生成候选类型配额")
    return [(candidate_type, ratio / total) for candidate_type, ratio in result]


def target_counts(total: int, packet: dict[str, Any]) -> dict[str, int]:
    """Allocate exact integer quotas with the largest-remainder method."""

    mix = recall_mix_ratios(packet)
    raw = [(candidate_type, total * ratio) for candidate_type, ratio in mix]
    counts = {candidate_type: int(value) for candidate_type, value in raw}
    remainder = total - sum(counts.values())
    ranked_remainders = sorted(raw, key=lambda item: (-(item[1] - int(item[1])), -item[1], item[0]))
    for candidate_type, _ in ranked_remainders[:remainder]:
        counts[candidate_type] += 1
    return counts


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any, label: str) -> datetime:
    text = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ContractError(f"{label} 必须是有效的 ISO-8601 时间") from exc


def parse_as_of_date(value: Any) -> date:
    text = _require_text(value, "as_of_date（缺失时请重新运行 analyze）")
    try:
        parsed = date.fromisoformat(text)
        if parsed.isoformat() != text:
            raise ValueError("non-canonical date")
        return parsed
    except ValueError as exc:
        raise ContractError("as_of_date 必须是有效的 YYYY-MM-DD 日期") from exc


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"找不到 JSON 文件：{path}") from exc
    except OSError as exc:
        raise ContractError(f"无法读取 JSON 文件：{path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"JSON 格式错误：{path}：第 {exc.lineno} 行") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ContractError(f"无法计算文件摘要：{path}：{exc}") from exc
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalized_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def normalized_name(value: Any) -> str:
    text = normalized_text(value).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text, flags=re.UNICODE)


def artist_key(value: Any) -> str:
    compact = normalized_name(value)
    return compact or "unknown-artist"


def track_key(title: Any, artist: Any) -> str:
    return f"{normalized_text(title).casefold()}\u001f{normalized_text(artist).casefold()}"


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} 必须是 JSON 对象")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} 不能为空")
    return value.strip()


def _require_int(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{label} 必须是大于等于 {minimum} 的整数")
    return value


def _require_score(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} 必须是 0 到 100 的数字")
    score = float(value)
    if not 0 <= score <= 100:
        raise ContractError(f"{label} 必须是 0 到 100 的数字")
    return score


_STYLE_REF_TYPO = re.compile(r"^style\s*[.:/／]\s*", re.IGNORECASE)


def canonical_style_ref(value: Any, known_style_refs: set[str]) -> str:
    """把模型常见的 ``style_ref`` 笔误规范化（点号/斜杠/空格/大小写）。

    关闭 reasoning 后模型偶发把 ``style:pop_punk`` 写成 ``style.pop_punk``；
    这类写法不代表风格本身无效，先规范再校验，避免整批研究因笔误失败。
    """

    text = normalized_text(value)
    if not text or text in known_style_refs:
        return text
    candidate = _STYLE_REF_TYPO.sub("style:", text).replace(" ", "")
    if candidate in known_style_refs:
        return candidate
    folded = candidate.casefold()
    for known in known_style_refs:
        if known.casefold() == folded:
            return known
    return text


def _validate_style_mix(value: Any, label: str, known_style_refs: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError(f"{label} 必须是数组")
    total = 0.0
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        entry = _require_dict(item, f"{label}[{index}]")
        style_ref = canonical_style_ref(entry.get("style_ref"), known_style_refs)
        _require_text(style_ref, f"{label}[{index}].style_ref")
        if style_ref not in known_style_refs:
            raise ContractError(f"{label}[{index}] 包含未知风格引用：{style_ref}")
        role = _require_text(entry.get("role"), f"{label}[{index}].role")
        if role not in {"primary", "secondary"}:
            raise ContractError(f"{label}[{index}].role 必须是 primary 或 secondary")
        weight = entry.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < float(weight) <= 1:
            raise ContractError(f"{label}[{index}].weight 必须大于 0 且小于等于 1")
        total += float(weight)
        result.append(entry)
    if result and abs(total - 1.0) > 0.001:
        raise ContractError(f"{label} 权重总和必须为 1，实际为 {total:.6f}")
    return result


def _validate_style_axes(value: Any, label: str, *, allow_unknown: bool = False) -> dict[str, float | None]:
    axes = _require_dict(value, label)
    missing = [axis for axis in STYLE_AXIS_IDS if axis not in axes]
    if missing:
        raise ContractError(f"{label} 缺少听感轴：{missing}")
    return {axis: None if allow_unknown and axes[axis] is None else _require_score(axes[axis], f"{label}.{axis}") for axis in STYLE_AXIS_IDS}


def _validate_weight_map(value: Any, label: str) -> None:
    if not isinstance(value, dict) or not value:
        raise ContractError(f"{label} 必须是非空对象")
    total = 0.0
    for key, raw_weight in value.items():
        _require_text(key, f"{label} 的键")
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
            raise ContractError(f"{label}.{key} 必须是 0 到 1 的数字")
        weight = float(raw_weight)
        if not 0 <= weight <= 1:
            raise ContractError(f"{label}.{key} 必须是 0 到 1 的数字")
        total += weight
    if abs(total - 1.0) > 0.001:
        raise ContractError(f"{label} 权重总和必须为 1，实际为 {total:.6f}")


def _validate_score_map(value: Any, label: str) -> None:
    if not isinstance(value, dict) or not value:
        raise ContractError(f"{label} 必须是非空对象")
    for key, raw_score in value.items():
        _require_text(key, f"{label} 的键")
        _require_score(raw_score, f"{label}.{key}")


def _validate_style_analysis(packet: dict[str, Any], source_count: int) -> None:
    style_analysis = _require_dict(packet.get("style_analysis"), "style_analysis")
    _require_text(style_analysis.get("taxonomy_version"), "style_analysis.taxonomy_version")
    for field in ("taxonomy_sha256", "profile_catalog_sha256"):
        _require_text(style_analysis.get(field), f"style_analysis.{field}")
    catalog_mode = _require_text(
        style_analysis.get("profile_catalog_mode"),
        "style_analysis.profile_catalog_mode",
    )
    if catalog_mode not in {"private", "explicit", "example_fallback", "agent_research", "taste_summary"}:
        raise ContractError("style_analysis.profile_catalog_mode 无效")
    coverage = _require_dict(
        style_analysis.get("profile_coverage"),
        "style_analysis.profile_coverage",
    )
    for field in ("required_artist_count", "classified_artist_count", "unclassified_artist_count"):
        _require_int(coverage.get(field), f"style_analysis.profile_coverage.{field}")
    required_artist_count = int(coverage["required_artist_count"])
    if int(coverage["classified_artist_count"]) + int(coverage["unclassified_artist_count"]) != required_artist_count:
        raise ContractError("style_analysis.profile_coverage 分类数与 required_artist_count 不一致")
    if not isinstance(coverage.get("degraded"), bool):
        raise ContractError("style_analysis.profile_coverage.degraded 必须是布尔值")
    known_refs = style_analysis.get("known_style_refs")
    if not isinstance(known_refs, list) or not known_refs or any(
        not isinstance(ref, str) or not ref for ref in known_refs
    ):
        raise ContractError("style_analysis.known_style_refs 必须是非空字符串数组")
    known_style_refs = set(known_refs)
    definitions = style_analysis.get("style_definitions")
    if not isinstance(definitions, list) or not definitions:
        raise ContractError("style_analysis.style_definitions 必须是非空数组")
    definition_refs: set[str] = set()
    for index, item in enumerate(definitions):
        definition = _require_dict(item, f"style_analysis.style_definitions[{index}]")
        ref = _require_text(definition.get("style_ref"), f"style_analysis.style_definitions[{index}].style_ref")
        if ref not in known_style_refs:
            raise ContractError(f"style_analysis.style_definitions[{index}] 包含未知风格引用")
        definition_refs.add(ref)
        _require_text(definition.get("style_id"), f"style_analysis.style_definitions[{index}].style_id")
        _require_text(definition.get("label"), f"style_analysis.style_definitions[{index}].label")
        _require_text(definition.get("definition"), f"style_analysis.style_definitions[{index}].definition")
        _require_text(definition.get("boundary"), f"style_analysis.style_definitions[{index}].boundary")
    if definition_refs != known_style_refs:
        raise ContractError("style_analysis.style_definitions 必须完整覆盖 known_style_refs")
    active_refs = style_analysis.get("active_style_refs")
    if not isinstance(active_refs, list) or any(
        not isinstance(ref, str) or ref not in known_style_refs for ref in active_refs
    ):
        raise ContractError("style_analysis.active_style_refs 必须是 known_style_refs 的字符串子集")
    _require_text(style_analysis.get("frequency_basis"), "style_analysis.frequency_basis")
    axis_definitions = style_analysis.get("axis_definitions")
    if not isinstance(axis_definitions, dict):
        raise ContractError("style_analysis.axis_definitions 必须是对象")
    for axis in STYLE_AXIS_IDS:
        _require_text(axis_definitions.get(axis), f"style_analysis.axis_definitions.{axis}")

    classified_count = _require_int(
        style_analysis.get("classified_track_count"),
        "style_analysis.classified_track_count",
    )
    unclassified_count = _require_int(
        style_analysis.get("unclassified_track_count"),
        "style_analysis.unclassified_track_count",
    )
    if classified_count + unclassified_count != source_count:
        raise ContractError(
            "style_analysis 分类歌曲数与 source_track_count 不一致："
            f"{classified_count}+{unclassified_count} != {source_count}"
        )

    profile_items = style_analysis.get("artist_profiles")
    if not isinstance(profile_items, list):
        raise ContractError("style_analysis.artist_profiles 必须是数组")
    profile_by_marker: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(profile_items):
        profile = _require_dict(item, f"style_analysis.artist_profiles[{index}]")
        artist = _require_text(profile.get("artist"), f"style_analysis.artist_profiles[{index}].artist")
        marker = normalized_name(artist)
        if marker in profile_by_marker:
            raise ContractError(f"style_analysis.artist_profiles 重复艺人：{artist}")
        profile_by_marker[marker] = profile
        _require_text(profile.get("entity_ref"), f"style_analysis.artist_profiles[{index}].entity_ref")
        status = _require_text(
            profile.get("classification_status"),
            f"style_analysis.artist_profiles[{index}].classification_status",
        )
        if status not in STYLE_CLASSIFICATION_STATUSES:
            raise ContractError(f"style_analysis.artist_profiles[{index}] 分类状态无效：{status}")
        confidence = _require_text(
            profile.get("confidence"),
            f"style_analysis.artist_profiles[{index}].confidence",
        )
        if confidence not in STYLE_CONFIDENCE_LEVELS:
            raise ContractError(
                f"style_analysis.artist_profiles[{index}].confidence 必须是 high、medium 或 low"
            )
        mix = _validate_style_mix(
            profile.get("style_mix"),
            f"style_analysis.artist_profiles[{index}].style_mix",
            known_style_refs,
        )
        if status == "classified" and not mix:
            raise ContractError(f"style_analysis.artist_profiles[{index}] 已分类但没有风格权重")
        _validate_style_axes(
            profile.get("style_axes"),
            f"style_analysis.artist_profiles[{index}].style_axes",
            allow_unknown=status == "unclassified",
        )
        _require_text(profile.get("summary"), f"style_analysis.artist_profiles[{index}].summary")
        boundaries = profile.get("boundaries")
        if not isinstance(boundaries, list) or any(not isinstance(item, str) or not item for item in boundaries):
            raise ContractError(f"style_analysis.artist_profiles[{index}].boundaries 必须是字符串数组")
        sources = profile.get("sources")
        if not isinstance(sources, list) or (
            status == "classified" and not sources
        ):
            raise ContractError(f"style_analysis.artist_profiles[{index}].sources 必须是非空数组")
        for source_index, source in enumerate(sources):
            _validate_http_url(source, f"style_analysis.artist_profiles[{index}].sources[{source_index}]")

    required_artists = {
        normalized_name(item.get("artist"))
        for field in ("primary_distribution", "credited_distribution")
        for item in packet[field]
        if isinstance(item, dict)
    }
    missing_profiles = sorted(marker for marker in required_artists if marker not in profile_by_marker)
    if missing_profiles:
        raise ContractError(f"style_analysis 缺少当前歌单艺人画像：{missing_profiles}")
    if int(coverage["required_artist_count"]) != len(profile_items):
        raise ContractError("style_analysis.profile_coverage.required_artist_count 与画像数不一致")
    actual_classified_artists = sum(
        1 for profile in profile_items if profile.get("classification_status") == "classified"
    )
    actual_unclassified_artists = len(profile_items) - actual_classified_artists
    if int(coverage["classified_artist_count"]) != actual_classified_artists:
        raise ContractError("style_analysis.profile_coverage.classified_artist_count 与画像不一致")
    if int(coverage["unclassified_artist_count"]) != actual_unclassified_artists:
        raise ContractError("style_analysis.profile_coverage.unclassified_artist_count 与画像不一致")
    if _require_int(style_analysis.get("artist_profile_count"), "style_analysis.artist_profile_count") != len(profile_items):
        raise ContractError("style_analysis.artist_profile_count 与画像数不一致")
    actual_core_count = sum(1 for profile in profile_items if profile.get("is_core_artist") is True)
    if _require_int(style_analysis.get("core_artist_profile_count"), "style_analysis.core_artist_profile_count") != actual_core_count:
        raise ContractError("style_analysis.core_artist_profile_count 与核心画像数不一致")

    assignments = packet.get("track_style_assignments")
    if not isinstance(assignments, list) or len(assignments) != source_count:
        raise ContractError("track_style_assignments 必须与 source_track_count 一致")
    favorite_keys = [str(key) for key in packet.get("favorite_track_keys", [])]
    assignment_keys: list[str] = []
    counted_classified = 0
    counted_unclassified = 0
    for index, item in enumerate(assignments):
        assignment = _require_dict(item, f"track_style_assignments[{index}]")
        key = _require_text(assignment.get("track_key"), f"track_style_assignments[{index}].track_key")
        assignment_keys.append(key)
        _require_text(assignment.get("artist"), f"track_style_assignments[{index}].artist")
        _require_text(assignment.get("title"), f"track_style_assignments[{index}].title")
        status = _require_text(
            assignment.get("classification_status"),
            f"track_style_assignments[{index}].classification_status",
        )
        if status not in STYLE_CLASSIFICATION_STATUSES:
            raise ContractError(f"track_style_assignments[{index}] 分类状态无效：{status}")
        confidence = _require_text(
            assignment.get("confidence"),
            f"track_style_assignments[{index}].confidence",
        )
        if confidence not in STYLE_CONFIDENCE_LEVELS:
            raise ContractError(
                f"track_style_assignments[{index}].confidence 必须是 high、medium 或 low"
            )
        refs = assignment.get("style_refs")
        if not isinstance(refs, list) or any(ref not in known_style_refs for ref in refs):
            raise ContractError(f"track_style_assignments[{index}].style_refs 包含未知风格引用")
        primary_ref = assignment.get("primary_style_ref", "")
        if status == "classified":
            counted_classified += 1
            if not refs or not isinstance(primary_ref, str) or primary_ref not in known_style_refs:
                raise ContractError(f"track_style_assignments[{index}] 已分类但缺少 primary_style_ref")
        else:
            counted_unclassified += 1
            if refs or primary_ref:
                raise ContractError(f"track_style_assignments[{index}] 未分类时不能携带风格引用")
        _validate_style_axes(
            assignment.get("style_axes"),
            f"track_style_assignments[{index}].style_axes",
            allow_unknown=status == "unclassified",
        )
        _require_text(assignment.get("rationale"), f"track_style_assignments[{index}].rationale")
    if assignment_keys != favorite_keys:
        raise ContractError("track_style_assignments 必须按顺序对应 favorite_track_keys")
    if counted_classified != classified_count or counted_unclassified != unclassified_count:
        raise ContractError("track_style_assignments 分类计数与 style_analysis 不一致")
    if catalog_mode == "agent_research":
        from analysis_contracts import validate_research_evidence
        research = _require_dict(packet.get("analysis_research"), "analysis_research")
        if (research.get("track_count") != source_count or research.get("publication_status") != "draft"
                or research.get("evidence_verification") != "pending_independent_verification"):
            raise ContractError("Agent 画像研究数量或待核验状态无效")
        for key in ("bundle_sha256", "snapshot_sha256"):
            _require_text(research.get(key), f"analysis_research.{key}")
        _require_int(research.get("batch_count"), "analysis_research.batch_count", 1)
        for assignment in assignments:
            if assignment["classification_status"] == "classified":
                if assignment.get("applied_scope") not in ("agent_artist", "agent_release", "agent_track"):
                    raise ContractError("Agent 画像缺少明确的研究范围")
                evidence = validate_research_evidence(assignment.get("evidence_items"), "style")
                if any(item.get("verification_result") != "unverified" for item in evidence):
                    raise ContractError("Agent 画像证据必须保持待独立核验")
                if set(assignment.get("sources", [])) != {item["url"] for item in evidence}:
                    raise ContractError("Agent 画像来源与研究证据不一致")
            elif assignment.get("applied_scope") != "agent_unknown" or assignment.get("evidence_items") != []:
                raise ContractError("Agent 未知画像不得附带已分类范围或证据")

    for field in ("style_distribution", "dominant_style_mix"):
        items = style_analysis.get(field)
        if not isinstance(items, list):
            raise ContractError(f"style_analysis.{field} 必须是数组")
        total = 0
        weight_total = 0.0
        for index, item in enumerate(items):
            entry = _require_dict(item, f"style_analysis.{field}[{index}]")
            ref = _require_text(entry.get("style_ref"), f"style_analysis.{field}[{index}].style_ref")
            if ref not in known_style_refs:
                raise ContractError(f"style_analysis.{field}[{index}] 包含未知风格引用")
            count = _require_int(entry.get("count"), f"style_analysis.{field}[{index}].count")
            total += count
            if field == "dominant_style_mix":
                weight = entry.get("weight")
                if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
                    raise ContractError(f"style_analysis.dominant_style_mix[{index}].weight 无效")
                weight_total += float(weight)
        if total != classified_count:
            raise ContractError(f"style_analysis.{field} 总数与 classified_track_count 不一致")
        if field == "dominant_style_mix" and items and abs(weight_total - 1.0) > 0.001:
            raise ContractError(
                "style_analysis.dominant_style_mix 权重总和必须为 1，"
                f"实际为 {weight_total:.6f}"
            )
    overlap_items = style_analysis.get("overlap_style_distribution")
    if overlap_items is not None:
        if not isinstance(overlap_items, list):
            raise ContractError("style_analysis.overlap_style_distribution 必须是数组")
        seen_overlap_refs: set[str] = set()
        for index, item in enumerate(overlap_items):
            entry = _require_dict(item, f"style_analysis.overlap_style_distribution[{index}]")
            ref = _require_text(
                entry.get("style_ref"),
                f"style_analysis.overlap_style_distribution[{index}].style_ref",
            )
            if ref not in known_style_refs:
                raise ContractError("style_analysis.overlap_style_distribution 包含未知风格引用")
            if ref in seen_overlap_refs:
                raise ContractError("style_analysis.overlap_style_distribution 不得重复风格引用")
            seen_overlap_refs.add(ref)
            count = _require_int(
                entry.get("count"),
                f"style_analysis.overlap_style_distribution[{index}].count",
                1,
            )
            if count > classified_count:
                raise ContractError(
                    "style_analysis.overlap_style_distribution 单个风格命中数不能超过已分类歌曲数"
                )
            for share_field in ("share", "classified_share"):
                share = entry.get(share_field)
                if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 <= float(share) <= 1:
                    raise ContractError(
                        f"style_analysis.overlap_style_distribution[{index}].{share_field} 无效"
                    )
            expected_share = round(count / source_count, 6) if source_count else 0.0
            expected_classified_share = round(count / classified_count, 6) if classified_count else 0.0
            if abs(float(entry["share"]) - expected_share) > 0.000001:
                raise ContractError("style_analysis.overlap_style_distribution.share 与计数不一致")
            if abs(float(entry["classified_share"]) - expected_classified_share) > 0.000001:
                raise ContractError("style_analysis.overlap_style_distribution.classified_share 与计数不一致")
            if entry.get("overlap") is not True:
                raise ContractError(
                    f"style_analysis.overlap_style_distribution[{index}].overlap 必须为 true"
                )
    _validate_style_axes(style_analysis.get("style_axes"), "style_analysis.style_axes", allow_unknown=classified_count == 0)
    if "interest_profiles" in style_analysis:
        from preference_model import MODEL_CONFIG, build_interest_profiles
        if style_analysis.get("interest_model") != MODEL_CONFIG or style_analysis["interest_profiles"] != build_interest_profiles(assignments):
            raise ContractError("兴趣分组与本次逐曲画像的确定性计算不一致；请重新 analyze")


def _validate_http_url(value: Any, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        if allow_empty:
            return ""
        raise ContractError(f"{label} 必须是 HTTP(S) URL")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ContractError(f"{label} 必须是 HTTP(S) URL")
    return value.strip()


def _track_from_snapshot(item: Any, index: int) -> dict[str, Any]:
    track = _require_dict(item, f"tracks[{index}]")
    title = _require_text(track.get("title"), f"tracks[{index}].title")
    artist = _require_text(track.get("artist"), f"tracks[{index}].artist")
    artists = track.get("artists", [artist])
    if not isinstance(artists, list) or not artists:
        raise ContractError(f"tracks[{index}].artists 必须是非空数组")
    for artist_index, credited in enumerate(artists):
        _require_text(credited, f"tracks[{index}].artists[{artist_index}]")
    links = track.get("links", {})
    if links is not None:
        if not isinstance(links, dict):
            raise ContractError(f"tracks[{index}].links 必须是对象")
        for platform, link in links.items():
            _validate_http_url(link, f"tracks[{index}].links[{platform}]")
    return track


def validate_playlist_snapshot(value: Any, require_complete: bool = True) -> dict[str, Any]:
    snapshot = _require_dict(value, "PlaylistSnapshot")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"PlaylistSnapshot.schema_version 必须是 {SCHEMA_VERSION}")
    _require_text(snapshot.get("snapshot_id"), "snapshot_id")
    _require_text(snapshot.get("platform"), "platform")
    _require_text(snapshot.get("captured_at"), "captured_at")
    status = _require_text(snapshot.get("reader_status"), "reader_status")
    if require_complete and status != "complete":
        raise ContractError(f"Step 1 快照不是 complete：reader_status={status}")
    declared = _require_int(snapshot.get("declared_track_count"), "declared_track_count")
    actual = _require_int(snapshot.get("track_count"), "track_count")
    tracks = snapshot.get("tracks")
    if not isinstance(tracks, list):
        raise ContractError("tracks 必须是数组")
    if declared != actual:
        raise ContractError(
            "Step 1 数量不一致："
            f"declared_track_count={declared}, track_count={actual}"
        )
    if len(tracks) != actual:
        raise ContractError(
            "Step 1 歌曲数组数量不一致："
            f"len(tracks)={len(tracks)}, track_count={actual}"
        )
    for index, item in enumerate(tracks):
        _track_from_snapshot(item, index)
    input_hash = snapshot.get("input_sha256")
    if input_hash is not None:
        _require_text(input_hash, "input_sha256")
    return snapshot


def _validate_entities(packet: dict[str, Any]) -> None:
    entities = packet.get("entities")
    if not isinstance(entities, list):
        raise ContractError("entities 必须是数组")
    known_refs = set(packet.get("analysis_ref_ids", []))
    seen_refs: set[str] = set()
    primary_counts = {
        str(item.get("entity_ref")): int(item.get("count") or 0)
        for item in packet.get("primary_distribution", [])
        if isinstance(item, dict)
    }
    credited_counts = {
        str(item.get("entity_ref")): int(item.get("count") or 0)
        for item in packet.get("credited_distribution", [])
        if isinstance(item, dict)
    }
    for index, raw_entity in enumerate(entities):
        entity = _require_dict(raw_entity, f"entities[{index}]")
        entity_ref = _require_text(entity.get("entity_ref"), f"entities[{index}].entity_ref")
        if entity_ref in seen_refs:
            raise ContractError(f"entities 包含重复 entity_ref：{entity_ref}")
        seen_refs.add(entity_ref)
        _require_text(entity.get("name"), f"entities[{index}].name")
        primary_count = _require_int(entity.get("primary_track_count"), f"entities[{index}].primary_track_count")
        credited_count = _require_int(entity.get("credited_track_count"), f"entities[{index}].credited_track_count")
        if primary_count != primary_counts.get(entity_ref, 0) or credited_count != credited_counts.get(entity_ref, 0):
            raise ContractError(f"entities[{index}] 的歌曲计数与分布不一致")
        if not isinstance(entity.get("is_preferred"), bool):
            raise ContractError(f"entities[{index}].is_preferred 必须是布尔值")
        relation_status = _require_text(entity.get("relation_status"), f"entities[{index}].relation_status")
        if relation_status not in {"confirmed", "researched", "unmapped"}:
            raise ContractError(f"entities[{index}].relation_status 无效")
        if packet.get("style_analysis", {}).get("profile_catalog_mode") == "agent_research":
            if (entity.get("research_origin") != "agent" or relation_status == "confirmed"
                    or entity.get("verification_scope") != "pending_independent_verification"):
                raise ContractError("Agent 关系必须保留研究来源且不得冒充目录核验")
        entity_refs = entity.get("analysis_refs")
        if not isinstance(entity_refs, list) or entity_ref not in entity_refs:
            raise ContractError(f"entities[{index}].analysis_refs 必须包含自身 entity_ref")
        unknown = [ref for ref in entity_refs if not isinstance(ref, str) or ref not in known_refs]
        if unknown:
            raise ContractError(f"entities[{index}] 包含未知 analysis_refs：{unknown}")
        relation_count = 0
        for field in ("lead_vocalists", "related_projects"):
            facts = entity.get(field)
            if not isinstance(facts, list):
                raise ContractError(f"entities[{index}].{field} 必须是数组")
            relation_count += len(facts)
            for fact_index, raw_fact in enumerate(facts):
                fact = _require_dict(raw_fact, f"entities[{index}].{field}[{fact_index}]")
                _require_text(fact.get("name"), f"entities[{index}].{field}[{fact_index}].name")
                sources = fact.get("sources", [])
                if not isinstance(sources, list):
                    raise ContractError(f"entities[{index}].{field}[{fact_index}].sources 必须是数组")
                for source_index, source in enumerate(sources):
                    _validate_http_url(source, f"entities[{index}].{field}[{fact_index}].sources[{source_index}]")
                if relation_status == "researched":
                    from analysis_contracts import validate_research_evidence
                    evidence = validate_research_evidence(fact.get("evidence_items"), "relation")
                    if any(item.get("verification_result") != "unverified" for item in evidence):
                        raise ContractError("Agent 关系证据必须保持待独立核验")
                    if set(sources) != {item["url"] for item in evidence}:
                        raise ContractError("Agent 关系来源与研究证据不一致")
        if relation_status in {"confirmed", "researched"} and relation_count == 0:
            raise ContractError(f"entities[{index}] 标记 confirmed 但没有关系事实")
        if relation_status == "researched" and entity.get("verification_scope") != "pending_independent_verification":
            raise ContractError("Agent 关系不得冒充已独立核验")
        if relation_status == "unmapped" and relation_count:
            raise ContractError(f"entities[{index}] 有关系事实却标记为 unmapped")
        sources = entity.get("sources")
        if not isinstance(sources, list):
            raise ContractError(f"entities[{index}].sources 必须是数组")
        for source_index, source in enumerate(sources):
            _validate_http_url(source, f"entities[{index}].sources[{source_index}]")
    required_entity_refs = set(primary_counts) | set(credited_counts)
    if not required_entity_refs.issubset(seen_refs):
        raise ContractError(f"entities 缺少分布中的艺人：{sorted(required_entity_refs - seen_refs)}")


def validate_analysis_packet(value: Any) -> dict[str, Any]:
    packet = _require_dict(value, "MusicianAnalysisPacket")
    if packet.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"MusicianAnalysisPacket.schema_version 必须是 {SCHEMA_VERSION}")
    if packet.get("packet_type") != "musician_analysis":
        raise ContractError("packet_type 必须是 musician_analysis")
    _require_text(packet.get("analysis_id"), "analysis_id")
    parse_as_of_date(packet.get("as_of_date"))
    _require_text(packet.get("source_snapshot_id"), "source_snapshot_id")
    source_count = _require_int(packet.get("source_track_count"), "source_track_count")
    favorite_tracks = packet.get("favorite_tracks")
    if not isinstance(favorite_tracks, list):
        raise ContractError("favorite_tracks 必须是数组")
    if len(favorite_tracks) != source_count:
        raise ContractError(
            "分析包与 Step 1 数量不一致："
            f"len(favorite_tracks)={len(favorite_tracks)}, source_track_count={source_count}"
        )
    favorite_keys = packet.get("favorite_track_keys")
    if not isinstance(favorite_keys, list) or len(favorite_keys) != source_count:
        raise ContractError("favorite_track_keys 必须与 source_track_count 一致")
    for index, item in enumerate(favorite_tracks):
        track = _require_dict(item, f"favorite_tracks[{index}]")
        title = _require_text(track.get("title"), f"favorite_tracks[{index}].title")
        artist = _require_text(track.get("artist"), f"favorite_tracks[{index}].artist")
        if track.get("track_key") != track_key(title, artist):
            raise ContractError(f"favorite_tracks[{index}].track_key 与标题/艺人不一致")
    for field in ("primary_distribution", "credited_distribution", "entities", "preferred_artists"):
        if not isinstance(packet.get(field), list):
            raise ContractError(f"{field} 必须是数组")
    for field in ("primary_distribution", "credited_distribution"):
        total = 0
        for index, item in enumerate(packet[field]):
            distribution = _require_dict(item, f"{field}[{index}]")
            _require_text(distribution.get("artist"), f"{field}[{index}].artist")
            _require_text(distribution.get("entity_ref"), f"{field}[{index}].entity_ref")
            total += _require_int(distribution.get("count"), f"{field}[{index}].count")
        if field == "primary_distribution" and total != source_count:
            raise ContractError(
                "primary_distribution 总数与 source_track_count 不一致："
                f"{total} != {source_count}"
            )
    validate_recommendation_policy(packet.get("recommendation_policy"),
                                   analysis_mode=packet.get("analysis_mode"))
    ref_ids = packet.get("analysis_ref_ids")
    if not isinstance(ref_ids, list) or any(not isinstance(ref, str) or not ref for ref in ref_ids):
        raise ContractError("analysis_ref_ids 必须是非空字符串数组")
    if len(ref_ids) != len(set(ref_ids)):
        raise ContractError("analysis_ref_ids 不得重复")
    _validate_entities(packet)
    _validate_style_analysis(packet, source_count)
    return packet


def require_analysis_coverage(packet: dict[str, Any]) -> None:
    """Missing preference evidence is not a quiet/low-energy taste."""
    total = int(packet["source_track_count"])
    classified = sum(item.get("classification_status") == "classified" for item in packet["track_style_assignments"])
    minimum = packet["recommendation_policy"].get("analysis_quality", {}).get("min_classified_share", 0.5)
    if not classified or not total or classified / total < minimum:
        raise ContractError(f"画像覆盖不足：{classified}/{total} 首已分类，要求至少 {minimum:.0%}；请补齐画像并重新 analyze，未调用 Agent")


def validate_recommendation_policy(value: Any, *, analysis_mode: str | None = None) -> dict[str, Any]:
    policy = _require_dict(value, "recommendation_policy")
    quality = policy.get("analysis_quality", {"min_classified_share": 0.5})
    if not isinstance(quality, dict) or set(quality) != {"min_classified_share"}:
        raise ContractError("analysis_quality 仅允许 min_classified_share")
    floor = quality["min_classified_share"]
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0 < floor <= 1:
        raise ContractError("min_classified_share 必须大于 0 且不超过 1")
    minimum = _require_int(policy.get("min_recommendations"), "min_recommendations", 1)
    maximum = _require_int(policy.get("max_recommendations"), "max_recommendations", minimum)
    if maximum < minimum:
        raise ContractError("max_recommendations 不能小于 min_recommendations")
    target = _require_int(policy.get("target_recommendations"), "target_recommendations", minimum)
    if not minimum <= target <= maximum:
        raise ContractError("target_recommendations 必须位于推荐数量范围内")
    _require_int(policy.get("max_per_artist"), "max_per_artist", 1)
    _require_int(policy.get("max_per_project"), "max_per_project", 1)
    min_projects = _require_int(policy.get("min_projects"), "min_projects", 1)
    if min_projects > target:
        raise ContractError("min_projects 不能大于 target_recommendations")
    candidate_pool_min = policy.get("candidate_pool_min")
    if candidate_pool_min is not None:
        pool_minimum = _require_int(candidate_pool_min, "candidate_pool_min", target)
        if pool_minimum < target:
            raise ContractError("candidate_pool_min 不能小于 target_recommendations")
    recall_mix = policy.get("recall_mix")
    if recall_mix is not None:
        if not isinstance(recall_mix, list) or not recall_mix:
            raise ContractError("recommendation_policy.recall_mix 必须是非空数组")
        ratios: dict[str, float] = {}
        for index, item in enumerate(recall_mix):
            entry = _require_dict(item, f"recommendation_policy.recall_mix[{index}]")
            if set(entry) != {"candidate_type", "target_ratio"}:
                raise ContractError("recommendation_policy.recall_mix 仅允许 candidate_type 与 target_ratio")
            candidate_type = _require_text(
                entry.get("candidate_type"),
                f"recommendation_policy.recall_mix[{index}].candidate_type",
            )
            if candidate_type not in CANDIDATE_TYPES:
                raise ContractError(f"recommendation_policy.recall_mix 候选类型无效：{candidate_type}")
            if candidate_type in ratios:
                raise ContractError("recommendation_policy.recall_mix 不得重复候选类型")
            raw_ratio = entry.get("target_ratio")
            if isinstance(raw_ratio, bool) or not isinstance(raw_ratio, (int, float)):
                raise ContractError("recommendation_policy.recall_mix.target_ratio 无效")
            ratio = float(raw_ratio)
            if not 0 <= ratio <= 1:
                raise ContractError("recommendation_policy.recall_mix.target_ratio 必须为 0 到 1")
            ratios[candidate_type] = ratio
        if abs(sum(ratios.values()) - 1.0) > 0.001:
            raise ContractError("recommendation_policy.recall_mix 权重总和必须为 1")
        required_candidate_types = set(CANDIDATE_TYPES)
        if analysis_mode in TASTE_MODES:
            # 品味/歌手摘要没有关系研究：召回配额只覆盖有依据的候选类型。
            required_candidate_types -= {"musician_relation"}
        if set(ratios) != required_candidate_types:
            raise ContractError("recommendation_policy.recall_mix 必须完整覆盖当前模式的召回类型")
    ranking_weights = policy.get("ranking_weights")
    if ranking_weights is not None:
        _validate_weight_map(ranking_weights, "recommendation_policy.ranking_weights")
        if set(ranking_weights) != set(RANKING_FEATURE_KEYS):
            raise ContractError("recommendation_policy.ranking_weights 必须完整且仅包含七项评分维度")
    diversity = _require_dict(policy.get("diversity_policy"), "recommendation_policy.diversity_policy")
    _require_score(diversity.get("new_interest_bonus", 4.0), "diversity_policy.new_interest_bonus")
    if _require_int(diversity.get("min_interest_groups", 1), "diversity_policy.min_interest_groups", 1) > target:
        raise ContractError("min_interest_groups 不能超过推荐数量")
    for field in ("mmr_penalty", "candidate_type_bonus", "new_project_bonus"):
        _require_score(diversity.get(field), f"recommendation_policy.diversity_policy.{field}")
    similarity_weights = diversity.get("similarity_weights")
    _validate_weight_map(similarity_weights, "recommendation_policy.diversity_policy.similarity_weights")
    if set(similarity_weights) != {"style", "axis", "artist", "project"}:
        raise ContractError("diversity_policy.similarity_weights 必须包含 style、axis、artist、project")
    sequence_policy = _require_dict(policy.get("sequence_policy"), "recommendation_policy.sequence_policy")
    if sequence_policy.get("mode") != "energy_arc":
        raise ContractError("recommendation_policy.sequence_policy.mode 必须是 energy_arc")
    _validate_weight_map(
        {field: sequence_policy.get(field) for field in ("transition_weight", "arc_weight", "ranking_weight")},
        "recommendation_policy.sequence_policy.weights",
    )
    for field in ("prefer_adjacent_transitions", "allow_familiar_anchor"):
        if not isinstance(sequence_policy.get(field), bool):
            raise ContractError(f"recommendation_policy.sequence_policy.{field} 必须是布尔值")
    for field in ("exclude_current_favorites", "cross_platform_links_allowed"):
        if not isinstance(policy.get(field), bool):
            raise ContractError(f"recommendation_policy.{field} 必须是布尔值")
    display_limits = policy.get("display_limits")
    if display_limits is not None:
        display_limits = _require_dict(display_limits, "recommendation_policy.display_limits")
        for field in ("artists", "styles"):
            _require_int(display_limits.get(field), f"recommendation_policy.display_limits.{field}", 1)
    return policy


def _source_is_forbidden_personalization(value: str) -> bool:
    lowered = value.casefold()
    return (
        "music.apple.com" in lowered
        or ("apple music" in lowered and "personal" in lowered)
        or ("netease" in lowered and "personal" in lowered)
        or "网易云个性化" in lowered
    )


def _validate_optional_ranking_fields(value: dict[str, Any], label: str) -> None:
    candidate_type = value.get("candidate_type")
    if candidate_type is not None:
        _require_text(candidate_type, f"{label}.candidate_type")
    ranking_score = value.get("ranking_score")
    if ranking_score is not None:
        _require_score(ranking_score, f"{label}.ranking_score")
    score_breakdown = value.get("score_breakdown")
    if score_breakdown is not None:
        _validate_score_map(score_breakdown, f"{label}.score_breakdown")
        if set(score_breakdown) != set(RANKING_FEATURE_KEYS):
            raise ContractError(f"{label}.score_breakdown 必须完整包含七项评分维度")
    score_features = value.get("score_features")
    if score_features is not None:
        _validate_score_map(score_features, f"{label}.score_features")
        if set(score_features) != set(RANKING_FEATURE_KEYS):
            raise ContractError(f"{label}.score_features 必须完整包含七项评分维度")


def _validate_explanation(value: Any, label: str) -> None:
    explanation = _require_dict(value, label)
    for field in (
        "preference_basis",
        "artist_relation",
        "music_fit",
        "style_fit",
        "novelty",
        "text",
    ):
        text = _require_text(explanation.get(field), f"{label}.{field}")
        if field == "text" and len(text) < 24:
            raise ContractError(f"{label}.text 太短，无法构成逐首说明")


def _validate_candidate_evidence(candidate: dict[str, Any], label: str) -> None:
    grade = _require_text(candidate.get("evidence_grade"), f"{label}.evidence_grade").upper()
    if grade not in EVIDENCE_GRADES:
        raise ContractError(f"{label}.evidence_grade 必须是 A、B 或 C")
    evidence_items = candidate.get("evidence_items")
    if not isinstance(evidence_items, list) or not evidence_items:
        raise ContractError(f"{label}.evidence_items 必须是非空数组")
    claim_types: set[str] = set()
    evidence_urls: set[str] = set()
    for index, raw_item in enumerate(evidence_items):
        item = _require_dict(raw_item, f"{label}.evidence_items[{index}]")
        claim_type = _require_text(
            item.get("claim_type"),
            f"{label}.evidence_items[{index}].claim_type",
        )
        if claim_type not in EVIDENCE_CLAIM_TYPES:
            raise ContractError(f"{label}.evidence_items[{index}].claim_type 无效")
        _require_text(item.get("claim"), f"{label}.evidence_items[{index}].claim")
        url = _validate_http_url(item.get("url"), f"{label}.evidence_items[{index}].url")
        if _source_is_forbidden_personalization(url):
            raise ContractError("个性化音乐页面不能作为候选证据来源")
        retrieved_at = item.get("retrieved_at")
        if retrieved_at is not None:
            parse_timestamp(retrieved_at, f"{label}.evidence_items[{index}].retrieved_at")
        source_identifier = item.get("source_identifier")
        if source_identifier is not None:
            _require_text(
                source_identifier,
                f"{label}.evidence_items[{index}].source_identifier",
            )
        verification_result = item.get("verification_result")
        if verification_result is not None:
            result_text = _require_text(
                verification_result,
                f"{label}.evidence_items[{index}].verification_result",
            )
            if result_text not in EVIDENCE_VERIFICATION_STATUSES:
                raise ContractError(
                    f"{label}.evidence_items[{index}].verification_result 无效"
                )
        claim_types.add(claim_type)
        evidence_urls.add(url)
    missing_claims = {"track_identity", "style"} - claim_types
    if missing_claims:
        raise ContractError(f"{label}.evidence_items 缺少必要证据类型：{sorted(missing_claims)}")
    if candidate.get("candidate_type") == "musician_relation" and "relation" not in claim_types:
        raise ContractError(f"{label} 音乐人关系候选必须提供 relation 证据")
    sources = candidate.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ContractError(f"{label}.sources 不能为空")
    source_urls: set[str] = set()
    for source_index, source in enumerate(sources):
        source_url = _validate_http_url(source, f"{label}.sources[{source_index}]")
        if _source_is_forbidden_personalization(source_url):
            raise ContractError("个性化音乐页面不能作为候选发现、排序或推荐说明的来源")
        source_urls.add(source_url)
    if not evidence_urls.issubset(source_urls):
        raise ContractError(f"{label}.evidence_items 的 URL 必须同时列入 sources")


def _validate_candidate_pool(
    value: Any,
    *,
    known_refs: set[str],
    known_style_refs: set[str],
    label: str = "candidate_pool",
    require_all_types: bool = True,
    required_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError(f"{label} 必须是数组")
    seen_keys: set[str] = set()
    seen_canonical_ids: set[str] = set()
    type_counts: Counter[str] = Counter()
    for index, item in enumerate(value):
        candidate = _require_dict(item, f"{label}[{index}]")
        title = _require_text(candidate.get("title"), f"{label}[{index}].title")
        artist = _require_text(candidate.get("artist"), f"{label}[{index}].artist")
        _require_text(candidate.get("project"), f"{label}[{index}].project")
        candidate_key = track_key(title, artist)
        if candidate_key in seen_keys:
            raise ContractError(f"{label} 包含重复歌曲：{title} - {artist}")
        seen_keys.add(candidate_key)
        canonical_id = _require_text(
            candidate.get("canonical_track_id"),
            f"{label}[{index}].canonical_track_id",
        ).casefold()
        if canonical_id in seen_canonical_ids:
            raise ContractError(f"{label} 包含重复 canonical_track_id：{canonical_id}")
        seen_canonical_ids.add(canonical_id)
        candidate_type = _require_text(
            candidate.get("candidate_type"),
            f"{label}[{index}].candidate_type",
        )
        if candidate_type not in CANDIDATE_TYPES:
            raise ContractError(f"{label}[{index}].candidate_type 不受支持：{candidate_type}")
        type_counts[candidate_type] += 1
        refs = candidate.get("analysis_refs")
        if not isinstance(refs, list) or not refs:
            raise ContractError(f"{label}[{index}].analysis_refs 不能为空")
        unknown_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in known_refs]
        if unknown_refs:
            raise ContractError(f"{label}[{index}] 包含未知 analysis_refs：{unknown_refs}")
        style_mix = _validate_style_mix(
            candidate.get("style_mix"),
            f"{label}[{index}].style_mix",
            known_style_refs,
        )
        if not style_mix:
            raise ContractError(f"{label}[{index}].style_mix 不能为空")
        style_refs = candidate.get("style_refs")
        if not isinstance(style_refs, list) or not style_refs:
            raise ContractError(f"{label}[{index}].style_refs 不能为空")
        unknown_style_refs = [
            ref for ref in style_refs if not isinstance(ref, str) or ref not in known_style_refs
        ]
        if unknown_style_refs:
            raise ContractError(f"{label}[{index}] 包含未知 style_refs：{unknown_style_refs}")
        if list(dict.fromkeys(style_refs)) != [entry["style_ref"] for entry in style_mix]:
            raise ContractError(f"{label}[{index}].style_refs 必须与 style_mix 顺序一致")
        _validate_style_axes(candidate.get("style_axes"), f"{label}[{index}].style_axes")
        style_confidence = _require_text(
            candidate.get("style_confidence"),
            f"{label}[{index}].style_confidence",
        )
        if style_confidence not in STYLE_CONFIDENCE_LEVELS:
            raise ContractError(f"{label}[{index}].style_confidence 必须是 high、medium 或 low")
        relation_path = candidate.get("relation_path")
        if not isinstance(relation_path, list) or len(relation_path) < 3:
            raise ContractError(f"{label}[{index}].relation_path 至少需要三段")
        for path_index, segment in enumerate(relation_path):
            _require_text(segment, f"{label}[{index}].relation_path[{path_index}]")
        _validate_candidate_evidence(candidate, f"{label}[{index}]")
        discovery_source = _require_text(
            candidate.get("discovery_source"),
            f"{label}[{index}].discovery_source",
        )
        if _source_is_forbidden_personalization(discovery_source):
            raise ContractError("个性化音乐页面不能作为候选来源")
        links = candidate.get("platform_links")
        if not isinstance(links, dict) or not links:
            raise ContractError(f"{label}[{index}].platform_links 必须是非空对象")
        for platform, link in links.items():
            _require_text(platform, f"{label}[{index}].platform_links 的键")
            _validate_http_url(link, f"{label}[{index}].platform_links[{platform}]")
        release_date = candidate.get("release_date")
        if release_date is not None and (
            not isinstance(release_date, str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date.strip()) is None
        ):
            raise ContractError(f"{label}[{index}].release_date 必须是 YYYY-MM-DD")
        if "explanation" in candidate:
            _validate_explanation(candidate["explanation"], f"{label}[{index}].explanation")
        if release_date is not None:
            try:
                date.fromisoformat(release_date)
            except ValueError as exc:
                raise ContractError(f"{label}[{index}].release_date 必须是有效日期") from exc
        forbidden_scores = PROGRAM_RANKING_FIELDS & set(candidate)
        if forbidden_scores:
            raise ContractError(f"{label}[{index}] 不得提交程序评分字段：{sorted(forbidden_scores)}")
    if require_all_types and value:
        # 召回覆盖要求来自策略配额（taste/artist 模式可能不含 musician_relation）。
        required = required_types or set(CANDIDATE_TYPES)
        missing = sorted(required - set(type_counts))
        if missing:
            raise ContractError(f"{label} 必须覆盖全部召回类型，缺少：{missing}")
    return value


def validate_recommendation_bundle(value: Any, packet: dict[str, Any]) -> dict[str, Any]:
    analysis = validate_analysis_packet(packet)
    bundle = _require_dict(value, "RecommendationBundle")
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"RecommendationBundle.schema_version 必须是 {SCHEMA_VERSION}")
    if bundle.get("bundle_type") != "recommendation_bundle":
        raise ContractError("bundle_type 必须是 recommendation_bundle")
    if bundle.get("analysis_id") != analysis.get("analysis_id"):
        raise ContractError("RecommendationBundle 必须引用本次 Step 2 的 analysis_id")
    _require_text(bundle.get("generated_at"), "generated_at")
    status = _require_text(bundle.get("status"), "status")
    if status not in {"ready", "insufficient_evidence"}:
        raise ContractError("status 必须是 ready 或 insufficient_evidence")
    stage = _require_text(bundle.get("bundle_stage"), "bundle_stage")
    if stage not in {"candidate_pool", "ranked", "final"}:
        raise ContractError("bundle_stage 必须是 candidate_pool、ranked 或 final")
    recommendations = bundle.get("recommendations", [])
    if not isinstance(recommendations, list):
        raise ContractError("recommendations 必须是数组")
    policy = analysis["recommendation_policy"]
    target = int(policy["target_recommendations"])
    if status == "insufficient_evidence":
        if stage != "final":
            raise ContractError("insufficient_evidence 状态的 bundle_stage 必须是 final")
        if recommendations:
            raise ContractError("insufficient_evidence 状态不能携带推荐歌曲")
        _require_text(bundle.get("message"), "message")
        if bundle.get("candidate_pool") not in (None, []):
            raise ContractError("insufficient_evidence 状态不能携带候选池")
        return bundle
    if stage == "final":
        raise ContractError("ready 状态不能使用 final 阶段")

    known_refs = set(analysis["analysis_ref_ids"])
    known_style_refs = set(analysis["style_analysis"]["known_style_refs"])
    candidate_pool = bundle.get("candidate_pool")
    candidate_pool = _validate_candidate_pool(
        candidate_pool,
        known_refs=known_refs,
        known_style_refs=known_style_refs,
        required_types={candidate_type for candidate_type, _ in recall_mix_ratios(analysis)},
    )
    from candidate_routes import resolve_candidate_route
    for candidate in candidate_pool:
        resolved_type = resolve_candidate_route(candidate, analysis)["candidate_type"]
        if candidate["candidate_type"] != resolved_type:
            raise ContractError(f"候选 {candidate['canonical_track_id']} 的召回类型与当前画像/关系目录不一致：应为 {resolved_type}")
    minimum_pool = int(policy["candidate_pool_min"])
    if len(candidate_pool) < minimum_pool:
        raise ContractError(f"candidate_pool 至少需要 {minimum_pool} 首候选，实际为 {len(candidate_pool)} 首")
    if stage == "candidate_pool":
        if "publication_status" in bundle:
            raise ContractError("candidate_pool 不得提交程序拥有的 publication_status")
        if recommendations:
            raise ContractError("candidate_pool 阶段不能预先指定 recommendations")
        if bundle.get("ranking") is not None:
            raise ContractError("candidate_pool 阶段不能携带 ranking")
        return bundle
    if bundle.get("publication_status") != "draft":
        raise ContractError("当前离线排序结果只能是 publication_status=draft；请重新生成候选排序结果")
    if len(recommendations) != target:
        raise ContractError(f"ranked 阶段必须包含 {target} 首推荐，实际为 {len(recommendations)}")
    ranking = _require_dict(bundle.get("ranking"), "ranking")
    _require_text(ranking.get("algorithm_version"), "ranking.algorithm_version")
    if _require_int(ranking.get("selected_count"), "ranking.selected_count") != target:
        raise ContractError("ranking.selected_count 与 target_recommendations 不一致")
    selected_ids = ranking.get("selected_canonical_track_ids")
    if not isinstance(selected_ids, list) or len(selected_ids) != target:
        raise ContractError("ranking.selected_canonical_track_ids 必须完整记录入选歌曲")
    candidates_by_id = {str(item["canonical_track_id"]).casefold(): item for item in candidate_pool}
    favorite_keys = set(str(key) for key in analysis["favorite_track_keys"])
    favorite_platform_ids = {
        normalized_text(item.get("platform_track_id")).casefold()
        for item in analysis["favorite_tracks"]
        if normalized_text(item.get("platform_track_id"))
    }
    seen_keys: set[str] = set()
    seen_canonical_ids: set[str] = set()
    artist_counts: Counter[str] = Counter()
    project_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    for index, item in enumerate(recommendations):
        recommendation = _require_dict(item, f"recommendations[{index}]")
        title = _require_text(recommendation.get("title"), f"recommendations[{index}].title")
        artist = _require_text(recommendation.get("artist"), f"recommendations[{index}].artist")
        project = _require_text(recommendation.get("project"), f"recommendations[{index}].project")
        canonical_id = _require_text(
            recommendation.get("canonical_track_id"),
            f"recommendations[{index}].canonical_track_id",
        ).casefold()
        if canonical_id not in candidates_by_id:
            raise ContractError(f"recommendations[{index}] 不在 candidate_pool 中")
        facts = {key: value for key, value in recommendation.items() if key not in PROGRAM_RANKING_FIELDS}
        if facts != candidates_by_id[canonical_id]:
            raise ContractError(f"recommendations[{index}] 曲目信息与 candidate_pool 不一致")
        if canonical_id in seen_canonical_ids:
            raise ContractError(f"recommendations[{index}] canonical_track_id 重复")
        seen_canonical_ids.add(canonical_id)
        _validate_optional_ranking_fields(recommendation, f"recommendations[{index}]")
        if "ranking_score" not in recommendation or "score_features" not in recommendation or "score_breakdown" not in recommendation:
            raise ContractError(f"recommendations[{index}] 缺少程序生成的完整评分")
        score_total = round(sum(float(value) for value in recommendation["score_breakdown"].values()), 4)
        if abs(score_total - float(recommendation["ranking_score"])) > 0.001:
            raise ContractError(f"recommendations[{index}] ranking_score 与 score_breakdown 不一致")
        artist_marker = normalized_name(artist)
        project_marker = normalized_name(project)
        artist_counts[artist_marker] += 1
        project_counts[project_marker] += 1
        candidate_type = _require_text(
            recommendation.get("candidate_type"),
            f"recommendations[{index}].candidate_type",
        )
        if candidate_type not in CANDIDATE_TYPES:
            raise ContractError(f"recommendations[{index}].candidate_type 无效")
        type_counts[candidate_type] += 1
        if artist_counts[artist_marker] > int(policy["max_per_artist"]):
            raise ContractError(
                f"recommendations[{index}] 超过单艺人上限：{artist}"
            )
        if project_counts[project_marker] > int(policy["max_per_project"]):
            raise ContractError(
                f"recommendations[{index}] 超过单项目上限：{project}"
            )
        candidate_key = track_key(title, artist)
        if candidate_key in favorite_keys:
            raise ContractError(f"recommendations[{index}] 命中当前喜爱歌曲：{title} - {artist}")
        if candidate_key in seen_keys:
            raise ContractError(f"recommendations[{index}] 与前面推荐重复：{title} - {artist}")
        seen_keys.add(candidate_key)
        platform_track_id = normalized_text(recommendation.get("platform_track_id")).casefold()
        if platform_track_id and platform_track_id in favorite_platform_ids:
            raise ContractError(f"recommendations[{index}] 命中当前喜爱歌曲平台 ID")

        refs = recommendation.get("analysis_refs")
        if not isinstance(refs, list) or not refs:
            raise ContractError(f"recommendations[{index}].analysis_refs 不能为空")
        unknown_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in known_refs]
        if unknown_refs:
            raise ContractError(f"recommendations[{index}] 包含未知 analysis_refs：{unknown_refs}")
        relation_path = recommendation.get("relation_path")
        if not isinstance(relation_path, list) or len(relation_path) < 3:
            raise ContractError(f"recommendations[{index}].relation_path 至少需要三段")

        style_refs = recommendation.get("style_refs")
        if not isinstance(style_refs, list) or not style_refs:
            raise ContractError(f"recommendations[{index}].style_refs 不能为空")
        unknown_style_refs = [
            ref for ref in style_refs if not isinstance(ref, str) or ref not in known_style_refs
        ]
        if unknown_style_refs:
            raise ContractError(f"recommendations[{index}] 包含未知 style_refs：{unknown_style_refs}")
        _validate_explanation(recommendation.get("program_explanation"), f"recommendations[{index}].program_explanation")
        _validate_candidate_evidence(recommendation, f"recommendations[{index}]")
        discovery_source = _require_text(
            recommendation.get("discovery_source"),
            f"recommendations[{index}].discovery_source",
        )
        if _source_is_forbidden_personalization(discovery_source):
            raise ContractError("个性化音乐页面不能作为候选来源")
        links = recommendation.get("platform_links")
        if not isinstance(links, dict) or not links:
            raise ContractError(f"recommendations[{index}].platform_links 必须是非空对象")
        for platform, link in links.items():
            _validate_http_url(link, f"recommendations[{index}].platform_links[{platform}]")
    if len(project_counts) < int(policy["min_projects"]):
        raise ContractError(
            "ready 状态的推荐项目覆盖数不足："
            f"需要至少 {policy['min_projects']} 个，实际为 {len(project_counts)} 个"
        )
    expected_types = target_counts(target, analysis)
    if type_counts != Counter(expected_types):
        raise ContractError(
            f"recommendations 候选类型配额不一致：实际 {dict(type_counts)}，要求 {expected_types}"
        )
    if [str(value).casefold() for value in selected_ids] != [
        str(item["canonical_track_id"]).casefold() for item in recommendations
    ]:
        raise ContractError("ranking.selected_canonical_track_ids 与 recommendations 顺序不一致")
    return bundle


def validate_recommendation_file(bundle_path: Path, analysis_path: Path) -> dict[str, Any]:
    packet = validate_analysis_packet(read_json(analysis_path))
    return validate_recommendation_bundle(read_json(bundle_path), packet)


def count_distribution(items: Iterable[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        value = normalized_text(item.get(field))
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    ordered = sorted(counts.items(), key=lambda entry: (-entry[1], entry[0].casefold()))
    return [
        {
            "rank": rank,
            "artist": name,
            "entity_ref": f"artist:{artist_key(name)}",
            "count": count,
        }
        for rank, (name, count) in enumerate(ordered, 1)
    ]


def validate_feedback_record(value: Any) -> dict[str, Any]:
    """Validate a single recommendation-feedback input contract.

    Feedback is a recorded user outcome for a previously ranked
    recommendation. It is never used to adjust ranking policy at runtime:
    offline evaluation and human-approved tuning consume it afterwards.
    """

    record = _require_dict(value, "FeedbackRecord")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"FeedbackRecord.schema_version 必须是 {SCHEMA_VERSION}")
    if record.get("record_type") != "recommendation_feedback":
        raise ContractError("record_type 必须是 recommendation_feedback")
    outcome = _require_text(record.get("outcome"), "outcome")
    if outcome not in FEEDBACK_OUTCOMES:
        raise ContractError(f"outcome 必须是 {sorted(FEEDBACK_OUTCOMES)} 之一：{outcome}")
    _require_text(record.get("timestamp"), "timestamp")
    _require_text(record.get("analysis_id"), "analysis_id")
    _require_text(record.get("recommendation_id"), "recommendation_id")
    bundle_id = record.get("bundle_id")
    if bundle_id is not None:
        _require_text(bundle_id, "bundle_id")
    position = record.get("position")
    if position is not None:
        _require_int(position, "position", 1)
    note = record.get("note")
    if note is not None:
        _require_text(note, "note")
    return record


def validate_feedback_log(value: Any) -> list[dict[str, Any]]:
    """Validate an ordered list of feedback records from one feedback source."""

    if not isinstance(value, list):
        raise ContractError("反馈记录必须是数组")
    return [validate_feedback_record(item) for item in value]


def validate_feedback_log_refs(
    log: list[dict[str, Any]],
    bundle: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Check feedback records reference the current analysis and ranked bundle.

    A feedback record matches a recommendation when its analysis_id equals the
    packet id and its recommendation_id equals a canonical track id in the
    ranked recommendations. Unmatched records are reported, not dropped by
    ranking policy (the ranking is never changed by feedback). Returns
    ``{"matched": [...], "unmatched": [...]}``.
    """

    analysis_id = packet["analysis_id"]
    ranked_ids = {
        str(item.get("canonical_track_id")).casefold()
        for item in bundle.get("recommendations", [])
        if isinstance(item, dict) and item.get("canonical_track_id")
    }
    checked: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for record in log:
        if record.get("analysis_id") != analysis_id:
            unmatched.append(
                {
                    "reason": "analysis_id_mismatch",
                    "recommendation_id": record.get("recommendation_id"),
                    "recorded_analysis_id": record.get("analysis_id"),
                    "current_analysis_id": analysis_id,
                }
            )
            continue
        if str(record.get("recommendation_id") or "").casefold() not in ranked_ids:
            unmatched.append(
                {
                    "reason": "recommendation_not_in_ranked_bundle",
                    "recommendation_id": record.get("recommendation_id"),
                }
            )
            continue
        checked.append(record)
    return {"matched": checked, "unmatched": unmatched}


def validate_feedback_file(feedback_path: Path, bundle_path: Path, analysis_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load and validate a feedback log against a ranked bundle and packet."""

    bundle = read_json(bundle_path)
    packet = read_json(analysis_path)
    log = validate_feedback_log(read_json(feedback_path))
    return validate_feedback_log_refs(log, bundle, packet)
