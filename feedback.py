#!/usr/bin/env python3
"""Recommendation-feedback input contract and offline log access.

Feedback is recorded *after* a recommendation is sent. It is never used to
adjust the ranking policy at runtime: a ranked bundle is first compared
against recorded feedback (``evaluation.py``), and any policy update goes
through a human-approved tuning loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import (
    ContractError,
    SCHEMA_VERSION,
    FEEDBACK_OUTCOMES,
    read_json,
    stable_hash,
    utc_now,
    validate_feedback_log,
    validate_feedback_log_refs,
    write_json,
)


ACCEPTED_OUTCOMES = {"saved", "replayed"}
REJECTED_OUTCOMES = {"skipped", "hidden"}


def _timestamp_order_key(value: str) -> tuple[int, float, str]:
    """Keep legacy invalid timestamps below valid ones; naive times use UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (1, parsed.timestamp(), "")
    except (ValueError, OverflowError, OSError):
        return (0, 0.0, value)


def new_feedback_record(
    *,
    analysis_id: str,
    recommendation_id: str,
    outcome: str,
    bundle_id: str | None = None,
    position: int | None = None,
    note: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Create a feedback record for one ranked recommendation."""
    if outcome not in FEEDBACK_OUTCOMES:
        raise ContractError(f"outcome 必须是 {sorted(FEEDBACK_OUTCOMES)} 之一：{outcome}")
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "recommendation_feedback",
        "outcome": outcome,
        "timestamp": timestamp or utc_now(),
        "analysis_id": analysis_id,
        "recommendation_id": recommendation_id,
    }
    if bundle_id is not None:
        record["bundle_id"] = bundle_id
    if position is not None:
        record["position"] = position
    if note is not None:
        record["note"] = note
    return record


def load_feedback_log(path: Path) -> list[dict[str, Any]]:
    """Load and validate a feedback log JSON array."""
    value = read_json(path)
    return validate_feedback_log(value)


def save_feedback_log(log: list[dict[str, Any]], path: Path) -> None:
    """Validate and persist a feedback log."""
    validate_feedback_log(log)
    write_json(path, log)


def feedback_outcome_counts(log: list[dict[str, Any]]) -> dict[str, int]:
    """Count outcomes of valid feedback records."""
    validate_feedback_log(log)
    counts = {outcome: 0 for outcome in sorted(FEEDBACK_OUTCOMES)}
    for record in log:
        counts[record["outcome"]] += 1
    return counts


def match_feedback_to_bundle(
    log: list[dict[str, Any]],
    bundle: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, Any]:
    """Rectify feedback records against a ranked bundle.

    Returns the validated contact point used by evaluation: matched records
    keyed by canonical track id, plus a report of unmatched records. Ranking
    policy is not modified here.
    """

    validate_feedback_log(log)
    result = validate_feedback_log_refs(log, bundle, packet)
    matched = result["matched"]
    unmatched = result["unmatched"]
    by_recommendation: dict[str, list[dict[str, Any]]] = {}
    for record in matched:
        recommendation_id = str(record["recommendation_id"]).casefold()
        by_recommendation.setdefault(recommendation_id, []).append(record)
    return {
        "analysis_id": packet["analysis_id"],
        "bundle_id": bundle.get("bundle_id"),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "unmatched": unmatched,
        "by_recommendation": by_recommendation,
        "latest_outcomes": {
            key: max(records, key=lambda record: (
                _timestamp_order_key(record["timestamp"]), stable_hash(record),
            ))["outcome"]
            for key, records in by_recommendation.items()
        },
        "outcome_counts": feedback_outcome_counts(matched),
    }


def latest_feedback_outcomes(bundle: dict[str, Any], log: list[dict[str, Any]]) -> dict[str, str]:
    """Resolve only current-analysis, ranked-track feedback in time order."""
    return match_feedback_to_bundle(log, bundle, {"analysis_id": bundle["analysis_id"]})["latest_outcomes"]
