#!/usr/bin/env python3
"""Deterministic scoring, constrained re-ranking and playlist sequencing."""

from __future__ import annotations

import math
from collections import Counter
from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

from contracts import (
    ContractError,
    RANKING_FEATURE_KEYS,
    normalized_name,
    normalized_text,
    recall_mix_ratios,
    target_counts,
    track_key,
)


FEATURE_KEYS = RANKING_FEATURE_KEYS
DEFAULT_WEIGHTS = {
    "style_fit": 0.30,
    "axis_fit": 0.20,
    "relation_fit": 0.15,
    "frequency_fit": 0.10,
    "novelty": 0.10,
    "evidence_quality": 0.10,
    "public_association": 0.05,
}
CONFIDENCE_FACTORS = {"high": 1.0, "medium": 0.78, "low": 0.55}
EVIDENCE_BASE = {"A": 88.0, "B": 73.0, "C": 58.0}


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(100.0, number))


def _ranking_weights(packet: dict[str, Any]) -> dict[str, float]:
    raw = packet.get("recommendation_policy", {}).get("ranking_weights", DEFAULT_WEIGHTS)
    if not isinstance(raw, dict) or set(raw) != set(FEATURE_KEYS):
        raise ContractError("ranking_weights 必须完整且仅包含七项评分维度")
    weights = {key: max(0.0, float(raw[key])) for key in FEATURE_KEYS}
    total = sum(weights.values())
    if total <= 0:
        raise ContractError("ranking_weights 总和必须大于 0")
    return {key: value / total for key, value in weights.items()}


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    keys = set(left) | set(right)
    numerator = sum(left.get(key, 0.0) * right.get(key, 0.0) for key in keys)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _candidate_style_vector(candidate: dict[str, Any]) -> dict[str, float]:
    return {
        str(item["style_ref"]): float(item["weight"])
        for item in candidate.get("style_mix", [])
        if isinstance(item, dict) and item.get("style_ref")
    }


def _user_style_vector(packet: dict[str, Any]) -> dict[str, float]:
    result: Counter[str] = Counter()
    for assignment in packet.get("track_style_assignments", []):
        if not isinstance(assignment, dict) or assignment.get("classification_status") != "classified":
            continue
        confidence = CONFIDENCE_FACTORS.get(str(assignment.get("confidence") or "low"), 0.55)
        mix = assignment.get("style_mix", [])
        if isinstance(mix, list) and mix:
            for item in mix:
                if isinstance(item, dict) and item.get("style_ref"):
                    result[str(item["style_ref"])] += float(item.get("weight") or 0.0) * confidence
        else:
            refs = [ref for ref in assignment.get("style_refs", []) if isinstance(ref, str)]
            for ref in refs:
                result[ref] += confidence / max(1, len(refs))
    return dict(result)


def _parent_map(packet: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("style_ref")): str(item.get("parent") or item.get("style_ref"))
        for item in packet.get("style_analysis", {}).get("style_definitions", [])
        if isinstance(item, dict) and item.get("style_ref")
    }


def _parent_vector(vector: dict[str, float], parents: dict[str, str]) -> dict[str, float]:
    result: Counter[str] = Counter()
    for style_ref, value in vector.items():
        result[parents.get(style_ref, style_ref)] += value
    return dict(result)


def _style_fit(candidate: dict[str, Any], packet: dict[str, Any]) -> float:
    user = _user_style_vector(packet)
    item = _candidate_style_vector(candidate)
    if not user or not item:
        return 0.0
    parents = _parent_map(packet)
    exact = _cosine(user, item)
    family = _cosine(_parent_vector(user, parents), _parent_vector(item, parents))
    confidence = CONFIDENCE_FACTORS.get(str(candidate.get("style_confidence") or "low"), 0.55)
    return _clamp((exact * 0.85 + family * 0.15) * (0.85 + 0.15 * confidence) * 100.0)


