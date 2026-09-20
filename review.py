"""Fast, local review gates for Music Atlas recommendation runs.

The review stage deliberately performs no network requests and no Agent calls. It
only checks data already collected by the provider adapters and copy already
validated by the Agent stage. Its runtime is measured and capped at 10 seconds.
"""
from __future__ import annotations

import time
from typing import Any

from contracts import ContractError, track_key, utc_now

REVIEW_BUDGET_SECONDS = 10.0


def _source_record(candidate: dict[str, Any]) -> dict[str, Any] | None:
    value = candidate.get("metadata_verified")
    return value if isinstance(value, dict) else None


def _review_one(candidate: dict[str, Any], packet: dict[str, Any], seen_keys: set[str], seen_ids: set[str]) -> tuple[str, list[str], dict[str, Any]]:
    issues: list[str] = []
    gaps: list[str] = []
    fact = _source_record(candidate)
    if not fact or not fact.get("url") or fact.get("source") == "skipped":
        issues.append("missing_source_record")
    else:
        for field in ("title", "artist", "platform_track_id"):
            if str(candidate.get(field) or "") != str(fact.get(field) or ""):
                issues.append(f"identity_mismatch:{field}")
        fact_album = str(fact.get("album") or "").strip()
        candidate_album = str(candidate.get("project") or "").strip()
        if fact_album and candidate_album not in {fact_album, "未知专辑"}:
            issues.append("identity_mismatch:album")
        expected_id = f"platform:{fact.get('source')}:{fact.get('platform_track_id')}"
        if candidate.get("canonical_track_id") != expected_id:
            issues.append("canonical_track_id_mismatch")
        if str(fact.get("url") or "") not in [str(url) for url in candidate.get("sources", [])]:
            gaps.append("source_url_not_projected_to_sources")

    key = track_key(candidate.get("title"), candidate.get("artist"))
    platform_id = str(candidate.get("platform_track_id") or "")
    if key in seen_keys or (platform_id and platform_id in seen_ids):
        issues.append("duplicate_candidate")
    seen_keys.add(key)
    if platform_id:
        seen_ids.add(platform_id)

    exclusion = packet.get("playlist_exclusion") or {}
    if key in {str(value) for value in exclusion.get("track_keys", [])}:
        issues.append("playlist_track_excluded")
    if platform_id in {str(value) for value in exclusion.get("platform_track_ids", [])}:
        issues.append("playlist_platform_id_excluded")

    if candidate.get("candidate_type") == "musician_relation":
        relation = candidate.get("provider_relation") or {}
        if not relation.get("url") or not relation.get("person"):
            issues.append("missing_relation_source")
        elif relation.get("url") not in relation.get("sources", []):
            issues.append("relation_source_not_projected")
    style = candidate.get("style_evidence") or {}
    if not style.get("tags"):
        gaps.append("style_unknown")
    elif not style.get("url") or not style.get("retrieved_at"):
        issues.append("invalid_style_source")

    status = "rejected" if issues else "accepted" if not gaps else "accepted_with_gaps"
    return status, issues + gaps, {
        "canonical_track_id": candidate.get("canonical_track_id"),
        "title": candidate.get("title"),
        "artist": candidate.get("artist"),
        "status": status,
        "issues": issues,
        "gaps": gaps,
        "source_recorded": bool(fact and fact.get("url")),
        "identity_verified": not any(item.startswith("identity_") or item == "missing_source_record" for item in issues),
    }


def review_candidates(candidates: list[dict[str, Any]], packet: dict[str, Any], *, stage: str = "candidate_pool") -> dict[str, Any]:
    """Review one candidate pool in a bounded local-only pass."""
    started = time.perf_counter()
    if not isinstance(candidates, list):
        raise ContractError("复核输入候选池必须是列表")
    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    entries: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            entries.append({"status": "rejected", "issues": ["candidate_not_object"], "gaps": []})
            continue
        _status, _issues, entry = _review_one(candidate, packet, seen_keys, seen_ids)
        entries.append(entry)
    elapsed = time.perf_counter() - started
    if elapsed > REVIEW_BUDGET_SECONDS:
        raise ContractError(f"{stage} 本地复核超过 10 秒：{elapsed:.3f}s")
    rejected = sum(entry.get("status") == "rejected" for entry in entries)
    accepted = sum(entry.get("status") in {"accepted", "accepted_with_gaps"} for entry in entries)
    gaps = sum(bool(entry.get("gaps")) for entry in entries)
    return {
        "schema_version": "1.0", "artifact_type": "music_atlas_review", "stage": stage,
        "status": "rejected" if rejected else "completed_with_gaps" if gaps else "accepted",
        "reviewed_candidate_count": len(entries), "accepted_count": accepted,
        "rejected_count": rejected, "gap_count": gaps,
        "verification_scope": "local_source_ledger_only", "network_requests": 0, "agent_calls": 0,
        "budget_seconds": REVIEW_BUDGET_SECONDS, "elapsed_seconds": round(elapsed, 6),
        "elapsed_ms": round(elapsed * 1000, 3), "reviewed_at": utc_now(), "entries": entries,
    }


def review_groups(groups: list[dict[str, Any]], packet: dict[str, Any], *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Review all Atlas groups with the same local gate and group uniqueness."""
    started = time.perf_counter()
    reports = [review_candidates(group.get("recommendations", []), packet, stage=f"atlas_group_{index}") for index, group in enumerate(groups, 1)]
    ids: set[str] = set()
    duplicate_groups = 0
    for group in groups:
        for item in group.get("recommendations", []):
            cid = str(item.get("canonical_track_id") or "")
            if cid in ids:
                duplicate_groups += 1
            ids.add(cid)
    elapsed = time.perf_counter() - started
    if elapsed > REVIEW_BUDGET_SECONDS:
        raise ContractError(f"Atlas 组复核超过 10 秒：{elapsed:.3f}s")
    rejected = sum(int(report["rejected_count"]) for report in reports) + duplicate_groups
    gaps = sum(int(report["gap_count"]) for report in reports)
    result = {
        "schema_version": "1.0", "artifact_type": "music_atlas_review", "stage": "atlas_groups",
        "status": "rejected" if rejected else "completed_with_gaps" if gaps else "accepted",
        "group_count": len(groups), "group_reports": reports, "duplicate_across_groups": duplicate_groups,
        "reviewed_candidate_count": sum(int(report["reviewed_candidate_count"]) for report in reports),
        "accepted_count": sum(int(report["accepted_count"]) for report in reports),
        "rejected_count": rejected, "gap_count": gaps,
        "verification_scope": "local_source_ledger_only", "network_requests": 0, "agent_calls": 0,
        "budget_seconds": REVIEW_BUDGET_SECONDS, "elapsed_seconds": round(elapsed, 6),
        "elapsed_ms": round(elapsed * 1000, 3), "reviewed_at": utc_now(),
    }
    if base:
        result["candidate_pool_review"] = base
    return result
