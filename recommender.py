#!/usr/bin/env python3
"""Deterministic scoring, constrained re-ranking and playlist sequencing."""

from __future__ import annotations

import math
from collections import Counter
from copy import deepcopy
from datetime import date
from typing import Any
from urllib.parse import urlparse

from contracts import (
    ContractError,
    normalized_name,
    normalized_text,
    parse_as_of_date,
    recall_mix_ratios,
    target_counts,
    track_key,
    validate_recommendation_bundle,
    require_analysis_coverage,
)
from evidence import classify_source, require_usable_evidence, source_evidence_grade
from candidate_routes import resolve_candidate_route
from preference_model import interest_profiles, match_interest, style_vector
from explanations import explain_selected


DEFAULT_WEIGHTS = {
    "style_fit": 0.35,
    "relation_fit": 0.20,
    "frequency_fit": 0.15,
    "novelty": 0.10,
    "evidence_quality": 0.15,
    "public_association": 0.05,
}
FEATURE_KEYS = tuple(DEFAULT_WEIGHTS)
CONFIDENCE_FACTORS = {"high": 1.0, "medium": 0.78, "low": 0.55}
# Album/artist tags are useful context but not a claim about the recording.
SCOPE_FACTORS = {"track": 1.0, "album": 0.8, "artist": 0.55}
EVIDENCE_BASE = {"A": 88.0, "B": 73.0, "C": 58.0}


class SelectionSearchBudgetExceeded(ContractError):
    """The bounded search could not establish feasibility or infeasibility."""


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
        raise ContractError("ranking_weights 必须完整且仅包含六项有据可查的评分维度")
    weights = {key: max(0.0, float(raw[key])) for key in FEATURE_KEYS}
    total = sum(weights.values())
    if total <= 0:
        raise ContractError("ranking_weights 总和必须大于 0")
    return {key: value / total for key, value in weights.items()}


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    keys = sorted(set(left) | set(right))
    numerator = sum(left.get(key, 0.0) * right.get(key, 0.0) for key in keys)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _candidate_style_vector(candidate: dict[str, Any]) -> dict[str, float]:
    # A style is usable only when the candidate carries a public source for it.
    sources = set(candidate.get("sources") or [])
    evidence = candidate.get("style_evidence") or {}
    if (evidence.get("status") == "supported" and evidence.get("scope") in {"track", "album", "artist"}
            and evidence.get("url") in sources and evidence.get("retrieved_at")):
        refs = [tag.get("style_ref") for tag in evidence.get("tags", [])
                if isinstance(tag, dict) and isinstance(tag.get("style_ref"), str)]
        if refs:
            counts = Counter(refs)
            return {ref: value / len(refs) for ref, value in counts.items()}
    public_styles = [item for item in candidate.get("evidence_items", [])
                     if isinstance(item, dict) and item.get("claim_type") == "style"
                     and item.get("url") in sources]
    return style_vector(candidate) if public_styles else {}


def _user_style_vector(packet: dict[str, Any]) -> dict[str, float]:
    result: Counter[str] = Counter()
    for assignment in packet.get("track_style_assignments", []):
        if not isinstance(assignment, dict) or assignment.get("classification_status") != "classified":
            continue
        confidence = CONFIDENCE_FACTORS.get(str(assignment.get("confidence") or "low"), 0.55)
        confidence *= SCOPE_FACTORS.get(str(assignment.get("applied_scope") or "track"), 1.0)
        mix = assignment.get("style_mix", [])
        if isinstance(mix, list) and mix:
            for item in mix:
                if isinstance(item, dict) and item.get("style_ref"):
                    result[str(item["style_ref"])] += float(item.get("weight") or 0.0) * confidence
        else:
            refs = [ref for ref in assignment.get("style_refs", []) if isinstance(ref, str)]
            for ref in refs:
                result[ref] += confidence / max(1, len(refs))
    if result:
        return dict(result)
    # Artist-only analysis contains no per-track classification. Artist profiles
    # represent documented public tags and are weighted by playlist frequency.
    frequency = {normalized_name(item.get("artist")): int(item.get("count") or 0)
                 for item in packet.get("primary_distribution", []) if isinstance(item, dict)}
    for profile in packet.get("style_analysis", {}).get("artist_profiles", []):
        if not isinstance(profile, dict) or profile.get("classification_status") != "classified":
            continue
        count = max(1, frequency.get(normalized_name(profile.get("artist")),
                                      int(profile.get("primary_track_count") or 1)))
        for ref, value in style_vector(profile).items():
            result[ref] += value * count * CONFIDENCE_FACTORS.get(str(profile.get("confidence") or "low"), 0.55)
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
    interest = match_interest(candidate, packet)
    user = style_vector(interest) if interest else _user_style_vector(packet)
    item = _candidate_style_vector(candidate)
    if not user or not item:
        return 0.0
    parents = _parent_map(packet)
    exact = _cosine(user, item)
    family = _cosine(_parent_vector(user, parents), _parent_vector(item, parents))
    confidence = CONFIDENCE_FACTORS.get(str(candidate.get("style_confidence") or "low"), 0.55)
    source_scope = (candidate.get("style_evidence") or {}).get("scope")
    scope_factor = SCOPE_FACTORS.get(str(source_scope), 1.0)
    return _clamp((exact * 0.85 + family * 0.15) * (0.85 + 0.15 * confidence) * scope_factor * 100.0)


