#!/usr/bin/env python3
"""Source-specific evidence verification and grade acceptance rules.

``verify_evidence_item`` classifies an evidence URL into a source class and,
when the host provides a stable public identifier (MusicBrainz UUID, Wikidata
QID, YouTube video id, Spotify track id), verifies the identifier pattern
offline. Network calls are never performed here; live checks are a reserved
extension behind an explicit flag. Provenance fields (retrieval time, source
identifier, verification result) are attached to each evidence item. The
A/B/C acceptance rules map claim type x source class to a required grade.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from contracts import EVIDENCE_CLAIM_TYPES, EVIDENCE_VERIFICATION_STATUSES


SOURCE_CLASSES = (
    "musicbrainz",
    "wikidata",
    "lastfm",
    "listenbrainz",
    "bandcamp",
    "youtube",
    "spotify",
    "official",
    "other",
)

_GRADE_ORDER = {"A": 0, "B": 1, "C": 2}

_MUSICBRAINZ_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_WIKIDATA_ID = re.compile(r"^Q\d+$", re.I)
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")


def classify_source(url: str) -> str:
    """Return the source class for a URL, or ``other``."""

    host = (urlparse(url).netloc or "").casefold()
    suffix = host[4:] if host.startswith("www.") else host
    if suffix == "musicbrainz.org":
        return "musicbrainz"
    if suffix == "wikidata.org":
        return "wikidata"
    if suffix == "last.fm":
        return "lastfm"
    if suffix == "listenbrainz.org":
        return "listenbrainz"
    if suffix.endswith("bandcamp.com"):
        return "bandcamp"
    if suffix in {"youtube.com", "youtu.be", "m.youtube.com"}:
        return "youtube"
    if suffix == "open.spotify.com":
        return "spotify"
    if suffix == "music.apple.com":
        return "official"
    return "other"


def extract_source_identifier(url: str) -> str | None:
    """Extract a stable public identifier from a supported URL, or None."""

    source_class = classify_source(url)
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if source_class == "musicbrainz":
        for segment in reversed(path.split("/")):
            if _MUSICBRAINZ_ID.match(segment):
                return f"musicbrainz:{segment.casefold()}"
        return None
    if source_class == "wikidata":
        for segment in reversed(path.split("/")):
            if _WIKIDATA_ID.match(segment):
                return f"wikidata:{segment.upper()}"
        return None
    if source_class == "youtube":
        query_id = _first_query(parsed.query, "v")
        if query_id and _YOUTUBE_ID.match(query_id):
            return f"youtube:{query_id}"
        if source_class == "youtube" and parsed.netloc.casefold() == "youtu.be":
            return f"youtube:{path.lstrip('/')}" if _YOUTUBE_ID.match(path.lstrip("/")) else None
        return None
    if source_class == "spotify":
        for segment in path.split("/"):
            if _SPOTIFY_ID.match(segment):
                return f"spotify:{segment}"
        return None
    return None


def _first_query(query: str, key: str) -> str:
    for part in query.split("&"):
        if part.startswith(key + "="):
            return part[len(key) + 1 :]
    return ""


def _is_stale(retrieved_at: str | None, *, max_age_days: int = 730) -> bool:
    if not retrieved_at:
        return False
    try:
        parsed = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    delta = now - parsed
    if delta.total_seconds() < 0:
        return False
    return delta.days > max_age_days


def verify_evidence_item(
    item: dict[str, Any],
    *,
    seen_urls: set[str] | None = None,
) -> dict[str, Any]:
    """Attach provenance fields and an offline verification result.

    An explicit ``verification_result`` supplied by a source-specific adapter
    (for example a live check reporting ``contradictory`` or
    ``inaccessible``) is preserved. Offline rules only fill the gap when no
    explicit result is present: duplicated URLs are flagged, stale retrieval
    times are flagged, otherwise a stable public identifier means verified
    and unknown pages stay unverified.
    """

    url = str(item.get("url") or "")
    source_class = classify_source(url)
    source_identifier = extract_source_identifier(url)
    duplicated = bool(seen_urls is not None and url in seen_urls)
    if seen_urls is not None:
        seen_urls.add(url)
    explicit = item.get("verification_result")
    if duplicated:
        result = "duplicated"
    elif isinstance(explicit, str) and explicit in EVIDENCE_VERIFICATION_STATUSES and explicit != "unverified":
        result = explicit
    elif _is_stale(item.get("retrieved_at")):
        result = "stale"
    elif source_identifier is not None:
        result = "verified"
    else:
        result = "unverified"
    verified_item = dict(item)
    verified_item["source_class"] = source_class
    if source_identifier is not None:
        verified_item["source_identifier"] = source_identifier
    verified_item["verification_result"] = result
    return verified_item


# Acceptance rules: claim_type -> source_class -> required grade. A claim is
# acceptable when every evidence item for that claim type reaches the required
# grade of at least one verified source class; the suggested bundle grade is
# the strictest claim-level requirement present.
GRADE_RULES: dict[str, dict[str, str]] = {
    "track_identity": {
        "musicbrainz": "A",
        "wikidata": "A",
        "official": "B",
        "spotify": "B",
        "youtube": "B",
        "bandcamp": "B",
        "listenbrainz": "B",
        "lastfm": "C",
        "other": "C",
    },
    "style": {
        "musicbrainz": "A",
        "wikidata": "B",
        "official": "B",
        "listenbrainz": "B",
        "lastfm": "B",
        "youtube": "C",
        "spotify": "C",
        "bandcamp": "C",
        "other": "C",
    },
    "relation": {
        "wikidata": "A",
        "musicbrainz": "A",
        "official": "A",
        "listenbrainz": "B",
        "lastfm": "B",
        "youtube": "C",
        "spotify": "C",
        "bandcamp": "C",
        "other": "C",
    },
    "release": {
        "musicbrainz": "A",
        "wikidata": "A",
        "bandcamp": "A",
        "official": "B",
        "spotify": "B",
        "youtube": "B",
        "listenbrainz": "B",
        "lastfm": "C",
        "other": "C",
    },
}


def _allowed_grade(item: dict[str, Any]) -> str:
    claim_type = str(item.get("claim_type") or "other")
    source_class = str(item.get("source_class") or "other")
    return GRADE_RULES.get(claim_type, {}).get(source_class, "C")


def claim_required_grade(claim_type: str, evidence_items: list[dict[str, Any]]) -> str:
    """Return the strongest grade a claim can honestly claim across its items."""

    items = [item for item in evidence_items if str(item.get("claim_type")) == claim_type]
    if not items:
        return "C"
    return min(
        (_allowed_grade(item) for item in items),
        key=lambda grade: _GRADE_ORDER[grade],
    )


def suggest_evidence_grade(evidence_items: list[dict[str, Any]]) -> str:
    """Suggest the strongest grade the whole evidence set can honestly claim."""

    if not evidence_items:
        return "C"
    return min(
        (_allowed_grade(item) for item in evidence_items),
        key=lambda grade: _GRADE_ORDER[grade],
    )


def check_evidence_acceptance(
    evidence_items: list[dict[str, Any]],
    submitted_grade: str,
) -> dict[str, Any]:
    """Check the submitted grade against the source-specific acceptance rules.

    Every evidence item carries a claim type and (after verification) a
    source class. The rules table states the strongest grade that source
    class can support for that claim type. A submission is accepted only when
    the declared grade is not stronger than what every verified item
    supports, so a low-grade source cannot be dressed up as high-grade
    evidence. The verdict is offline audit information; the bundle contract
    keeps the declared grade as the Agent's own statement.
    """

    submitted_order = _GRADE_ORDER.get(str(submitted_grade), 9)
    violations = [
        item
        for item in evidence_items
        if submitted_order < _GRADE_ORDER[_allowed_grade(item)]
    ]
    return {
        "accepted": not violations,
        "submitted_grade": submitted_grade,
        "suggested_grade": suggest_evidence_grade(evidence_items),
        "violating_items": [
            {
                "claim_type": item.get("claim_type"),
                "url": item.get("url"),
                "allowed_grade": _allowed_grade(item),
            }
            for item in violations
        ],
        "rule": "声明等级不得超过每条已验证来源按 claim_type × source_class 支持的最高等级",
        "note": "接受规则用于离线审计；契约仍保留 A/B/C 三档由 Agent 声明。",
    }


def verify_and_check_items(
    items: list[dict[str, Any]],
    submitted_grade: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Verify provenance for a list of items and return acceptance verdict."""

    seen: set[str] = set()
    verified_items: list[dict[str, Any]] = []
    for item in items:
        verified_items.append(verify_evidence_item(item, seen_urls=seen))
    verdict = check_evidence_acceptance(verified_items, submitted_grade)
    return verified_items, verdict


