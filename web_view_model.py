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


def _playlist_display_name(value: Any) -> str:
    """把平台默认英文歌单名转成中文显示，原始快照仍保留平台返回值。"""
    name = _text(value, "输入歌单")
    defaults = {"favorite songs", "favorites", "my favorite songs"}
    return "喜欢的歌曲" if name.casefold() in defaults else name


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


INTEREST_NAME_MIN_CHARS = 3
INTEREST_NAME_MAX_CHARS = 5


def _short_style_label(label: Any) -> str:
    """把 taxonomy 标签裁成可用作兴趣组名的中文词。

    去掉英文与分隔符号（`渐进/ djent 金属核` → `渐进金属核`）；超长时保留
    结尾的核心词（`现代另类金属核` → `另类金属核`）。
    """

    text = re.sub(r"[A-Za-z]+", "", _text(label))
    text = re.sub(r"[\s/\\|·、,，&+—–-]+", "", text)
    if len(text) > INTEREST_NAME_MAX_CHARS:
        text = text[-INTEREST_NAME_MAX_CHARS:]
    return text


def _derive_interest_name(
    profile: dict[str, Any],
    definitions: dict[str, dict[str, Any]],
    used: set[str],
) -> str:
    """按该组风格构成总结出 3-5 字名字；素材不足时返回空串。

    名字由程序从本次歌单实际聚合出的主导风格派生，与聚类同层，不再依赖
    可选的人工 editorial 配置；长度不符合或与已用名字重复时依次尝试下一个
    风格。
    """

    entries = [item for item in profile.get("style_mix", []) if isinstance(item, dict)]
    entries.sort(key=lambda item: float(item.get("weight", 0) or 0), reverse=True)
    for entry in entries:
        definition = definitions.get(_text(entry.get("style_ref"))) or {}
        label = _short_style_label(definition.get("label"))
        if INTEREST_NAME_MIN_CHARS <= len(label) <= INTEREST_NAME_MAX_CHARS and label not in used:
            return label
    return ""


def _editorial_names(editorial_by_id: dict[str, dict[str, Any]]) -> set[str]:
    """人工 editorial 提供的名字先占位，避免自动名字与之撞车。"""

    return {_text(item.get("name")) for item in editorial_by_id.values() if _text(item.get("name"))}


def _interest_entries(
    analysis: dict[str, Any],
    editorial: dict[str, Any],
) -> list[dict[str, Any]]:
    style_analysis = analysis.get("style_analysis", {})
    definitions = _style_definitions(style_analysis)
    editorial_by_id = _editorial_interests(editorial)
    used_names = _editorial_names(editorial_by_id)
    result: list[dict[str, Any]] = []
    for index, profile in enumerate(style_analysis.get("interest_profiles", [])):
        if not isinstance(profile, dict):
            continue
        interest_id = _text(profile.get("interest_id"), f"interest-{index + 1:02d}")
        custom = editorial_by_id.get(interest_id, {})
        code = _text(custom.get("code"), f"{index + 1:02d}")
        name = _text(custom.get("name"))
        if not name:
            name = _derive_interest_name(profile, definitions, used_names) or f"兴趣组 {code}"
        used_names.add(name)
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
            "风格归纳仅依据可核对的公开资料。",
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
    if hostname and (hostname == "last.fm" or hostname.endswith(".last.fm")):
        return "风格资料"
    return hostname or "公开来源"


_PUBLIC_STATUS_LABELS = {
    "complete": "已完成", "completed": "已完成", "confirmed": "已确认",
    "running": "运行中", "queued": "排队中", "failed": "失败",
    "degraded": "覆盖不足", "ready": "已生成", "draft": "研究草稿",
    "pending": "待处理", "pending_verification": "待独立核验",
    "completed_with_gaps": "已完成（有资料缺口）", "accepted": "已通过",
    "rejected": "未通过", "unconfirmed": "待确认", "incomplete": "不完整",
    "not_available": "不可用", "unknown": "未知", "insufficient_evidence": "证据不足",
    "offline": "本地核验", "analysis": "程序分析", "recommendation": "推荐生成",
    "research": "公开资料研究", "lastfm": "音乐资料",
}

