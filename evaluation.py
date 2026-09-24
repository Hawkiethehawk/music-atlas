#!/usr/bin/env python3
"""Offline evaluation of a ranked bundle against recorded feedback.

Evaluation is read-only: it never changes the ranking policy. Precision,
novelty, diversity, calibration, artist/project repetition and sequence
quality are reported so a human can decide whether a policy tuning proposal
is worth approving.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from contracts import SCHEMA_VERSION, normalized_name, utc_now
from feedback import ACCEPTED_OUTCOMES, latest_feedback_outcomes, match_feedback_to_bundle
from recommender import rank_bundle


CALIBRATION_BINS = ((0.0, 70.0), (70.0, 80.0), (80.0, 90.0), (90.0, 100.0))


def _outcome_by_track(
    bundle: dict[str, Any],
    feedback_log: list[dict[str, Any]],
) -> dict[str, str]:
    """Latest feedback outcome per ranked canonical track id."""

    return latest_feedback_outcomes(bundle, feedback_log)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _precision_metrics(
    recommendations: list[dict[str, Any]],
    outcome_by_track: dict[str, str],
) -> dict[str, float]:
    total = len(recommendations)
    covered = sum(1 for item in recommendations if str(item["canonical_track_id"]).casefold() in outcome_by_track)
    accepted = sum(
        1
        for item in recommendations
        if outcome_by_track.get(str(item["canonical_track_id"]).casefold()) in ACCEPTED_OUTCOMES
    )
    return {
        "recommendation_count": total,
        "covered_by_feedback": covered,
        "feedback_coverage": round(covered / total, 4) if total else 0.0,
        "accepted_count": accepted,
        "precision": round(accepted / total, 4) if total else 0.0,
        "acceptance_rate": round(accepted / covered, 4) if covered else 0.0,
        "precision_semantics": "confirmed_accepts_over_all_recommendations_lower_bound",
        "acceptance_status": "observed" if covered else "unmeasured",
    }


def _novelty_metrics(
    recommendations: list[dict[str, Any]],
    packet: dict[str, Any],
    outcome_by_track: dict[str, str],
) -> dict[str, float]:
    favorite_artists = {
        normalized_name(item.get("artist"))
        for item in packet.get("primary_distribution", [])
        if isinstance(item, dict)
    }
    accepted = [
        item
        for item in recommendations
        if outcome_by_track.get(str(item["canonical_track_id"]).casefold()) in ACCEPTED_OUTCOMES
    ]
    novel_artist_count = sum(
        1 for item in accepted if normalized_name(item.get("artist")) not in favorite_artists
    )
    exploration_accepted = sum(
        1 for item in accepted if item.get("candidate_type") == "exploration"
    )
    novelty_scores = [
        float(item.get("score_features", {}).get("novelty") or 0.0) for item in accepted
    ]
    return {
        "accepted_count": len(accepted),
        "novel_artist_share": round(novel_artist_count / len(accepted), 4) if accepted else 0.0,
        "exploration_share": round(exploration_accepted / len(accepted), 4) if accepted else 0.0,
        "mean_novelty_feature": round(_mean(novelty_scores), 4) if novelty_scores else 0.0,
    }


def _diversity_metrics(recommendations: list[dict[str, Any]]) -> dict[str, float]:
    total = len(recommendations)
    if not total:
        return {
            "unique_artists": 0,
            "artist_diversity": 0.0,
            "unique_projects": 0,
            "project_diversity": 0.0,
            "unique_styles": 0,
            "style_diversity": 0.0,
            "candidate_type_coverage": 0.0,
        }
    artists = {normalized_name(item.get("artist")) for item in recommendations}
    projects = {normalized_name(item.get("project")) for item in recommendations}
    styles: set[str] = set()
    for item in recommendations:
        for ref in item.get("style_refs", []):
            if isinstance(ref, str) and ref:
                styles.add(ref)
    types = {str(item.get("candidate_type")) for item in recommendations if item.get("candidate_type")}
    return {
        "unique_artists": len(artists),
        "artist_diversity": round(len(artists) / total, 4),
        "unique_projects": len(projects),
        "project_diversity": round(len(projects) / total, 4),
        "unique_styles": len(styles),
        "style_diversity": round(len(styles) / total, 4),
        "candidate_type_coverage": round(len(types) / 4.0, 4),
    }


def _calibration_metrics(
    recommendations: list[dict[str, Any]],
    outcome_by_track: dict[str, str],
) -> dict[str, Any]:
    recommendations = [
        item for item in recommendations
        if str(item["canonical_track_id"]).casefold() in outcome_by_track
    ]
    bins: list[dict[str, Any]] = []
    for low, high in CALIBRATION_BINS:
        items = [
            item
            for item in recommendations
            if low <= float(item.get("ranking_score") or 0.0) < high
            or high == 100.0 and float(item.get("ranking_score") or 0.0) == high
        ]
        count = len(items)
        if not count:
            bins.append(
                {
                    "score_range": [low, high],
                    "count": 0,
                    "mean_score": 0.0,
                    "acceptance_rate": 0.0,
                    "calibration_error": None,
                }
            )
            continue
        mean_score = _mean([float(item.get("ranking_score") or 0.0) for item in items])
        accepted = sum(
            1
            for item in items
            if outcome_by_track.get(str(item["canonical_track_id"]).casefold()) in ACCEPTED_OUTCOMES
        )
        acceptance_rate = accepted / count
        bins.append(
            {
                "score_range": [low, high],
                "count": count,
                "mean_score": round(mean_score, 4),
                "acceptance_rate": round(acceptance_rate, 4),
                "calibration_error": None,
            }
        )
    return {"bins": bins, "expected_calibration_error": None, "status": "not_calibrated",
            "score_semantics": "heuristic_not_probability",
            "note": "规则分数不是喜欢概率；分箱仅展示已反馈样本，不计算概率校准误差。"}


def _repetition_metrics(recommendations: list[dict[str, Any]]) -> dict[str, float]:
    total = len(recommendations)
    if not total:
        return {"max_artist_share": 0.0, "max_project_share": 0.0, "repeated_share": 0.0}
    artist_counts = Counter(normalized_name(item.get("artist")) for item in recommendations)
    project_counts = Counter(normalized_name(item.get("project")) for item in recommendations)
    return {
        "max_artist_share": round(max(artist_counts.values()) / total, 4),
        "max_project_share": round(max(project_counts.values()) / total, 4),
        "repeated_share": round(
            1.0 - (len(artist_counts) + len(project_counts)) / (2.0 * total),
            4,
        ),
    }


def _sequence_quality_metrics(
    recommendations: list[dict[str, Any]],
    ranking: dict[str, Any],
) -> dict[str, float]:
    total = len(recommendations)
    if total <= 1:
        return {"mean_supported_tag_distance": None, "supported_transition_share": 0.0}
    positions = [entry for entry in ranking.get("sequence", {}).get("positions", [])
                 if isinstance(entry, dict) and entry.get("position", 0) > 1]
    transitions = [
        float(entry["transition_distance"]) for entry in positions
        if isinstance(entry.get("transition_distance"), (int, float))
    ]
    return {
        "mean_supported_tag_distance": round(_mean(transitions), 4) if transitions else None,
        "supported_transition_share": round(len(transitions) / (total - 1), 4),
    }


def evaluate_offline(
    bundle: dict[str, Any],
    packet: dict[str, Any],
    feedback_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate a ranked bundle against feedback. Never mutates ranking policy."""

    bundle = rank_bundle(bundle, packet)
    rectify = match_feedback_to_bundle(feedback_log, bundle, packet)
    recommendations = bundle.get("recommendations", [])
    outcome_by_track = rectify["latest_outcomes"]
    ranking = bundle.get("ranking", {})
    report = {
        "schema_version": SCHEMA_VERSION,
        "report_type": "offline_evaluation",
        "analysis_id": packet["analysis_id"],
        "bundle_id": bundle.get("bundle_id"),
        "generated_at": utc_now(),
        "feedback": {
            "record_count": len(feedback_log),
            "matched_count": rectify["matched_count"],
            "unmatched_count": len(rectify["unmatched"]),
            "unmatched_reasons": Counter(
                str(item.get("reason")) for item in rectify["unmatched"]
            ),
        },
        "precision": _precision_metrics(recommendations, outcome_by_track),
        "novelty": _novelty_metrics(recommendations, packet, outcome_by_track),
        "diversity": _diversity_metrics(recommendations),
        "calibration": _calibration_metrics(recommendations, outcome_by_track),
        "repetition": _repetition_metrics(recommendations),
        "sequence_quality": _sequence_quality_metrics(recommendations, ranking),
        "policy_changed": False,
    }
    return report
