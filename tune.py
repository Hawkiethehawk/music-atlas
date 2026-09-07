#!/usr/bin/env python3
"""Human-approved policy-tuning loop.

``propose_tuning`` reads an offline evaluation report and the current policy,
then writes a *proposal* artifact describing suggested deltas for ranking
weights, recall mix, caps and sequence weights. The proposal is never applied
by this module: applying means a human edits a policy JSON and re-runs
``analyze`` with an explicit policy file. Unreviewed feedback therefore never
self-adjusts ranking policy.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from contracts import CANDIDATE_TYPES, RANKING_FEATURE_KEYS, utc_now
from feedback import ACCEPTED_OUTCOMES, REJECTED_OUTCOMES


def _acceptance_by_type(report: dict[str, Any], packet: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Measure acceptance only among recommendations with explicit feedback."""

    bundle = report.get("_bundle") or {}
    recommendations = bundle.get("recommendations", [])
    total = len(recommendations)
    if not total:
        return {}
    feedback = report.get("_feedback_by_track") or {}
    result: dict[str, dict[str, float]] = {}
    for candidate_type in sorted(CANDIDATE_TYPES):
        items = [item for item in recommendations if item.get("candidate_type") == candidate_type]
        outcomes = [feedback.get(str(item["canonical_track_id"]).casefold()) for item in items]
        accepted = sum(outcome in ACCEPTED_OUTCOMES for outcome in outcomes)
        rejected = sum(outcome in REJECTED_OUTCOMES for outcome in outcomes)
        covered = accepted + rejected
        result[candidate_type] = {
            "ranked_count": len(items),
            "covered_count": covered,
            "accepted_count": accepted,
            "rejected_count": rejected,
            "acceptance_rate": round(accepted / covered, 4) if covered else 0.0,
        }
    return result