def _candidate_ref_strength(candidate: dict[str, Any], packet: dict[str, Any]) -> tuple[float, float]:
    route = resolve_candidate_route(candidate, packet)
    return route["strength"], route["frequency_strength"]


def _relation_fit(candidate: dict[str, Any], packet: dict[str, Any]) -> float:
    graph_strength, _ = _candidate_ref_strength(candidate, packet)
    route = resolve_candidate_route(candidate, packet)
    base = {
        "artist_continuation": 0.92,
        "musician_relation": 0.88,
        "style_neighbor": 0.50,
        "exploration": 0.32,
    }.get(route["candidate_type"], 0.0)
    if route["verification_scope"] == "no_style_evidence":
        base = 0.0
    return _clamp((base * 0.45 + graph_strength * 0.55) * 100.0)


def _frequency_fit(candidate: dict[str, Any], packet: dict[str, Any]) -> float:
    _, frequency_strength = _candidate_ref_strength(candidate, packet)
    return _clamp(frequency_strength * 100.0)


def _reference_date(packet: dict[str, Any]) -> date:
    return parse_as_of_date(packet.get("as_of_date"))


def _novelty(candidate: dict[str, Any], packet: dict[str, Any]) -> float:
    reference_date = _reference_date(packet)
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
        }.get(resolve_candidate_route(candidate, packet)["candidate_type"], 55.0)
    raw_date = candidate.get("release_date")
    has_release_source = any(item.get("claim_type") == "release" and item.get("url") in (candidate.get("sources") or [])
                             for item in candidate.get("evidence_items", []) if isinstance(item, dict))
    metadata = candidate.get("metadata_verified") or {}
    has_release_source |= bool(isinstance(metadata, dict) and metadata.get("url") in (candidate.get("sources") or []))
    if isinstance(raw_date, str) and has_release_source:
        try:
            age_days = max(0, (reference_date - date.fromisoformat(raw_date)).days)
            if age_days <= 90:
                base += 6.0
            elif age_days <= 365:
                base += 3.0
        except ValueError:
            pass
    return _clamp(base)


def _evidence_quality(candidate: dict[str, Any]) -> float:
    grade = source_evidence_grade(candidate)
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
    association_items = [item for item in candidate.get("evidence_items", [])
                         if isinstance(item, dict) and item.get("claim_type") in {"style", "relation"}
                         and item.get("url") in (candidate.get("sources") or [])]
    if not association_items and not resolve_candidate_route(candidate, packet)["sources"]:
        return 0.0
    source_classes = {classify_source(str(item.get("url") or "")) for item in association_items}
    trusted = 1.0 if source_classes & {"official", "musicbrainz", "wikidata", "bandcamp"} else 0.72
    source_count = len({urlparse(str(item.get("url") or "")).netloc.casefold() for item in association_items})
    source_factor = min(1.0, 0.55 + source_count * 0.15)
    return _clamp((relation_strength * 0.55 + trusted * 0.25 + source_factor * 0.20) * 100.0)