def _public_status(value: Any, fallback: str = "未知") -> str:
    text = _text(value, fallback)
    return _PUBLIC_STATUS_LABELS.get(text.casefold(), text)


def _public_copy(value: Any) -> str:
    text = re.sub(r"last[\s.\-]*fm\s*相似艺人", "相似艺人", _text(value), flags=re.I)
    text = re.sub(r"last[\s.\-]*fm", "音乐资料", text, flags=re.I)
    text = re.sub(r"\bAgent\b", "智能助手", text, flags=re.I)
    return text


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
                "detail": _public_copy(item.get("claim")) or "公开来源条目",
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
    path = [_public_copy(item) for item in path if _text(item)]
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
            else "当前偏好画像"
        )
        why = _public_copy(explanation.get("text")) or "本条推荐说明未提供。"
        title = _text(item.get("title"), "未命名曲目")
        # 平台数据可能把歌手拼进歌名（网易云较常见，如「Just Pretend - Bad Omens」）；
        # 复用旧分析包时这里是最后一道去重，保证展示不重复歌手。
        try:
            from metadata_verify import _strip_artist_suffix
            title = _strip_artist_suffix(title, item.get("artist"))
        except Exception:
            pass
        artist = _text(item.get("artist"), "未知艺人")
        project = _text(item.get("project"), "—")
        # 平台核验结果（程序写入）：专辑名、封面与来源优先用平台数据，
        # 不用模型记忆里的“项目”名充当专辑。
        verified = item.get("metadata_verified") if isinstance(item.get("metadata_verified"), dict) else {}
        verified_album = _text(verified.get("album"))
        verified_cover = _text(verified.get("cover"))
        recommendation = {
            "id": canonical_id,
            "rank": int(item.get("sequence_position") or item.get("selection_rank") or index + 1),
            "track": title,
            "artist": artist,
            "album": verified_album or project,
            "cover": verified_cover or None,
            "metadataStatus": "verified" if verified else "unverified",
            "metadataSource": _text(verified.get("source")) or None,
            "metadataUrl": _text(verified.get("url")) or None,
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
            "details": {
                key: _public_copy(explanation.get(key))
                for key in ("preference_basis", "music_fit", "novelty", "listening_tip")
            } if item.get("agent_details") else None,
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
            "platformLinks": _platform_links(item.get("platform_links")),
            "researchLinks": [
                _text(url) for url in item.get("sources", []) if _text(url)
            ],
            "score": item.get("ranking_score"),
        }
        result.append(recommendation)
    result.sort(key=lambda item: int(item["rank"]))
    return result


def _relations(entity: dict[str, Any]) -> list[list[str]]:
    result: list[list[str]] = []
    relation_status = _text(entity.get("relation_status"), "confirmed")
    groups = (
        ("members", "成员"),
        ("collaborators", "合作音乐人"),
        ("related_projects", "共享音乐人项目"),
    )
    for field, default_relation in groups:
        raw_items = entity.get(field, [])
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if isinstance(item, str):
                target, relation, note = item, default_relation, relation_status
            elif isinstance(item, dict):
                target = _text(item.get("name"), _text(item.get("artist"), _text(item.get("project"))))
                if field == "members":
                    relation = _text(item.get("role"), default_relation)
                elif field == "related_projects" and _text(item.get("person")):
                    relation = f"共享成员 · {_text(item.get('person'))}"
                else:
                    relation = _text(item.get("relation"), _text(item.get("type"), default_relation))
                status = _text(item.get("status"), relation_status)
                confidence = _text(item.get("confidence"))
                note = " · ".join(part for part in (status, confidence) if part)
            else:
                continue
            if target:
                row = [target, relation, note]
                if row not in result:
                    result.append(row)
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
            "bio": _text(profile.get("summary"), "当前资料未提供艺人简介。"),
            "relations": _relations(entity),
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
            "bio": "当前候选仅提供曲目级研究画像，尚未建立完整艺人档案。",
            "relations": [],
            "sources": recommendation.get("researchLinks", []),
            "boundaries": ["本页仅基于当前候选资料，不代表艺人全部作品。"],
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
                "blurb": "当前输入或推荐中出现的发行项目；现有资料未提供更完整的发行说明。",
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
        entry=result.get(item.get("album"))
        if entry and item.get("details"):
            entry["blurb"] = item["details"]["novelty"]
            entry["cover"] = item.get("cover")
    return result