def audit_bundle_evidence(bundle: dict[str, Any]) -> dict[str, Any]:
    """Attach offline provenance and acceptance verdicts to one bundle.

    Used by ``workflow.py validate --evidence-audit`` to surface stale,
    inaccessible, duplicated or grade-inconsistent evidence without changing
    the ranking policy or the bundle itself.
    """

    entries: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for index, candidate in enumerate(bundle.get("recommendations", [])):
        items = candidate.get("evidence_items", [])
        verified_items: list[dict[str, Any]] = []
        for item in items:
            verified_items.append(verify_evidence_item(item, seen_urls=seen_urls))
        verdict = check_evidence_acceptance(verified_items, str(candidate.get("evidence_grade") or "C"))
        entries.append(
            {
                "position": index + 1,
                "canonical_track_id": candidate.get("canonical_track_id"),
                "title": candidate.get("title"),
                "artist": candidate.get("artist"),
                "evidence_grade": candidate.get("evidence_grade"),
                "acceptance": verdict,
                "evidence_items": verified_items,
            }
        )
    return {
        "schema_version": "2.0",
        "artifact_type": "evidence_audit",
        "recommendation_count": len(entries),
        "accepted_count": sum(1 for entry in entries if entry["acceptance"]["accepted"]),
        "rejected_count": sum(1 for entry in entries if not entry["acceptance"]["accepted"]),
        "entries": entries,
        "note": "离线确定性验证，不发网络请求；stale/duplicated/inaccessible 按证据出处字段判定。",
    }