def score_candidate(candidate: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Score sourced facts; unavailable styles are excluded, never valued at zero."""

    weights = _ranking_weights(packet)
    features = {
        "style_fit": round(_style_fit(candidate, packet), 4),
        "relation_fit": round(_relation_fit(candidate, packet), 4),
        "frequency_fit": round(_frequency_fit(candidate, packet), 4),
        "novelty": round(_novelty(candidate, packet), 4),
        "evidence_quality": round(_evidence_quality(candidate), 4),
        "public_association": round(_public_association(candidate, packet), 4),
    }
    # Missing source-backed style evidence is unavailable, not a zero score.
    unavailable = set()
    if not _candidate_style_vector(candidate) or not (
        style_vector(match_interest(candidate, packet)) if match_interest(candidate, packet)
        else _user_style_vector(packet)
    ):
        unavailable.add("style_fit")
    active_weight = sum(weight for key, weight in weights.items() if key not in unavailable)
    effective_weights = {key: (weight / active_weight if key not in unavailable and active_weight else 0.0)
                         for key, weight in weights.items()}
    breakdown = {key: round(features[key] * effective_weights[key], 4) for key in FEATURE_KEYS}
    return {"score": round(sum(breakdown.values()), 4), "features": features,
            "breakdown": breakdown, "unavailable_features": sorted(unavailable)}


def _style_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    return _cosine(_candidate_style_vector(left), _candidate_style_vector(right))


def _candidate_similarity(left: dict[str, Any], right: dict[str, Any], packet: dict[str, Any]) -> float:
    weights = packet["recommendation_policy"]["diversity_policy"]["similarity_weights"]
    same_artist = normalized_name(left.get("artist")) == normalized_name(right.get("artist"))
    same_project = normalized_name(left.get("project")) == normalized_name(right.get("project"))
    return min(
        1.0,
        _style_similarity(left, right) * float(weights["style"])
        + (float(weights["artist"]) if same_artist else 0.0)
        + (float(weights["project"]) if same_project else 0.0),
    )


def _sequence_candidates(selected: list[dict[str, Any]], packet: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Order by documented tag continuity and rank, never an inferred energy arc."""
    policy = packet["recommendation_policy"]["sequence_policy"]
    if len(selected) <= 1:
        ordered = list(selected)
    else:
        transition_weight = float(policy["transition_weight"])
        ranking_weight = float(policy["ranking_weight"])
        remaining = list(selected)
        ordered = []
        for position in range(len(selected)):
            best_index = 0
            best_key: tuple[float, str] | None = None
            for index, candidate in enumerate(remaining):
                previous = ordered[-1] if ordered else None
                left = _candidate_style_vector(previous) if previous else {}
                right = _candidate_style_vector(candidate)
                # No tag evidence: neutral cost, never a fabricated 0% or 100% similarity.
                transition = (100.0 * (1.0 - _cosine(left, right)) if left and right else 50.0)
                rank_cost = 100.0 - float(candidate["ranking_score"])
                familiar_penalty = 0.0
                if position == 0 and policy.get("allow_familiar_anchor"):
                    familiar_penalty = 0.0 if candidate.get("candidate_type") == "artist_continuation" else 8.0
                cost = (transition * transition_weight if previous and policy.get("prefer_adjacent_transitions") else 0.0)
                cost += rank_cost * ranking_weight + familiar_penalty
                key = (cost, str(candidate.get("canonical_track_id") or ""))
                if best_key is None or key < best_key:
                    best_key = key
                    best_index = index
            ordered.append(remaining.pop(best_index))
    for index, candidate in enumerate(ordered, 1):
        candidate["sequence_position"] = index
    return ordered, {
        "mode": "public_tag_continuity",
        "positions": [
            {
                "canonical_track_id": item["canonical_track_id"],
                "position": item["sequence_position"],
                "transition_distance": (
                    round(100.0 * (1.0 - _style_similarity(ordered[index - 1], item)), 4)
                    if index and _candidate_style_vector(ordered[index - 1]) and _candidate_style_vector(item)
                    else None
                ),
            }
            for index, item in enumerate(ordered)
        ],
    }


def _adaptive_quotas(limit: int, packet: dict[str, Any], prepared: list[dict[str, Any]]) -> dict[str, int]:
    """按 recall_mix 分配配额，并把无法满足的部分收缩到实际可用数量。

    候选池不足时（例如只研究了少数候选），先按可用数量裁剪，再把缺口补给仍有余量的
    类型；只有全部候选都被排进去仍填不满 limit 时才留下缺口，由选曲搜索按实际数量返回。
    """

    quotas = target_counts(limit, packet)
    available = Counter(str(item.get("candidate_type")) for item in prepared)
    for kind in list(quotas):
        quotas[kind] = min(quotas[kind], available.get(kind, 0))
    deficit = limit - sum(quotas.values())
    if deficit > 0:
        order = [kind for kind, _ratio in recall_mix_ratios(packet)]
        order += [kind for kind in available if kind not in order]
        for kind in order:
            if deficit <= 0:
                break
            spare = available.get(kind, 0) - quotas.get(kind, 0)
            if spare <= 0:
                continue
            take = min(spare, deficit)
            quotas[kind] = quotas.get(kind, 0) + take
            deficit -= take
    return quotas


def rank_candidates(
    candidates: list[dict[str, Any]], packet: dict[str, Any], *, limit: int,
    search_budget: int = 50000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    exclusion = packet.get("playlist_exclusion") if isinstance(packet.get("playlist_exclusion"), dict) else {}
    favorite_keys |= {str(value) for value in exclusion.get("track_keys") or []}
    favorite_platform_ids |= {normalized_text(value).casefold() for value in exclusion.get("platform_track_ids") or []}
    prepared: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    for source_index, original in enumerate(candidates):
        if not isinstance(original, dict):
            continue
        require_usable_evidence(original)
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
        candidate["resolved_route"] = resolve_candidate_route(candidate, packet)
        interest = match_interest(candidate, packet)
        candidate["matched_interest_id"] = interest["interest_id"] if interest else None
        candidate["_source_index"] = source_index
        prepared.append(candidate)
    if not prepared:
        raise ContractError("candidate_pool 没有可用于排序的候选歌曲")

    # 候选少于目标时按可用数量收缩（有多少用多少），不因候选不足整体失败；
    # 项目覆盖下限同样不能超过实际可用项目数，否则只会变成无解。
    limit = min(limit, len(prepared))
    min_projects = min(
        min_projects,
        len({str(item.get("project") or "") for item in prepared if item.get("project")}),
    )
    quotas = _adaptive_quotas(limit, packet, prepared)
    prepared.sort(key=lambda item: (-float(item["ranking_score"]), int(item["_source_index"])))
    selected: list[dict[str, Any]] = []
    artist_counts: Counter[str] = Counter()
    project_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    interest_counts: Counter[str] = Counter()
    min_interests = min(int(diversity.get("min_interest_groups", 1)), len(interest_profiles(packet)))
    # 候选匹配到的兴趣组可能少于策略要求；不收缩就会变成搜索无解。
    min_interests = min(
        min_interests,
        len({item.get("matched_interest_id") for item in prepared if item.get("matched_interest_id")}),
    )
    failed_states: set[frozenset[int]] = set()
    visited = 0

    def search(remaining: list[dict[str, Any]]) -> list[tuple[dict[str, Any], float]] | None:
        nonlocal visited
        visited += 1
        if visited > search_budget:
            raise SelectionSearchBudgetExceeded(f"选曲搜索预算耗尽（{search_budget} 个状态），尚不能判定约束是否有解")
        state = frozenset(item["_source_index"] for item in selected)
        if state in failed_states:
            return None
        slots_left = limit - len(selected)
        projects_needed = max(0, min_projects - len(project_counts))
        if slots_left == 0:
            return [] if projects_needed == 0 and len(interest_counts) >= min_interests else None
        eligible = [
            item for item in remaining
            if type_counts[str(item["candidate_type"])] < quotas.get(str(item["candidate_type"]), 0)
            and artist_counts[normalized_name(item["artist"])] < max_per_artist
            and project_counts[normalized_name(item["project"])] < max_per_project
        ]

        def capacity(items: list[dict[str, Any]], field: str, counts: Counter[str], cap: int) -> int:
            available = Counter(normalized_name(item[field]) for item in items)
            return sum(min(count, cap - counts[key]) for key, count in available.items())

        # Necessary capacity bounds prune dead ends without changing greedy preference.
        feasible = (
            len(eligible) >= slots_left
            and projects_needed <= slots_left
            and len({normalized_name(item["project"]) for item in eligible} - set(project_counts)) >= projects_needed
            and capacity(eligible, "artist", artist_counts, max_per_artist) >= slots_left
            and capacity(eligible, "project", project_counts, max_per_project) >= slots_left
            and len(set(interest_counts) | {item["matched_interest_id"] for item in eligible if item["matched_interest_id"]}) >= min_interests
        )
        for kind, quota in quotas.items():
            items = [item for item in eligible if item["candidate_type"] == kind]
            needed = quota - type_counts[kind]
            if min(
                len(items), capacity(items, "artist", artist_counts, max_per_artist),
                capacity(items, "project", project_counts, max_per_project),
            ) < needed:
                feasible = False
        if not feasible:
            failed_states.add(state)
            return None
        options = []
        for candidate in eligible:
            project_marker = normalized_name(candidate.get("project"))
            if projects_needed >= slots_left and project_marker in project_counts:
                continue
            similarity = max((_candidate_similarity(candidate, chosen, packet) for chosen in selected), default=0.0)
            type_bonus = float(diversity["candidate_type_bonus"])
            project_bonus = float(diversity["new_project_bonus"]) if project_marker not in project_counts and len(project_counts) < min_projects else 0.0
            reservation_priority = 0
            if candidate["candidate_type"] == "musician_relation":
                artist_marker = normalized_name(candidate["artist"])
                project_marker_for_reservation = normalized_name(candidate["project"])
                if any(
                    item["candidate_type"] != "musician_relation"
                    and (
                        normalized_name(item["artist"]) == artist_marker
                        or normalized_name(item["project"]) == project_marker_for_reservation
                    )
                    for item in eligible
                ):
                    reservation_priority = 1
            adjusted = (
                float(candidate["ranking_score"])
                + type_bonus
                + project_bonus
                - similarity * float(diversity["mmr_penalty"])
            )
            if candidate["matched_interest_id"] and candidate["matched_interest_id"] not in interest_counts:
                adjusted += float(diversity.get("new_interest_bonus", 4.0))
            options.append((candidate, adjusted, reservation_priority))
        options.sort(key=lambda option: (-option[2], -option[1]))
        for chosen, adjusted, _reservation_priority in options:
            selected.append(chosen)
            keys = (
                (artist_counts, normalized_name(chosen["artist"])),
                (project_counts, normalized_name(chosen["project"])),
                (type_counts, str(chosen["candidate_type"])),
                (interest_counts, chosen["matched_interest_id"]),
            )
            for counts, key in keys:
                counts[key] += 1
            tail = search([item for item in eligible if item is not chosen])
            if tail is not None:
                return [(chosen, adjusted), *tail]
            selected.pop()
            for counts, key in keys:
                counts[key] -= 1
                if counts[key] == 0:
                    del counts[key]
        failed_states.add(state)
        return None

    solution = search(prepared)
    if solution is None:
        raise ContractError(f"candidate_pool 约束无解：无法同时满足 {limit} 首、召回配额、艺人/项目上限与项目覆盖")
    for index, (candidate, adjusted) in enumerate(solution, 1):
        candidate["selection_rank"] = index
        candidate["selection_adjusted_score"] = round(adjusted, 4)
    if type_counts != Counter(quotas):
        raise ContractError(f"候选类型配额未满足：实际 {dict(type_counts)}，要求 {quotas}")
    if len(project_counts) < min_projects:
        raise ContractError(f"候选项目覆盖不足：实际 {len(project_counts)}，要求 {min_projects}")
    for candidate in selected:
        candidate.pop("_source_index", None)
    sequenced, sequence_manifest = _sequence_candidates(selected, packet)
    for candidate in sequenced:
        candidate["program_explanation"] = explain_selected(candidate, packet)
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
        "selected_interest_counts": {str(key): value for key, value in interest_counts.items() if key is not None},
        "interest_group_count": len(interest_profiles(packet)),
        "selected_candidate_types": dict(type_counts),
        "selected_canonical_track_ids": [item["canonical_track_id"] for item in sequenced],
        "sequence": sequence_manifest,
    }
    return sequenced, manifest


def rank_bundle(bundle: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Turn a validated candidate-pool bundle into a ranked bundle."""

    validate_recommendation_bundle(bundle, packet)
    if packet.get("selection_mode") == "lastfm_constraints_v1":
        from lastfm_pipeline import select
        return select(bundle, packet)
    if bundle.get("status") != "ready":
        return bundle
    require_analysis_coverage(packet)
    if bundle.get("bundle_stage") not in {"candidate_pool", "ranked"}:
        raise ContractError("ready RecommendationBundle 必须从 candidate_pool 阶段开始")
    candidate_pool = bundle.get("candidate_pool")
    if not isinstance(candidate_pool, list) or not candidate_pool:
        raise ContractError("ready RecommendationBundle 必须包含候选池")
    limit = int(packet["recommendation_policy"]["target_recommendations"])
    recommendations, manifest = rank_candidates(candidate_pool, packet, limit=limit)
    if bundle["bundle_stage"] == "ranked":
        if bundle["recommendations"] != recommendations or bundle["ranking"] != manifest:
            raise ContractError("ranked 结果与确定性重算不一致：评分、选曲或顺序已变化，请从 candidate_pool 重新生成")
        return bundle
    ranked = deepcopy(bundle)
    ranked["bundle_stage"] = "ranked"
    ranked["publication_status"] = "draft"
    ranked["recommendations"] = recommendations
    ranked["ranking"] = manifest
    return ranked