def _source_entries(
    snapshot: dict[str, Any],
    analysis: dict[str, Any],
    bundle: dict[str, Any],
    evidence_audit: dict[str, Any] | None,
    review_report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    style_analysis = analysis.get("style_analysis", {})
    source_count = int(snapshot.get("track_count", 0) or 0)
    declared_count = int(snapshot.get("declared_track_count", source_count) or source_count)
    classified, _unclassified, coverage = _effective_analysis_coverage(analysis, source_count)
    source_coverage = style_analysis.get("source_coverage") or {}
    if source_coverage.get("mode") == "artist_only":
        source_coverage_label = (
            f"{source_coverage.get('sourced_artist_count', 0)}/{source_coverage.get('artist_count', 0)} 位歌手；"
            f"歌单权重 {source_coverage.get('weighted_artist_track_count', 0)}/{source_count}"
        )
    elif source_coverage.get("mode") == "track_with_context":
        direct = int(source_coverage.get("track_evidence_count", 0) or 0)
        album = int(source_coverage.get("album_background_count", 0) or 0)
        artist = int(source_coverage.get("artist_background_count", 0) or 0)
        source_coverage_label = f"{direct + album}/{source_count}（曲目 {direct}、专辑 {album}；艺人背景 {artist}）"
    else:
        source_coverage_label = f"{classified}/{source_count}"
    island_assigned = _island_assigned_track_count(analysis, source_count)
    reports = ((review_report or {}).get("atlas_groups") or {}).get("group_reports") or []
    group_index = int(bundle.get("atlas_group_index", 0) or 0)
    group_review = reports[group_index] if 0 <= group_index < len(reports) else {}
    review_status = _text(group_review.get("status"), "not_available")
    review_accepted = int(group_review.get("accepted_count", 0) or 0)
    review_gaps = int(group_review.get("gap_count", 0) or 0)
    review_rejected = int(group_review.get("rejected_count", 0) or 0)
    review_details = [
        {"track": _short_text(entry.get("title"), 100),
         "artist": _short_text(entry.get("artist"), 80),
         "reason": "缺少风格资料" if "style_unknown" in entry.get("gaps", []) else "资料待补充"}
        for entry in group_review.get("entries", [])
        if isinstance(entry, dict) and entry.get("gaps")
    ]
    reader = snapshot.get("reader") if isinstance(snapshot.get("reader"), dict) else {}
    completeness_status = _text(reader.get("completeness_status"))
    return [
        {
            "id": "snapshot",
            "name": _playlist_display_name(snapshot.get("playlist_name")),
            "kind": "PlaylistSnapshot",
            "via": _public_copy(_text(reader.get("type"), _text(snapshot.get("platform")))),
            "tracks": source_count,
            "coverage": f"{source_count}/{declared_count}",
            "updated": _date(snapshot.get("captured_at")),
            "status": _public_status(completeness_status or _text(snapshot.get("reader_status"), "unknown")),
        },
        {
            "id": "analysis",
            "name": "音乐人与风格研究",
            "kind": "第二步",
            "via": _public_status(style_analysis.get("profile_catalog_mode"), "程序分析"),
            "tracks": source_count,
            "coverage": source_coverage_label,
            "islandAssignment": f"{island_assigned}/{source_count}" if island_assigned is not None else None,
            "updated": _date(analysis.get("generated_at")),
            "status": "degraded" if coverage.get("degraded") else "complete",
        },
        {
            "id": "recommendation",
            "name": "Atlas 推荐",
            "kind": "第三步",
            "via": _public_status(bundle.get("bundle_stage"), "推荐生成"),
            "tracks": len(bundle.get("recommendations", [])),
            "coverage": _public_status(bundle.get("status")),
            "updated": _date(bundle.get("generated_at")),
            "status": _text(bundle.get("publication_status"), _text(bundle.get("status"), "unknown")),
        },
        {
            "id": "evidence",
            "name": "本地证据复核",
            "kind": "来源与身份核验",
            "via": "本地来源记录核对（非联网独立核验）",
            "tracks": len(bundle.get("recommendations", [])),
            "coverage": (f"通过 {review_accepted} / 资料缺口 {review_gaps} / 拒绝 {review_rejected}"
                         if group_review else "未生成复核记录"),
            "updated": _date(group_review.get("reviewed_at")) if group_review else "—",
            "status": review_status,
            "details": review_details,
        },
    ]


def _island_assigned_track_count(analysis: dict[str, Any], source_count: int) -> int | None:
    """Interest-island membership is not source-supported style classification."""
    islands = analysis.get("agent_islands")
    if isinstance(islands, list):
        assigned = {
            record_id
            for island in islands if isinstance(island, dict)
            for record_id in island.get("record_ids", []) if type(record_id) is int and 0 <= record_id < source_count
        }
        return len(assigned)
    return None


def _effective_analysis_coverage(analysis: dict[str, Any], source_count: int) -> tuple[int, int, dict[str, Any]]:
    """Keep source-backed artist coverage distinct from per-track classification."""
    style_analysis = analysis.get("style_analysis", {})
    coverage = dict(style_analysis.get("profile_coverage") or {})
    classified = max(0, min(source_count, int(style_analysis.get("classified_track_count", 0) or 0)))
    unclassified = max(0, source_count - classified)
    source_coverage = style_analysis.get("source_coverage") or {}
    if source_coverage.get("mode") == "artist_only":
        covered = int(source_coverage.get("weighted_artist_track_count", 0) or 0)
        coverage["degraded"] = covered < source_count
    elif source_coverage.get("mode") == "track_with_context":
        covered = int(source_coverage.get("track_evidence_count", 0) or 0) + int(
            source_coverage.get("album_background_count", 0) or 0)
        coverage["degraded"] = covered < source_count
    else:
        coverage["degraded"] = bool(coverage.get("degraded") or unclassified)
    return classified, unclassified, coverage


def build_web_payload(
    snapshot: dict[str, Any],
    analysis: dict[str, Any],
    bundle: dict[str, Any],
    evidence_audit: dict[str, Any] | None = None,
    editorial: dict[str, Any] | None = None,
    review_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert validated pipeline artifacts into the SPA's allow-listed model."""

    return _build_web_payload_from_ranked(
        snapshot, analysis, rank_bundle(bundle, analysis), evidence_audit, editorial, review_report
    )


def _build_web_payload_from_ranked(
    snapshot: dict[str, Any],
    analysis: dict[str, Any],
    ranked_bundle: dict[str, Any],
    evidence_audit: dict[str, Any] | None = None,
    editorial: dict[str, Any] | None = None,
    review_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a ranked bundle after its caller has enforced the bundle contract."""

    editorial = editorial or {}
    interests = _interest_entries(analysis, editorial)
    if analysis.get("agent_islands"):
        records=analysis["source_tags"]["records"]
        tracks=analysis["favorite_tracks"]
        artist_only = analysis.get("analysis_mode") == "artist_summary"
        artist_records = {
            _text(item.get("artist")).casefold(): item
            for item in analysis.get("source_tags", {}).get("artist_records", [])
            if isinstance(item, dict) and _text(item.get("artist"))
        }
        interests=[]
        for index, group in enumerate(analysis["agent_islands"]):
            members=[tracks[rid] for rid in group["record_ids"]]
            names=list(dict.fromkeys(t["artist"] for t in members))
            entry={"id":group["id"],"code":str(index+1).zfill(2),"name":group["name"],
                   "summary":group["summary"],"hue":25+index*90,"genres":[],"artists":names,
                   "repTracks":[] if artist_only else [t["artist"]+" — "+t["title"] for t in members]}
            if artist_only:
                entry["sourceArtists"]=[artist_records[name.casefold()] for name in names
                                        if name.casefold() in artist_records]
            else:
                entry["sourceRecords"]=[{**records[rid],"title":tracks[rid].get("title",""),
                                         "artist":tracks[rid].get("artist","")}
                                        for rid in group["record_ids"]]
            interests.append(entry)
    recommendations = _recommendation_entries(ranked_bundle, interests, evidence_audit)
    style_analysis = analysis.get("style_analysis", {})
    audit_status = _text((evidence_audit or {}).get("status"), "not_available")
    review_status = _text((review_report or {}).get("status"), "not_available")
    source_count = int(snapshot.get("track_count", 0) or 0)
    classified_count, unclassified_count, coverage = _effective_analysis_coverage(analysis, source_count)
    island_assigned_count = _island_assigned_track_count(analysis, source_count)
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
        f"基于 {source_count} 首完整歌单快照，沿着有来源的风格、艺人关系与探索路径，"
        f"整理 {len(recommendations)} 个推荐方向。"
    )
    snapshot_status = _text(raw_reader.get("completeness_status"), _text(snapshot.get("reader_status"), "unknown"))
    payload = {
        "schema_version": WEB_SCHEMA_VERSION,
        "payload_type": "music_atlas_web",
        "generated_at": _text(
            ranked_bundle.get("generated_at"),
            _text(analysis.get("generated_at"), _text(snapshot.get("captured_at"))),
        ),
        "sourceTags": analysis.get("source_tags", {}).get("records", []),
        "issue": {
            "title": _text(editorial.get("title"), ""),
            "lede": lede,
        },
        "status": {
            "run": "completed",
            "snapshot": snapshot_status,
            "analysis": ("complete_with_gaps" if style_analysis.get("evidence_model") == "sourced_tags_v1"
                         else "degraded") if coverage.get("degraded") else "complete",
            "recommendation": _text(ranked_bundle.get("status"), "unknown"),
            "publication": _text(ranked_bundle.get("publication_status"), "not_applicable"),
            "evidence_audit": audit_status,
            "review": review_status,
            "review_elapsed_ms": (review_report or {}).get("elapsed_ms"),
            "profile_coverage_degraded": bool(coverage.get("degraded")),
        },
        "source": {
            "snapshotId": _text(snapshot.get("snapshot_id")),
            "platform": _text(snapshot.get("platform")),
            "playlistId": _text(snapshot.get("playlist_id")),
            "playlistName": _playlist_display_name(snapshot.get("playlist_name")),
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
            "sourcePlaylistTrackCount": int(analysis.get("source_playlist_track_count",
                                                     analysis.get("source_track_count", 0)) or 0),
            "classifiedTrackCount": classified_count,
            "unclassifiedTrackCount": unclassified_count,
            "islandAssignedTrackCount": island_assigned_count,
            "profileCoverage": coverage,
            "evidenceModel": _text(style_analysis.get("evidence_model")),
            "mode": _text(analysis.get("analysis_mode")),
            "sourceCoverage": style_analysis.get("source_coverage") or {},
        },
        "audit": {
            "status": audit_status,
            "acceptedCount": int((evidence_audit or {}).get("accepted_count", 0) or 0),
            "pendingCount": int((evidence_audit or {}).get("pending_count", 0) or 0),
            "rejectedCount": int((evidence_audit or {}).get("rejected_count", 0) or 0),
        },
        "review": {
            "status": review_status,
            "elapsedMs": (review_report or {}).get("elapsed_ms"),
            "networkRequests": int((review_report or {}).get("network_requests", 0) or 0),
            "agentCalls": int((review_report or {}).get("agent_calls", 0) or 0),
        },
        "interests": interests,
        "recommendations": recommendations,
        "artists": _artist_entries(analysis, recommendations),
        "albums": _release_entries(snapshot, recommendations),
        "sources": _source_entries(snapshot, analysis, ranked_bundle, evidence_audit, review_report),
        "taste_review": _taste_review(analysis),
    }
    if analysis.get("selection_mode") == "lastfm_constraints_v1":
        payload["issue"]["lede"] = analysis.get("overall_summary") or custom_lede or "整体风格总结待生成，请重新分析歌单。"
        records = analysis.get("source_tags", {}).get("records", [])
        payload["taggedTrackCount"] = sum(bool(r.get("tags")) for r in records)
    return payload


def _taste_review(analysis: dict[str, Any]) -> dict[str, Any] | None:
    """品味摘要模式的锐评投影；逐曲研究模式返回 None。"""
    if analysis.get("analysis_mode") not in ("taste_summary", "artist_summary"):
        return None
    bundle = analysis.get("taste_summary")
    if not isinstance(bundle, dict):
        return None
    from taste_summary import summarize_review
    return summarize_review(bundle)


def export_web_payload(
    runtime_dir: Path,
    output_path: Path,
    *,
    snapshot_path: Path | None = None,
    analysis_path: Path | None = None,
    bundle_path: Path | None = None,
    evidence_audit_path: Path | None = None,
    editorial_path: Path | None = None,
    review_report_path: Path | None = None,
    require_publishable: bool = False,
    publish_web_result: bool = False,
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
    review_report_path = review_report_path.resolve() if review_report_path is not None else None
    output_path = output_path.resolve()
    input_paths = [snapshot_path, analysis_path, bundle_path, evidence_audit_path]
    if editorial_path is not None:
        input_paths.append(editorial_path)
    if review_report_path is not None:
        input_paths.append(review_report_path)
    if output_path in input_paths:
        raise ContractError("网页视图输出不能覆盖原始运行产物")
    for path in (snapshot_path, analysis_path, bundle_path):
        if not path.is_file():
            raise ContractError(f"找不到网页导出输入：{path}")

    snapshot = validate_playlist_snapshot(read_json(snapshot_path), require_complete=True)
    analysis = validate_analysis_packet(read_json(analysis_path))
    bundle = read_json(bundle_path)
    if (isinstance(bundle, dict) and bundle.get("status") == "ready"
            and bundle.get("bundle_stage") == "ranked"
            and analysis.get("selection_mode") != "lastfm_constraints_v1"):
        # A ranked bundle's contract already recomputes its exact song selection,
        # scores and order against the candidate pool. Re-ranking it would only
        # repeat that expensive deterministic comparison on a fresh export.
        bundle = validate_recommendation_bundle(bundle, analysis)
    else:
        bundle = rank_bundle(bundle, analysis)
        validate_recommendation_bundle(bundle, analysis)

    # Web publication follows deterministic selection gates. A stale audit or
    # local review report must never be credited to this run.
    publish_web_result = publish_web_result or require_publishable
    evidence_audit = None
    if not publish_web_result and evidence_audit_path.is_file():
        evidence_audit = read_json(evidence_audit_path)
        if not isinstance(evidence_audit, dict):
            raise ContractError("evidence_audit 必须是对象")

    review_report = None
    if not publish_web_result and review_report_path is not None and review_report_path.is_file():
        review_report = read_json(review_report_path)
        if not isinstance(review_report, dict):
            raise ContractError("review_report 必须是对象")

    if publish_web_result:
        if (bundle.get("status") != "ready" or bundle.get("bundle_stage") != "ranked"
                or bundle.get("atlas_group_count") != 3
                or type(bundle.get("atlas_group_index")) is not int
                or bundle["atlas_group_index"] not in (0, 1, 2)
                or len(bundle.get("recommendations") or []) != 10):
            raise ContractError("正式 Web 发布需要已验证的三组 Atlas，每组十首且曲目顺序已锁定")

    editorial = None
    if editorial_path is not None:
        if not editorial_path.is_file():
            raise ContractError(f"找不到网页 editorial 配置：{editorial_path}")
        editorial = read_json(editorial_path)
        if not isinstance(editorial, dict):
            raise ContractError("网页 editorial 配置必须是对象")

    payload = _build_web_payload_from_ranked(snapshot, analysis, bundle, evidence_audit, editorial, review_report)
    if publish_web_result:
        payload["status"].update(publication="published", evidence_audit="not_performed",
                                 review="not_performed", review_elapsed_ms=None)
        payload["audit"] = {"status": "not_performed", "acceptedCount": 0,
                            "pendingCount": 0, "rejectedCount": 0}
        payload["review"] = {"status": "not_performed", "elapsedMs": None,
                             "networkRequests": 0, "agentCalls": 0}
        # Gaps remain visible on the recommendation source without inventing
        # a second verification stage.
        gaps = [{"track": _short_text(item.get("title"), 100),
                 "artist": _short_text(item.get("artist"), 80), "reason": "缺少风格资料"}
                for item in bundle["recommendations"]
                if not (item.get("style_evidence") or {}).get("tags")]
        payload["sources"] = [source for source in payload["sources"] if source.get("id") != "evidence"]
        for source in payload["sources"]:
            if source.get("id") == "recommendation":
                source["status"] = "published"
                source["details"] = gaps
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
        "review_status": payload["status"]["review"],
        "review_elapsed_ms": payload["status"].get("review_elapsed_ms"),
    }
