"""Build the public, read-only view model consumed by the Music Atlas web UI.

The web layer intentionally does not consume the raw runtime artifacts.  This
module keeps the pipeline contracts authoritative and emits an allow-listed
payload whose shape is optimized for the editorial SPA.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from contracts import (
    ContractError,
    read_json,
    validate_analysis_packet,
    validate_playlist_snapshot,
    validate_recommendation_bundle,
    write_json,
)
from recommender import rank_bundle


WEB_SCHEMA_VERSION = "1.0"

WEB_AXES = (
    ("HEA", "重量感 Heaviness", "heaviness"),
    ("AGG", "攻击性 Aggression", "aggression"),
    ("ATM", "氛围 Atmosphere", "atmosphere"),
    ("ELE", "电子存在感 Electronic", "electronic_presence"),
    ("POP", "流行亲和度 Pop", "pop_accessibility"),
    ("RHY", "节奏密度 Rhythmic", "rhythmic_density"),
    ("VOC", "人声沙哑度 Vocal", "vocal_harshness"),
    ("EMO", "情绪强度 Emotional", "emotional_intensity"),
)

_TYPE_UI = {
    "style_neighbor": ("style", "风格邻近"),
    "artist_continuation": ("ext", "艺人延伸"),
    "musician_relation": ("rel", "音乐人关系"),
    "exploration": ("exp", "探索推荐"),
}

_HUES = (14, 268, 166, 196, 38, 320, 238, 178, 278, 352)


def _text(value: Any, default: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _short_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", _text(value))
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _year(value: Any) -> int | str:
    match = re.match(r"^(\d{4})", _text(value))
    return int(match.group(1)) if match else "—"


def _date(value: Any) -> str:
    text = _text(value)
    return text[:10] if text else "—"


def _hue(seed: str, fallback_index: int = 0) -> int:
    if not seed:
        return _HUES[fallback_index % len(_HUES)]
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % 360


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = _text(item)
        marker = value.casefold()
        if value and marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _axes(value: Any) -> dict[str, float | None]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, float | None] = {}
    for code, _label, key in WEB_AXES:
        raw = source.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            result[code] = None
        else:
            result[code] = round(float(raw), 1)
    return result


def _style_definitions(style_analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    for item in style_analysis.get("style_definitions", []):
        if isinstance(item, dict) and _text(item.get("style_ref")):
            definitions[_text(item["style_ref"])] = item
    return definitions


def _style_labels(
    style_mix: Any,
    definitions: dict[str, dict[str, Any]],
    limit: int = 3,
) -> list[str]:
    if not isinstance(style_mix, list):
        return []
    entries = [item for item in style_mix if isinstance(item, dict)]
    entries.sort(key=lambda item: float(item.get("weight", 0) or 0), reverse=True)
    labels: list[str] = []
    for item in entries:
        ref = _text(item.get("style_ref"))
        definition = definitions.get(ref, {})
        labels.append(_text(definition.get("label"), ref))
        if len(labels) >= limit:
            break
    return _unique(labels)


def _editorial_interests(editorial: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = editorial.get("interests", {})
    if raw in (None, {}):
        return {}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    if isinstance(raw, list):
        return {
            _text(item.get("id")): item
            for item in raw
            if isinstance(item, dict) and _text(item.get("id"))
        }
    raise ContractError("网页 editorial.interests 必须是对象或数组")


def _interest_entries(
    analysis: dict[str, Any],
    editorial: dict[str, Any],
) -> list[dict[str, Any]]:
    style_analysis = analysis.get("style_analysis", {})
    definitions = _style_definitions(style_analysis)
    editorial_by_id = _editorial_interests(editorial)
    result: list[dict[str, Any]] = []
    for index, profile in enumerate(style_analysis.get("interest_profiles", [])):
        if not isinstance(profile, dict):
            continue
        interest_id = _text(profile.get("interest_id"), f"interest-{index + 1:02d}")
        custom = editorial_by_id.get(interest_id, {})
        code = _text(custom.get("code"), f"{index + 1:02d}")
        name = _text(custom.get("name"), f"兴趣组 {code}")
        representatives = [
            item for item in profile.get("representative_tracks", []) if isinstance(item, dict)
        ]
        artists = _unique([_text(item.get("artist")) for item in representatives])[:3]
        rep_tracks = [
            f"{_text(item.get('title'))} — {_text(item.get('artist'))}".strip(" —")
            for item in representatives
            if _text(item.get("title")) or _text(item.get("artist"))
        ]
        genres = _style_labels(profile.get("style_mix"), definitions)
        summary = _text(
            custom.get("summary"),
            f"本兴趣组由本次歌单中 {int(profile.get('track_count', 0) or 0)} 首已分类曲目聚合而成；"
            "八轴为描述性研究估计。",
        )
        result.append(
            {
                "id": interest_id,
                "code": code,
                "name": name,
                "hue": int(custom.get("hue", _HUES[index % len(_HUES)])),
                "genres": custom.get("genres") if isinstance(custom.get("genres"), list) else genres,
                "artists": custom.get("artists") if isinstance(custom.get("artists"), list) else artists,
                "repTracks": custom.get("repTracks") if isinstance(custom.get("repTracks"), list) else rep_tracks,
                "summary": summary,
                "axes": _axes(profile.get("style_axes")),
                "trackCount": int(profile.get("track_count", 0) or 0),
                "share": profile.get("share"),
                "styleMix": profile.get("style_mix", []),
            }
        )
    return result


def _audit_index(evidence_audit: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(evidence_audit, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for entry in evidence_audit.get("entries", []):
        if isinstance(entry, dict) and _text(entry.get("canonical_track_id")):
            result[_text(entry["canonical_track_id"]).casefold()] = entry
    return result


def _provider(url: Any) -> str:
    try:
        hostname = urlparse(_text(url)).hostname
    except ValueError:
        hostname = None
    return hostname or "公开来源"


def _evidence_cards(
    recommendation: dict[str, Any],
    audit_entry: dict[str, Any] | None,
    audit_status: str,
) -> list[dict[str, Any]]:
    audit_items = {}
    if isinstance(audit_entry, dict):
        audit_items = {
            _text(item.get("url")): item
            for item in audit_entry.get("evidence_items", [])
            if isinstance(item, dict) and _text(item.get("url"))
        }
    result: list[dict[str, Any]] = []
    for item in recommendation.get("evidence_items", []):
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url"))
        audit_item = audit_items.get(url, {})
        verification = _text(audit_item.get("verification_result"), _text(item.get("verification_result")))
        if verification == "verified":
            status = "confirmed"
        elif audit_status == "pending_verification" or verification not in {"verified", ""}:
            status = "pending"
        else:
            status = "has-source"
        result.append(
            {
                "kind": _text(item.get("claim_type"), "SOURCE").upper(),
                "provider": _provider(url),
                "detail": _text(item.get("claim"), "公开来源条目"),
                "grade": _text(recommendation.get("evidence_grade"), "—"),
                "status": status,
                "retrieved": _date(item.get("retrieved_at")),
                "url": url,
                "verification": verification or "unverified",
            }
        )
    return result


def _route(
    recommendation: dict[str, Any],
    interest_by_id: dict[str, dict[str, Any]],
) -> list[list[str]]:
    route: list[list[str]] = []
    interest_id = _text(recommendation.get("matched_interest_id"))
    interest = interest_by_id.get(interest_id)
    if interest:
        route.append(["int", f"Interest {interest['code']} · {interest['name']}", "your orbit"])
    path = recommendation.get("relation_path")
    if not isinstance(path, list) or not path:
        resolved = recommendation.get("resolved_route")
        path = resolved.get("path", []) if isinstance(resolved, dict) else []
    path = [_text(item) for item in path if _text(item)]
    for index, item in enumerate(path):
        if index == len(path) - 1:
            kind, detail = "track", "result"
        elif index == 0:
            kind, detail = "artist", "anchor"
        else:
            kind, detail = "move", "route"
        route.append([kind, item, detail])
    if not route:
        route.append(["track", f"{_text(recommendation.get('title'))} — {_text(recommendation.get('artist'))}", "result"])
    return route


def _platform_links(value: Any) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    aliases = {
        "apple": ("apple", "apple_music"),
        "netease": ("netease", "netease_music"),
        "qq": ("qq", "qq_music"),
    }
    result: dict[str, str] = {}
    for target, keys in aliases.items():
        for key in keys:
            candidate = _text(source.get(key))
            if candidate:
                result[target] = candidate
                break
    return result


def _recommendation_entries(
    bundle: dict[str, Any],
    interests: list[dict[str, Any]],
    evidence_audit: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    interest_by_id = {item["id"]: item for item in interests}
    audit_status = _text((evidence_audit or {}).get("status"), "not_available")
    audit_by_id = _audit_index(evidence_audit)
    result: list[dict[str, Any]] = []
    for index, item in enumerate(bundle.get("recommendations", [])):
        if not isinstance(item, dict):
            continue
        candidate_type = _text(item.get("candidate_type"), "exploration")
        short_type, type_label = _TYPE_UI.get(candidate_type, ("exp", "探索推荐"))
        explanation = item.get("program_explanation") if isinstance(item.get("program_explanation"), dict) else {}
        canonical_id = _text(item.get("canonical_track_id"), f"recommendation-{index + 1:02d}")
        interest_id = _text(item.get("matched_interest_id")) or None
        interest = interest_by_id.get(interest_id or "")
        interest_label = (
            f"Interest {interest['code']} · {interest['name']}"
            if interest
            else "本期偏好画像"
        )
        why = _text(explanation.get("text"), "本条推荐说明未提供。")
        title = _text(item.get("title"), "未命名曲目")
        artist = _text(item.get("artist"), "未知艺人")
        project = _text(item.get("project"), "—")
        recommendation = {
            "id": canonical_id,
            "rank": int(item.get("sequence_position") or item.get("selection_rank") or index + 1),
            "track": title,
            "artist": artist,
            "album": project,
            "year": _year(item.get("release_date")),
            "country": "",
            "type": short_type,
            "typeLabel": type_label,
            "candidateType": candidate_type,
            "interest": interest_id,
            "interestLabel": interest_label,
            "hue": interest.get("hue") if interest else _hue(canonical_id, index),
            "oneLiner": _short_text(why),
            "why": why,
            "taste": [
                interest_label,
                _text(explanation.get("preference_basis"), "当前收藏偏好"),
                _text(explanation.get("music_fit"), _text(explanation.get("style_fit"), "描述性画像匹配")),
                f"{artist} — {title}",
            ],
            "route": _route(item, interest_by_id),
            "evidence": _evidence_cards(
                item,
                audit_by_id.get(canonical_id.casefold()),
                audit_status,
            ),
            "axis": _axes(item.get("style_axes")),
            "platformLinks": _platform_links(item.get("platform_links")),
            "researchLinks": [
                _text(url) for url in item.get("sources", []) if _text(url)
            ],
            "score": item.get("ranking_score"),
            "sequenceEnergy": item.get("sequence_energy"),
        }
        result.append(recommendation)
    result.sort(key=lambda item: int(item["rank"]))
    return result


def _relations(entity: dict[str, Any]) -> list[list[str]]:
    result: list[list[str]] = []
    raw_items = entity.get("related_projects", [])
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        return result
    relation_status = _text(entity.get("relation_status"), "researched")
    for item in raw_items:
        if isinstance(item, str):
            target, relation, note = item, "相关项目", relation_status
        elif isinstance(item, dict):
            target = _text(item.get("name"), _text(item.get("artist"), _text(item.get("project"))))
            relation = _text(item.get("relation"), _text(item.get("type"), "相关项目"))
            note = _text(item.get("note"), relation_status)
        else:
            continue
        if target:
            result.append([target, relation, note])
    return result


def _artist_entries(
    analysis: dict[str, Any],
    recommendations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    style_analysis = analysis.get("style_analysis", {})
    definitions = _style_definitions(style_analysis)
    entities = {
        _text(item.get("entity_ref")): item
        for item in analysis.get("entities", [])
        if isinstance(item, dict) and _text(item.get("entity_ref"))
    }
    result: dict[str, dict[str, Any]] = {}
    for profile in style_analysis.get("artist_profiles", []):
        if not isinstance(profile, dict):
            continue
        artist = _text(profile.get("artist"))
        if not artist:
            continue
        entity = entities.get(_text(profile.get("entity_ref")), {})
        result[artist] = {
            "origin": "",
            "years": "",
            "genres": _style_labels(profile.get("style_mix"), definitions),
            "bio": _text(profile.get("summary"), "本期研究未提供艺人简介。"),
            "relations": _relations(entity),
            "axis": _axes(profile.get("style_axes")),
            "sources": [_text(url) for url in profile.get("sources", []) if _text(url)],
            "boundaries": [
                _text(boundary) for boundary in profile.get("boundaries", []) if _text(boundary)
            ],
            "primaryTrackCount": profile.get("primary_track_count", 0),
            "creditedTrackCount": profile.get("credited_track_count", 0),
        }
    for recommendation in recommendations:
        artist = _text(recommendation.get("artist"))
        if artist in result:
            continue
        result[artist] = {
            "origin": "",
            "years": "",
            "genres": [],
            "bio": "本期候选仅提供曲目级研究画像，尚未建立完整艺人档案。",
            "relations": [],
            "axis": recommendation.get("axis", {}),
            "sources": recommendation.get("researchLinks", []),
            "boundaries": ["本页仅代表本期候选研究，不代表艺人全部作品。"],
            "primaryTrackCount": 0,
            "creditedTrackCount": 0,
        }
    return result


def _release_entries(
    snapshot: dict[str, Any],
    recommendations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def add(project: Any, artist: Any, title: Any, year: Any) -> None:
        name = _text(project)
        if not name:
            return
        entry = result.setdefault(
            name,
            {
                "name": name,
                "artist": _text(artist),
                "year": year if year not in (None, "") else "—",
                "label": "—",
                "blurb": "本期输入或推荐中出现的发行项目；当前契约未提供更完整的发行说明。",
                "tracks": [],
            },
        )
        if not entry.get("artist"):
            entry["artist"] = _text(artist)
        if entry.get("year") in (None, "—") and year not in (None, ""):
            entry["year"] = year
        track = _text(title)
        if track and track not in entry["tracks"]:
            entry["tracks"].append(track)

    for item in snapshot.get("tracks", []):
        if isinstance(item, dict):
            add(item.get("album"), item.get("artist"), item.get("title"), "—")
    for item in recommendations:
        add(item.get("album"), item.get("artist"), item.get("track"), item.get("year"))
    return result


def _source_entries(
    snapshot: dict[str, Any],
    analysis: dict[str, Any],
    bundle: dict[str, Any],
    evidence_audit: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    style_analysis = analysis.get("style_analysis", {})
    coverage = style_analysis.get("profile_coverage", {})
    source_count = int(snapshot.get("track_count", 0) or 0)
    declared_count = int(snapshot.get("declared_track_count", source_count) or source_count)
    classified = int(style_analysis.get("classified_track_count", 0) or 0)
    audit_status = _text((evidence_audit or {}).get("status"), "not_available")
    audit_accepted = int((evidence_audit or {}).get("accepted_count", 0) or 0)
    audit_pending = int((evidence_audit or {}).get("pending_count", 0) or 0)
    reader = snapshot.get("reader") if isinstance(snapshot.get("reader"), dict) else {}
    completeness_status = _text(reader.get("completeness_status"))
    return [
        {
            "id": "snapshot",
            "name": _text(snapshot.get("playlist_name"), "输入歌单"),
            "kind": "PlaylistSnapshot",
            "via": _text(reader.get("type"), _text(snapshot.get("platform"))),
            "tracks": source_count,
            "coverage": f"{source_count}/{declared_count}",
            "updated": _date(snapshot.get("captured_at")),
            "status": completeness_status or _text(snapshot.get("reader_status"), "unknown"),
        },
        {
            "id": "analysis",
            "name": "音乐人与风格研究",
            "kind": "Step 2",
            "via": _text(style_analysis.get("profile_catalog_mode"), "analysis"),
            "tracks": source_count,
            "coverage": f"{classified}/{source_count}",
            "updated": _date(analysis.get("generated_at")),
            "status": "degraded" if coverage.get("degraded") else "complete",
        },
        {
            "id": "recommendation",
            "name": "Atlas 推荐",
            "kind": "Step 3",
            "via": _text(bundle.get("bundle_stage"), "recommendation"),
            "tracks": len(bundle.get("recommendations", [])),
            "coverage": _text(bundle.get("status"), "unknown"),
            "updated": _date(bundle.get("generated_at")),
            "status": _text(bundle.get("publication_status"), _text(bundle.get("status"), "unknown")),
        },
        {
            "id": "evidence",
            "name": "证据审计",
            "kind": "Evidence audit",
            "via": _text((evidence_audit or {}).get("verification_scope"), "offline"),
            "tracks": len(bundle.get("recommendations", [])),
            "coverage": f"accepted {audit_accepted} / pending {audit_pending}",
            "updated": "—",
            "status": audit_status,
        },
    ]


def build_web_payload(
    snapshot: dict[str, Any],
    analysis: dict[str, Any],
    bundle: dict[str, Any],
    evidence_audit: dict[str, Any] | None = None,
    editorial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert validated pipeline artifacts into the SPA's allow-listed model."""

    editorial = editorial or {}
    ranked_bundle = rank_bundle(bundle, analysis)
    interests = _interest_entries(analysis, editorial)
    recommendations = _recommendation_entries(ranked_bundle, interests, evidence_audit)
    style_analysis = analysis.get("style_analysis", {})
    coverage = style_analysis.get("profile_coverage", {})
    audit_status = _text((evidence_audit or {}).get("status"), "not_available")
    source_count = int(snapshot.get("track_count", 0) or 0)
    declared_count = int(snapshot.get("declared_track_count", source_count) or source_count)
    custom_lede = _text(editorial.get("lede"))
    raw_reader = snapshot.get("reader") if isinstance(snapshot.get("reader"), dict) else {}
    reader = {
        "type": _text(raw_reader.get("type")),
        "declaredCountSource": _short_text(raw_reader.get("declared_count_source"), 80),
        "completenessStatus": _short_text(raw_reader.get("completeness_status"), 40),
    }
    reader = {key: value for key, value in reader.items() if value}
    lede = custom_lede or (
        f"基于 {source_count} 首完整歌单快照，沿着风格、听感、关系与探索路径，"
        f"整理 {len(recommendations)} 个本期 Atlas 方向。"
    )
    snapshot_status = _text(raw_reader.get("completeness_status"), _text(snapshot.get("reader_status"), "unknown"))
    payload = {
        "schema_version": WEB_SCHEMA_VERSION,
        "payload_type": "music_atlas_web",
        "generated_at": _text(
            ranked_bundle.get("generated_at"),
            _text(analysis.get("generated_at"), _text(snapshot.get("captured_at"))),
        ),
        "axes": [[code, label] for code, label, _key in WEB_AXES],
        "issue": {
            "title": _text(editorial.get("title"), "本期 Atlas"),
            "lede": lede,
        },
        "status": {
            "snapshot": snapshot_status,
            "analysis": "degraded" if coverage.get("degraded") else "complete",
            "recommendation": _text(ranked_bundle.get("status"), "unknown"),
            "publication": _text(ranked_bundle.get("publication_status"), "not_applicable"),
            "evidence_audit": audit_status,
            "profile_coverage_degraded": bool(coverage.get("degraded")),
        },
        "source": {
            "snapshotId": _text(snapshot.get("snapshot_id")),
            "platform": _text(snapshot.get("platform")),
            "playlistId": _text(snapshot.get("playlist_id")),
            "playlistName": _text(snapshot.get("playlist_name")),
            "declaredTrackCount": declared_count,
            "trackCount": source_count,
            "capturedAt": _text(snapshot.get("captured_at")),
            "inputSha256": _text(snapshot.get("input_sha256")),
            "reader": reader,
        },
        "analysis": {
            "analysisId": _text(analysis.get("analysis_id")),
            "asOfDate": _text(analysis.get("as_of_date")),
            "generatedAt": _text(analysis.get("generated_at")),
            "sourceTrackCount": int(analysis.get("source_track_count", 0) or 0),
            "classifiedTrackCount": int(style_analysis.get("classified_track_count", 0) or 0),
            "unclassifiedTrackCount": int(style_analysis.get("unclassified_track_count", 0) or 0),
            "profileCoverage": coverage,
        },
        "audit": {
            "status": audit_status,
            "acceptedCount": int((evidence_audit or {}).get("accepted_count", 0) or 0),
            "pendingCount": int((evidence_audit or {}).get("pending_count", 0) or 0),
            "rejectedCount": int((evidence_audit or {}).get("rejected_count", 0) or 0),
        },
        "interests": interests,
        "recommendations": recommendations,
        "artists": _artist_entries(analysis, recommendations),
        "albums": _release_entries(snapshot, recommendations),
        "sources": _source_entries(snapshot, analysis, ranked_bundle, evidence_audit),
    }
    return payload