def _acceptance_by_feature(report: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Mean feature scores of accepted vs rejected recommendations.

    The proposal only describes correlation; it never changes the policy.
    """

    bundle = report.get("_bundle") or {}
    recommendations = bundle.get("recommendations", [])
    accepted_by_id: set[str] = set()
    feedback = report.get("_feedback_by_track") or {}
    for item in recommendations:
        canonical_id = str(item.get("canonical_track_id") or "").casefold()
        if feedback.get(canonical_id) in ACCEPTED_OUTCOMES:
            accepted_by_id.add(canonical_id)
    summary: dict[str, dict[str, float]] = {}
    for feature in RANKING_FEATURE_KEYS:
        accepted_values: list[float] = []
        rejected_values: list[float] = []
        for item in recommendations:
            value = float(item.get("score_features", {}).get(feature) or 0.0)
            if str(item.get("canonical_track_id") or "").casefold() in accepted_by_id:
                accepted_values.append(value)
            elif feedback.get(str(item.get("canonical_track_id") or "").casefold()) in REJECTED_OUTCOMES:
                rejected_values.append(value)
        accepted_mean = sum(accepted_values) / len(accepted_values) if accepted_values else 0.0
        rejected_mean = sum(rejected_values) / len(rejected_values) if rejected_values else 0.0
        summary[feature] = {
            "accepted_count": len(accepted_values),
            "rejected_count": len(rejected_values),
            "accepted_mean": round(accepted_mean, 4),
            "rejected_mean": round(rejected_mean, 4),
            "delta": round(accepted_mean - rejected_mean, 4),
        }
    return summary


def _suggest_weight_deltas(feature_summary: dict[str, dict[str, float]]) -> dict[str, float]:
    """Small, bounded weight deltas based on accepted-vs-rejected deltas."""

    deltas: dict[str, float] = {}
    for feature, stats in feature_summary.items():
        if not stats["accepted_count"] or not stats["rejected_count"]:
            continue
        delta = stats["delta"] / 100.0
        deltas[feature] = round(max(-0.05, min(0.05, delta)), 4)
    return deltas


def _suggest_recall_mix_deltas(type_acceptance: dict[str, dict[str, float]]) -> dict[str, float]:
    """Suggest recall mix shifts only as *proposals*; applied by humans only."""

    deltas: dict[str, float] = {}
    covered = sum(stats["covered_count"] for stats in type_acceptance.values())
    accepted = sum(stats["accepted_count"] for stats in type_acceptance.values())
    if not covered or accepted in (0, covered):
        return deltas
    baseline = accepted / covered
    for candidate_type in sorted(CANDIDATE_TYPES):
        stats = type_acceptance.get(candidate_type, {})
        if stats.get("covered_count", 0):
            deltas[candidate_type] = round(max(-0.04, min(0.04, stats["acceptance_rate"] - baseline)), 4)
    return deltas


def _suggest_sequence_deltas(report: dict[str, Any]) -> dict[str, float]:
    sequence = report.get("sequence_quality", {})
    arc = float(sequence.get("arc_conformance") or 0.0)
    transition = float(sequence.get("mean_transition_distance") or 0.0)
    return {
        "transition_weight": round(max(-0.05, min(0.05, (50.0 - transition) / 1000.0)), 4),
        "arc_weight": round(max(-0.05, min(0.05, (arc - 0.7) / 10.0)), 4),
    }


def _suggest_cap_deltas(report: dict[str, Any]) -> dict[str, float]:
    repetition = report.get("repetition", {})
    max_artist = float(repetition.get("max_artist_share") or 0.0)
    max_project = float(repetition.get("max_project_share") or 0.0)
    policy = report.get("_policy", {})
    suggestions: dict[str, float] = {}
    if max_artist > 0.5:
        suggestions["max_per_artist"] = max(1, int(policy.get("max_per_artist") or 2) - 1)
    if max_project > 0.6:
        suggestions["max_per_project"] = max(1, int(policy.get("max_per_project") or 3) - 1)
    return suggestions


def propose_tuning(
    report: dict[str, Any],
    packet: dict[str, Any],
    *,
    bundle: dict[str, Any] | None = None,
    feedback_by_track: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a human-review proposal. Never applies changes."""

    policy = packet.get("recommendation_policy", {})
    report_with_context = dict(report)
    if bundle is not None:
        report_with_context["_bundle"] = bundle
    if feedback_by_track is not None:
        report_with_context["_feedback_by_track"] = feedback_by_track
    report_with_context["_policy"] = policy

    type_acceptance = _acceptance_by_type(report_with_context, packet)
    feature_summary = _acceptance_by_feature(report_with_context)
    accepted_count = sum(stats["accepted_count"] for stats in type_acceptance.values())
    rejected_count = sum(stats["rejected_count"] for stats in type_acceptance.values())
    enough_feedback = accepted_count > 0 and rejected_count > 0
    weight_deltas = _suggest_weight_deltas(feature_summary)
    recall_deltas = _suggest_recall_mix_deltas(type_acceptance)
    sequence_deltas = _suggest_sequence_deltas(report_with_context) if enough_feedback else {}
    cap_suggestions = _suggest_cap_deltas(report_with_context) if enough_feedback else {}

    return {
        "schema_version": "2.0",
        "artifact_type": "policy_tuning_proposal",
        "analysis_id": packet["analysis_id"],
        "generated_at": utc_now(),
        "approval_required": True,
        "auto_applied": False,
        "status": "review_required" if enough_feedback else "insufficient_feedback",
        "feedback_sample": {
            "accepted_count": accepted_count, "rejected_count": rejected_count,
            "minimum_per_group": 1,
            "note": "未反馈歌曲排除；最小样本门槛仅防止空对照，不代表统计显著性。",
        },
        "basis": {
            "evidence": "recorded user feedback, never inferred preferences",
            "requirement": "权重、召回组合、上限与排序权重必须由人工审阅后显式应用",
        },
        "current_policy": deepcopy({
            key: policy.get(key)
            for key in ("ranking_weights", "recall_mix", "max_per_artist", "max_per_project", "sequence_policy")
            if policy.get(key) is not None
        }),
        "observations": {
            "candidate_type_acceptance": type_acceptance,
            "feature_acceptance": feature_summary,
        },
        "suggested_deltas": {
            "ranking_weights": weight_deltas,
            "recall_mix": recall_deltas,
            "sequence_weights": sequence_deltas,
        },
        "suggested_caps": cap_suggestions,
        "how_to_apply": [
            "人工审阅建议；把增量转换为最终策略数值，保持各组权重与配额比例总和为 1；",
            "重新运行 analyze 时使用 --policy-file 显式加载；",
            "策略文件一经人工批准即可成为下一次运行的基础，未审阅反馈不会自动生效。",
        ],
    }