def _axis_fit(candidate: dict[str, Any], packet: dict[str, Any]) -> float:
    user_axes = packet.get("style_analysis", {}).get("style_axes", {})
    candidate_axes = candidate.get("style_axes", {})
    common = [key for key in user_axes if key in candidate_axes]
    if not common:
        return 0.0
    mean_distance = sum(abs(float(user_axes[key]) - float(candidate_axes[key])) for key in common) / len(common)
    confidence = CONFIDENCE_FACTORS.get(str(candidate.get("style_confidence") or "low"), 0.55)
    return _clamp((100.0 - mean_distance) * (0.88 + 0.12 * confidence))


def _entity_affinities(packet: dict[str, Any]) -> list[tuple[dict[str, Any], float]]:
    counts = {
        str(item.get("entity_ref")): int(item.get("count") or 0)
        for item in packet.get("primary_distribution", [])
        if isinstance(item, dict) and item.get("entity_ref")
    }
    peak = max(counts.values(), default=1)
    preferred_refs = {
        str(item.get("entity_ref"))
        for item in packet.get("preferred_artists", [])
        if isinstance(item, dict) and item.get("entity_ref")
    }
    result: list[tuple[dict[str, Any], float]] = []
    for entity in packet.get("entities", []):
        if not isinstance(entity, dict):
            continue
        ref = str(entity.get("entity_ref") or "")
        affinity = counts.get(ref, 0) / peak
        if ref in preferred_refs:
            affinity = max(affinity, 1.0)
        result.append((entity, affinity))
    return result


def _candidate_ref_strength(candidate: dict[str, Any], packet: dict[str, Any]) -> tuple[float, float]:
    refs = {str(ref) for ref in candidate.get("analysis_refs", []) if isinstance(ref, str)}
    relation_strength = 0.0
    frequency_strength = 0.0
    for entity, affinity in _entity_affinities(packet):
        entity_ref = str(entity.get("entity_ref") or "")
        analysis_refs = {str(ref) for ref in entity.get("analysis_refs", []) if isinstance(ref, str)}
        if entity_ref in refs:
            relation_strength = max(relation_strength, 1.0)
            frequency_strength = max(frequency_strength, affinity)
        if refs & (analysis_refs - {entity_ref}):
            relation_strength = max(relation_strength, 0.90)
            frequency_strength = max(frequency_strength, affinity * 0.85)
    return relation_strength, frequency_strength


def _relation_fit(candidate: dict[str, Any], packet: dict[str, Any]) -> float:
    graph_strength, _ = _candidate_ref_strength(candidate, packet)
    base = {
        "artist_continuation": 0.92,
        "musician_relation": 0.88,
        "style_neighbor": 0.50,
        "exploration": 0.32,
    }.get(str(candidate.get("candidate_type") or ""), 0.0)
    return _clamp((base * 0.45 + graph_strength * 0.55) * 100.0)


def _frequency_fit(candidate: dict[str, Any], packet: dict[str, Any]) -> float:
    _, frequency_strength = _candidate_ref_strength(candidate, packet)
    return _clamp(frequency_strength * 100.0)


