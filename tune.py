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

from typing import Any

from contracts import CANDIDATE_TYPES, RANKING_FEATURE_KEYS, utc_now


def _acceptance_by_type(report: dict[str, Any], packet: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Estimate acceptance rate per candidate type from precision + bundle types."""

    bundle = report.get("_bundle") or {}
    recommendations = bundle.get("recommendations", [])
    total = len(recommendations)
    if not total:
        return {}
    type_counts: dict[str, int] = {}
    for item in recommendations:
        candidate_type = str(item.get("candidate_type") or "")
        type_counts[candidate_type] = type_counts.get(candidate_type, 0) + 1
    accepted = report.get("precision", {}).get("accepted_count", 0)
    result: dict[str, dict[str, float]] = {}
    for candidate_type in sorted(CANDIDATE_TYPES):
        count = type_counts.get(candidate_type, 0)
        result[candidate_type] = {
            "ranked_count": count,
            "share": round(count / total, 4) if total else 0.0,
        }
    result["_accepted_total"] = {"accepted_count": accepted}
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
        if feedback.get(canonical_id) in {"saved", "replayed"}:
            accepted_by_id.add(canonical_id)
    summary: dict[str, dict[str, float]] = {}
    for feature in RANKING_FEATURE_KEYS:
        accepted_values: list[float] = []
        rejected_values: list[float] = []
        for item in recommendations:
            value = float(item.get("score_features", {}).get(feature) or 0.0)
            if str(item.get("canonical_track_id") or "").casefold() in accepted_by_id:
                accepted_values.append(value)
            else:
                rejected_values.append(value)
        accepted_mean = sum(accepted_values) / len(accepted_values) if accepted_values else 0.0
        rejected_mean = sum(rejected_values) / len(rejected_values) if rejected_values else 0.0
        summary[feature] = {
            "accepted_mean": round(accepted_mean, 4),
            "rejected_mean": round(rejected_mean, 4),
            "delta": round(accepted_mean - rejected_mean, 4),
        }
    return summary


def _suggest_weight_deltas(feature_summary: dict[str, dict[str, float]]) -> dict[str, float]:
    """Small, bounded weight deltas based on accepted-vs-rejected deltas."""

    deltas: dict[str, float] = {}
    for feature, stats in feature_summary.items():
        delta = stats["delta"] / 100.0
        deltas[feature] = round(max(-0.05, min(0.05, delta)), 4)
    return deltas


def _suggest_recall_mix_deltas(type_acceptance: dict[str, dict[str, float]]) -> dict[str, float]:
    """Suggest recall mix shifts only as *proposals*; applied by humans only."""

    deltas: dict[str, float] = {}
    for candidate_type in sorted(CANDIDATE_TYPES):
        share = type_acceptance.get(candidate_type, {}).get("share", 0.0)
        deltas[candidate_type] = round(max(-0.04, min(0.04, (share - 0.25))), 4)
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
    weight_deltas = _suggest_weight_deltas(feature_summary)
    recall_deltas = _suggest_recall_mix_deltas(type_acceptance)
    sequence_deltas = _suggest_sequence_deltas(report_with_context)
    cap_suggestions = _suggest_cap_deltas(report_with_context)

    return {
        "schema_version": "2.0",
        "artifact_type": "policy_tuning_proposal",
        "analysis_id": packet["analysis_id"],
        "generated_at": utc_now(),
        "approval_required": True,
        "auto_applied": False,
        "basis": {
            "evidence": "recorded user feedback, never inferred preferences",
            "requirement": "权重、召回组合、上限与排序权重必须由人工审阅后显式应用",
        },
        "current_policy": {
            key: policy.get(key)
            for key in ("ranking_weights", "recall_mix", "max_per_artist", "max_per_project", "sequence_policy")
            if policy.get(key) is not None
        },
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
            "人工审阅建议；编辑策略 JSON；",
            "重新运行 analyze 时使用 --policy-file 显式加载；",
            "策略文件一经人工批准即可成为下一次运行的基础，未审阅反馈不会自动生效。",
        ],
    }