def export_web_payload(
    runtime_dir: Path,
    output_path: Path,
    *,
    snapshot_path: Path | None = None,
    analysis_path: Path | None = None,
    bundle_path: Path | None = None,
    evidence_audit_path: Path | None = None,
    editorial_path: Path | None = None,
    require_publishable: bool = False,
) -> dict[str, Any]:
    """Validate one runtime and write an atomic web payload."""

    runtime_dir = runtime_dir.resolve()
    snapshot_path = (snapshot_path or runtime_dir / "snapshot.json").resolve()
    analysis_path = (analysis_path or runtime_dir / "musician_analysis.json").resolve()
    bundle_path = (bundle_path or runtime_dir / "recommendation_bundle.json").resolve()
    evidence_audit_path = (
        evidence_audit_path or runtime_dir / "evidence_audit.json"
    ).resolve()
    editorial_path = editorial_path.resolve() if editorial_path is not None else None
    output_path = output_path.resolve()
    input_paths = [snapshot_path, analysis_path, bundle_path, evidence_audit_path]
    if editorial_path is not None:
        input_paths.append(editorial_path)
    if output_path in input_paths:
        raise ContractError("网页视图输出不能覆盖原始运行产物")
    for path in (snapshot_path, analysis_path, bundle_path):
        if not path.is_file():
            raise ContractError(f"找不到网页导出输入：{path}")

    snapshot = validate_playlist_snapshot(read_json(snapshot_path), require_complete=True)
    analysis = validate_analysis_packet(read_json(analysis_path))
    bundle = read_json(bundle_path)
    bundle = rank_bundle(bundle, analysis)
    validate_recommendation_bundle(bundle, analysis)

    evidence_audit = None
    if evidence_audit_path.is_file():
        evidence_audit = read_json(evidence_audit_path)
        if not isinstance(evidence_audit, dict):
            raise ContractError("evidence_audit 必须是对象")

    if require_publishable:
        publication = _text(bundle.get("publication_status"))
        audit_status = _text((evidence_audit or {}).get("status"))
        if publication not in {"published", "approved", "accepted"} or audit_status != "accepted":
            raise ContractError(
                "当前运行不是可发布状态：需要 publication_status 为 published/approved/accepted，"
                "且 evidence_audit.status=accepted"
            )

    editorial = None
    if editorial_path is not None:
        if not editorial_path.is_file():
            raise ContractError(f"找不到网页 editorial 配置：{editorial_path}")
        editorial = read_json(editorial_path)
        if not isinstance(editorial, dict):
            raise ContractError("网页 editorial 配置必须是对象")

    payload = build_web_payload(snapshot, analysis, bundle, evidence_audit, editorial)
    write_json(output_path, payload)
    return {
        "status": "web_payload_written",
        "payload_path": str(output_path),
        "payload_schema_version": WEB_SCHEMA_VERSION,
        "snapshot_id": snapshot["snapshot_id"],
        "analysis_id": analysis["analysis_id"],
        "source_track_count": snapshot["track_count"],
        "recommendation_count": len(payload["recommendations"]),
        "interest_count": len(payload["interests"]),
        "publication_status": payload["status"]["publication"],
        "evidence_audit_status": payload["status"]["evidence_audit"],
    }