def _reference_date(packet: dict[str, Any]) -> date:
    raw = str(packet.get("generated_at") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return datetime.now(UTC).date()


def _novelty(candidate: dict[str, Any], packet: dict[str, Any]) -> float:
    favorite_artists = {
        normalized_name(item.get("artist"))
        for item in packet.get("primary_distribution", [])
        if isinstance(item, dict)
    }
    if normalized_name(candidate.get("artist")) in favorite_artists:
        base = 35.0
    else:
        base = {
            "artist_continuation": 48.0,
            "musician_relation": 64.0,
            "style_neighbor": 78.0,
            "exploration": 92.0,
        }.get(str(candidate.get("candidate_type") or ""), 55.0)
    raw_date = candidate.get("release_date")
    if isinstance(raw_date, str):
        try:
            age_days = max(0, (_reference_date(packet) - date.fromisoformat(raw_date)).days)
            if age_days <= 90:
                base += 6.0
            elif age_days <= 365:
                base += 3.0
        except ValueError:
            pass
    return _clamp(base)


def _evidence_quality(candidate: dict[str, Any]) -> float:
    grade = str(candidate.get("evidence_grade") or "C").upper()
    items = [item for item in candidate.get("evidence_items", []) if isinstance(item, dict)]
    claim_types = {str(item.get("claim_type")) for item in items}
    domains = {
        urlparse(str(item.get("url") or "")).netloc.casefold()
        for item in items
        if urlparse(str(item.get("url") or "")).netloc
    }
    coverage = len(claim_types) / 4.0
    diversity_bonus = min(6.0, max(0, len(domains) - 1) * 3.0)
    return _clamp(EVIDENCE_BASE.get(grade, 0.0) + coverage * 6.0 + diversity_bonus)


def _public_association(candidate: dict[str, Any], packet: dict[str, Any]) -> float:
    relation_strength, _ = _candidate_ref_strength(candidate, packet)
    discovery = str(candidate.get("discovery_source") or "").casefold()
    trusted = 1.0 if any(marker in discovery for marker in ("official", "musicbrainz", "wikidata", "bandcamp")) else 0.72
    source_count = len({str(value) for value in candidate.get("sources", [])})
    source_factor = min(1.0, 0.55 + source_count * 0.15)
    return _clamp((relation_strength * 0.55 + trusted * 0.25 + source_factor * 0.20) * 100.0)


def score_candidate(candidate: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Calculate all seven dimensions from structured facts, never Agent scores."""

    weights = _ranking_weights(packet)
    features = {
        "style_fit": round(_style_fit(candidate, packet), 4),
        "axis_fit": round(_axis_fit(candidate, packet), 4),
        "relation_fit": round(_relation_fit(candidate, packet), 4),
        "frequency_fit": round(_frequency_fit(candidate, packet), 4),
        "novelty": round(_novelty(candidate, packet), 4),
        "evidence_quality": round(_evidence_quality(candidate), 4),
        "public_association": round(_public_association(candidate, packet), 4),
    }
    breakdown = {key: round(features[key] * weights[key], 4) for key in FEATURE_KEYS}
    return {"score": round(sum(breakdown.values()), 4), "features": features, "breakdown": breakdown}


def _style_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    return _cosine(_candidate_style_vector(left), _candidate_style_vector(right))


def _axis_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_axes = left.get("style_axes", {})
    right_axes = right.get("style_axes", {})
    common = [key for key in left_axes if key in right_axes]
    if not common:
        return 0.0
    distance = sum(abs(float(left_axes[key]) - float(right_axes[key])) for key in common) / len(common)
    return max(0.0, 1.0 - distance / 100.0)


def _candidate_similarity(left: dict[str, Any], right: dict[str, Any], packet: dict[str, Any]) -> float:
    weights = packet["recommendation_policy"]["diversity_policy"]["similarity_weights"]
    same_artist = normalized_name(left.get("artist")) == normalized_name(right.get("artist"))
    same_project = normalized_name(left.get("project")) == normalized_name(right.get("project"))
    return min(
        1.0,
        _style_similarity(left, right) * float(weights["style"])
        + _axis_similarity(left, right) * float(weights["axis"])
        + (float(weights["artist"]) if same_artist else 0.0)
        + (float(weights["project"]) if same_project else 0.0),
    )


def _energy(candidate: dict[str, Any]) -> float:
    axes = candidate.get("style_axes", {})
    keys = ("heaviness", "aggression", "rhythmic_density", "vocal_harshness", "emotional_intensity")
    values = [float(axes[key]) for key in keys if key in axes]
    return sum(values) / len(values) if values else 0.0


def _axis_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    return (1.0 - _axis_similarity(left, right)) * 100.0


def _sequence_candidates(selected: list[dict[str, Any]], packet: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(selected) <= 1:
        ordered = list(selected)
    else:
        policy = packet["recommendation_policy"]["sequence_policy"]
        transition_weight = float(policy["transition_weight"])
        arc_weight = float(policy["arc_weight"])
        ranking_weight = float(policy["ranking_weight"])
        energies = [_energy(item) for item in selected]
        low, high = min(energies), max(energies)
        end = low + (high - low) * 0.35
        peak_index = max(1, round((len(selected) - 1) * 0.60))

        def target(position: int) -> float:
            if position <= peak_index:
                return low + (high - low) * position / peak_index
            tail = max(1, len(selected) - 1 - peak_index)
            return high - (high - end) * (position - peak_index) / tail

        remaining = list(selected)
        ordered = []
        for position in range(len(selected)):
            best_index = 0
            best_cost = float("inf")
            for index, candidate in enumerate(remaining):
                transition = _axis_distance(ordered[-1], candidate) if ordered else 0.0
                arc_error = abs(_energy(candidate) - target(position))
                rank_cost = 100.0 - float(candidate["ranking_score"])
                familiar_penalty = 0.0
                if position == 0 and policy.get("allow_familiar_anchor"):
                    familiar_penalty = 0.0 if candidate.get("candidate_type") == "artist_continuation" else 8.0
                cost = transition * transition_weight + arc_error * arc_weight + rank_cost * ranking_weight + familiar_penalty
                if cost < best_cost:
                    best_cost = cost
                    best_index = index
            ordered.append(remaining.pop(best_index))
    for index, candidate in enumerate(ordered, 1):
        candidate["sequence_position"] = index
        candidate["sequence_energy"] = round(_energy(candidate), 4)
    return ordered, {
        "mode": "energy_arc",
        "positions": [
            {
                "canonical_track_id": item["canonical_track_id"],
                "position": item["sequence_position"],
                "energy": item["sequence_energy"],
                "transition_distance": round(_axis_distance(ordered[index - 1], item), 4) if index else 0.0,
            }
            for index, item in enumerate(ordered)
        ],
    }


def rank_candidates(candidates: list[dict[str, Any]], packet: dict[str, Any], *, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply exact recall quotas, hard caps, MMR diversity and sequencing."""

    if limit <= 0:
        raise ContractError("推荐数量必须大于 0")
    policy = packet["recommendation_policy"]
    max_per_artist = int(policy["max_per_artist"])
    max_per_project = int(policy["max_per_project"])
    min_projects = int(policy["min_projects"])
    diversity = policy["diversity_policy"]
    favorite_keys = {str(value) for value in packet.get("favorite_track_keys", [])}
    favorite_platform_ids = {
        normalized_text(item.get("platform_track_id")).casefold()
        for item in packet.get("favorite_tracks", [])
        if normalized_text(item.get("platform_track_id"))
    }
    prepared: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    for source_index, original in enumerate(candidates):
        if not isinstance(original, dict):
            continue
        candidate = deepcopy(original)
        candidate_key = track_key(candidate.get("title"), candidate.get("artist"))
        canonical_id = normalized_text(candidate.get("canonical_track_id")).casefold()
        platform_id = normalized_text(candidate.get("platform_track_id")).casefold()
        if not candidate_key or not canonical_id or candidate_key in seen_keys or canonical_id in seen_ids:
            continue
        if candidate_key in favorite_keys or (platform_id and platform_id in favorite_platform_ids):
            continue
        seen_keys.add(candidate_key)
        seen_ids.add(canonical_id)
        score = score_candidate(candidate, packet)
        candidate["ranking_score"] = score["score"]
        candidate["score_breakdown"] = score["breakdown"]
        candidate["score_features"] = score["features"]
        candidate["_source_index"] = source_index
        prepared.append(candidate)
    if not prepared:
        raise ContractError("candidate_pool 没有可用于排序的候选歌曲")

    quotas = target_counts(limit, packet)
    available = Counter(str(item.get("candidate_type")) for item in prepared)
    shortages = {kind: count - available[kind] for kind, count in quotas.items() if available[kind] < count}
    if shortages:
        raise ContractError(f"candidate_pool 无法满足召回配额：{shortages}")
    prepared.sort(key=lambda item: (-float(item["ranking_score"]), int(item["_source_index"])))
    selected: list[dict[str, Any]] = []
    artist_counts: Counter[str] = Counter()
    project_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    remaining = list(prepared)
    while remaining and len(selected) < limit:
        slots_left = limit - len(selected)
        projects_needed = max(0, min_projects - len(project_counts))
        best_index = -1
        best_value = float("-inf")
        for index, candidate in enumerate(remaining):
            type_name = str(candidate.get("candidate_type"))
            if type_counts[type_name] >= quotas.get(type_name, 0):
                continue
            artist_marker = normalized_name(candidate.get("artist"))
            project_marker = normalized_name(candidate.get("project"))
            if artist_counts[artist_marker] >= max_per_artist or project_counts[project_marker] >= max_per_project:
                continue
            if projects_needed >= slots_left and project_marker in project_counts:
                continue
            similarity = max((_candidate_similarity(candidate, chosen, packet) for chosen in selected), default=0.0)
            type_bonus = float(diversity["candidate_type_bonus"])
            project_bonus = float(diversity["new_project_bonus"]) if project_marker not in project_counts and len(project_counts) < min_projects else 0.0
            adjusted = float(candidate["ranking_score"]) + type_bonus + project_bonus - similarity * float(diversity["mmr_penalty"])
            if adjusted > best_value:
                best_value = adjusted
                best_index = index
        if best_index < 0:
            break
        chosen = remaining.pop(best_index)
        chosen["selection_rank"] = len(selected) + 1
        chosen["selection_adjusted_score"] = round(best_value, 4)
        selected.append(chosen)
        artist_counts[normalized_name(chosen.get("artist"))] += 1
        project_counts[normalized_name(chosen.get("project"))] += 1
        type_counts[str(chosen.get("candidate_type"))] += 1
    if len(selected) != limit:
        raise ContractError(f"candidate_pool 经硬约束后只能选出 {len(selected)} 首，要求 {limit} 首")
    if dict(type_counts) != quotas:
        raise ContractError(f"候选类型配额未满足：实际 {dict(type_counts)}，要求 {quotas}")
    if len(project_counts) < min_projects:
        raise ContractError(f"候选项目覆盖不足：实际 {len(project_counts)}，要求 {min_projects}")
    for candidate in selected:
        candidate.pop("_source_index", None)
    sequenced, sequence_manifest = _sequence_candidates(selected, packet)
    manifest = {
        "algorithm_version": str(policy.get("algorithm_version") or "hybrid_music_discovery_v2"),
        "weights": _ranking_weights(packet),
        "recall_mix": [
            {"candidate_type": candidate_type, "target_ratio": round(ratio, 6)}
            for candidate_type, ratio in recall_mix_ratios(packet)
        ],
        "target_counts": quotas,
        "diversity_policy": deepcopy(diversity),
        "selected_count": len(sequenced),
        "selected_project_count": len(project_counts),
        "selected_candidate_types": dict(type_counts),
        "selected_canonical_track_ids": [item["canonical_track_id"] for item in sequenced],
        "sequence": sequence_manifest,
    }
    return sequenced, manifest


def rank_bundle(bundle: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Turn a validated candidate-pool bundle into a ranked bundle."""

    if bundle.get("status") != "ready":
        return bundle
    if bundle.get("bundle_stage") == "ranked":
        return bundle
    if bundle.get("bundle_stage") != "candidate_pool":
        raise ContractError("ready RecommendationBundle 必须从 candidate_pool 阶段开始")
    candidate_pool = bundle.get("candidate_pool")
    if not isinstance(candidate_pool, list) or not candidate_pool:
        raise ContractError("ready RecommendationBundle 必须包含候选池")
    limit = int(packet["recommendation_policy"]["target_recommendations"])
    recommendations, manifest = rank_candidates(candidate_pool, packet, limit=limit)
    ranked = deepcopy(bundle)
    ranked["bundle_stage"] = "ranked"
    ranked["recommendations"] = recommendations
    ranked["ranking"] = manifest
    return ranked
