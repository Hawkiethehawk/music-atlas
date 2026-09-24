#!/usr/bin/env python3
"""Deterministic aggregation of Step 2 Agent research or explicit local catalogs.

The analyzer consumes one completed PlaylistSnapshot and its research facts.
It never fetches a platform page, reads recommendation history, or
selects recommendation songs.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any
from preference_model import MODEL_CONFIG, build_interest_profiles
from analysis_contracts import validate_research_bundle

from contracts import (
    canonical_style_ref,
    ContractError,
    SCHEMA_VERSION,
    STYLE_AXIS_IDS,
    artist_key,
    normalized_name,
    normalized_text,
    parse_as_of_date,
    parse_timestamp,
    read_json,
    sha256_path,
    stable_hash,
    track_key,
    utc_now,
    validate_analysis_packet,
    validate_playlist_snapshot,
    validate_recommendation_policy,
    write_json,
)


STYLE_AXIS_LABELS = {
    "heaviness": "重量感",
    "aggression": "攻击性",
    "atmosphere": "氛围感",
    "electronic_presence": "电子存在感",
    "pop_accessibility": "流行可及性",
    "rhythmic_density": "节奏密度",
    "vocal_harshness": "人声极端度",
    "emotional_intensity": "情绪强度",
}

STYLE_AXIS_GUIDE = {
    "heaviness": "失真、低频和整体冲击的重量",
    "aggression": "riff、鼓组和演唱的攻击性",
    "atmosphere": "空间、延音、梦幻或沉浸式层次",
    "electronic_presence": "合成器、采样、电子鼓和制作纹理的比重",
    "pop_accessibility": "旋律直达性、hook 和结构的易听程度",
    "rhythmic_density": "切分、律动变化、说唱和复杂节奏的密度",
    "vocal_harshness": "嘶吼、喊唱、失真和极端唱法的比重",
    "emotional_intensity": "歌词、音色和动态带来的情绪张力",
}


DEFAULT_STYLE_TAXONOMY_PATH = Path(__file__).resolve().parent / "styles" / "style_taxonomy.json"
DEFAULT_STYLE_PROFILE_PATH = Path(__file__).resolve().parent / "styles" / "artist_style_profiles.json"


DEFAULT_POLICY: dict[str, Any] = {
    "min_recommendations": 10,
    "max_recommendations": 12,
    "target_recommendations": 10,
    "max_per_artist": 2,
    "max_per_project": 3,
    "min_projects": 6,
    "candidate_pool_min": 1,
    "exclude_current_favorites": True,
    "cross_platform_links_allowed": True,
    "algorithm_version": "hybrid_music_discovery_v2",
    # 风格资料不足时允许保持 unknown；对应候选评分维度会被程序排除。
    "analysis_quality": {"min_classified_share": 0.5},
    "display_limits": {
        "artists": 5,
        "styles": 5,
    },
    "recall_mix": [
        {"candidate_type": "style_neighbor", "target_ratio": 0.40},
        {"candidate_type": "artist_continuation", "target_ratio": 0.30},
        {"candidate_type": "musician_relation", "target_ratio": 0.20},
        {"candidate_type": "exploration", "target_ratio": 0.10},
    ],
    "ranking_weights": {
        "style_fit": 0.35,
        "relation_fit": 0.20,
        "frequency_fit": 0.15,
        "novelty": 0.10,
        "evidence_quality": 0.15,
        "public_association": 0.05,
    },
    "diversity_policy": {
        "mmr_penalty": 12.0,
        "candidate_type_bonus": 3.0,
        "new_project_bonus": 4.0,
        "new_interest_bonus": 4.0,
        "min_interest_groups": 1,
        "similarity_weights": {
            "style": 0.60,
            "artist": 0.25,
            "project": 0.15,
        },
    },
    "sequence_policy": {
        "mode": "public_tag_continuity",
        "prefer_adjacent_transitions": True,
        "allow_familiar_anchor": True,
        "transition_weight": 0.70,
        "ranking_weight": 0.30,
    },
    "source_policy": {
        "candidate_discovery": [
            "official_artist_or_label",
            "musicbrainz",
            "wikidata",
            "lastfm",
            "listenbrainz",
            "bandcamp",
            "youtube_public_page",
            "spotify_public_page",
        ],
        "apple_music_role": "link_only",
        "forbidden_candidate_sources": [
            "apple_music_personalized_recommendations",
            "netease_personalized_recommendations",
        ],
    },
}


def load_recommendation_policy(path: Path | None = None) -> dict[str, Any]:
    """Explicit, validated partial overrides; never read feedback or proposals."""
    policy = deepcopy(DEFAULT_POLICY)
    if path is None:
        return validate_recommendation_policy(policy)
    overrides = _load_json_object(path)
    tunable = {
        "ranking_weights", "recall_mix", "max_per_artist", "max_per_project",
        "min_projects", "candidate_pool_min", "diversity_policy", "sequence_policy", "display_limits", "analysis_quality",
    }

    def merge(target: dict[str, Any], changes: dict[str, Any], label: str) -> None:
        unknown = set(changes) - set(target)
        if unknown:
            raise ContractError(f"{label} 包含未知字段：{sorted(unknown)}")
        for key, value in changes.items():
            if isinstance(target[key], dict):
                if not isinstance(value, dict):
                    raise ContractError(f"{label}.{key} 必须是对象")
                merge(target[key], value, f"{label}.{key}")
            else:
                target[key] = deepcopy(value)

    for key in set(overrides) - tunable:
        if key in policy and overrides[key] != policy[key]:
            raise ContractError(f"策略文件不得修改固定设计边界：{key}")
    merge(policy, overrides, "recommendation_policy")
    return validate_recommendation_policy(policy)


def _load_json_object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ContractError(f"{path} 必须是 JSON 对象")
    return value


def load_style_taxonomy(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"找不到风格本体：{path}")
    payload = _load_json_object(path)
    version = normalized_text(payload.get("taxonomy_version"))
    styles = payload.get("styles")
    axis_definitions = payload.get("axes", STYLE_AXIS_GUIDE)
    if not version or not isinstance(styles, dict) or not styles:
        raise ContractError(f"风格本体必须包含 taxonomy_version 和 styles：{path}")
    if not isinstance(axis_definitions, dict):
        raise ContractError(f"历史听感轴定义必须是对象：{path}")
    cleaned_axes = {
        axis: normalized_text(axis_definitions.get(axis))
        for axis in STYLE_AXIS_IDS
    }
    if any(not description for description in cleaned_axes.values()):
        raise ContractError(f"历史听感轴定义必须完整覆盖听感轴：{path}")
    cleaned: dict[str, dict[str, Any]] = {}
    for style_id, raw_style in styles.items():
        if not isinstance(raw_style, dict):
            raise ContractError(f"风格定义不是对象：{style_id}")
        key = normalized_text(style_id)
        label = normalized_text(raw_style.get("label"))
        if not key or not label:
            raise ContractError(f"风格定义缺少 id 或 label：{style_id}")
        cleaned[key] = {
            "style_id": key,
            "style_ref": f"style:{key}",
            "label": label,
            "parent": normalized_text(raw_style.get("parent")),
            "definition": normalized_text(raw_style.get("definition")),
            "boundary": normalized_text(raw_style.get("boundary")),
        }
    return {
        "taxonomy_version": version,
        "styles": cleaned,
        "known_style_refs": [item["style_ref"] for item in cleaned.values()],
        "axis_definitions": cleaned_axes,
    }


def _normalize_style_axes(value: Any, label: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} 必须是对象")
    axes: dict[str, float] = {}
    for axis in STYLE_AXIS_IDS:
        score = value.get(axis)
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= float(score) <= 100:
            raise ContractError(f"{label}.{axis} 必须是 0 到 100 的数字")
        axes[axis] = float(score)
    return axes


# 与契约层共用同一套笔误规范化，避免两份实现漂移。
_canonical_style_ref = canonical_style_ref


def _normalize_style_mix(
    value: Any,
    label: str,
    known_style_refs: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError(f"{label} 必须是数组")
    mix: list[dict[str, Any]] = []
    total = 0.0
    for index, raw_item in enumerate(value):
        if not isinstance(raw_item, dict):
            raise ContractError(f"{label}[{index}] 必须是对象")
        style_ref = _canonical_style_ref(raw_item.get("style_ref"), known_style_refs)
        if style_ref not in known_style_refs:
            raise ContractError(f"{label}[{index}] 包含未知风格引用：{style_ref}")
        role = normalized_text(raw_item.get("role"))
        if role not in {"primary", "secondary"}:
            raise ContractError(f"{label}[{index}].role 必须是 primary 或 secondary")
        weight = raw_item.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < float(weight) <= 1:
            raise ContractError(f"{label}[{index}].weight 必须大于 0 且小于等于 1")
        item = {
            "style_ref": style_ref,
            "role": role,
            "weight": round(float(weight), 6),
        }
        confidence = normalized_text(raw_item.get("confidence"))
        if confidence:
            item["confidence"] = confidence
        mix.append(item)
        total += float(weight)
    if mix and abs(total - 1.0) > 0.001:
        raise ContractError(f"{label} 权重总和必须为 1，实际为 {total:.6f}")
    return mix


def _normalize_style_profile(
    raw_profile: dict[str, Any],
    *,
    fallback_name: str,
    known_style_refs: set[str],
) -> dict[str, Any]:
    canonical_name = normalized_text(raw_profile.get("canonical_name") or fallback_name)
    if not canonical_name:
        raise ContractError("风格画像缺少 canonical_name")
    aliases = raw_profile.get("aliases", [])
    if not isinstance(aliases, list):
        aliases = []
    status = normalized_text(raw_profile.get("classification_status") or "classified")
    if status not in {"classified", "unclassified"}:
        raise ContractError(f"{canonical_name} 的 classification_status 无效：{status}")
    sources = raw_profile.get("sources", [])
    if not isinstance(sources, list):
        sources = []
    cleaned_sources = [normalized_text(source) for source in sources if normalized_text(source)]
    boundaries = raw_profile.get("boundaries", [])
    if not isinstance(boundaries, list):
        boundaries = []
    cleaned_boundaries = [normalized_text(item) for item in boundaries if normalized_text(item)]
    profile = {
        "canonical_name": canonical_name,
        "aliases": [normalized_text(alias) for alias in aliases if normalized_text(alias)],
        "classification_status": status,
        "confidence": normalized_text(raw_profile.get("confidence") or "medium"),
        "style_mix": _normalize_style_mix(
            raw_profile.get("style_mix", []),
            f"{canonical_name}.style_mix",
            known_style_refs,
        ),
        "style_axes": _normalize_style_axes(
            raw_profile.get("style_axes"),
            f"{canonical_name}.style_axes",
        ) if status == "classified" else dict.fromkeys(STYLE_AXIS_IDS),
        "summary": normalized_text(raw_profile.get("summary")),
        "boundaries": cleaned_boundaries,
        "sources": cleaned_sources,
        "release_overrides": [],
    }
    if not profile["summary"]:
        raise ContractError(f"{canonical_name} 的 summary 不能为空")
    if profile["confidence"] not in {"low", "medium", "high"}:
        raise ContractError(f"{canonical_name} 的 confidence 无效")
    if status == "classified" and not profile["style_mix"]:
        raise ContractError(f"{canonical_name} 已分类但没有 style_mix")
    raw_overrides = raw_profile.get("release_overrides", [])
    if not isinstance(raw_overrides, list):
        raise ContractError(f"{canonical_name}.release_overrides 必须是数组")
    for index, raw_override in enumerate(raw_overrides):
        if not isinstance(raw_override, dict):
            raise ContractError(f"{canonical_name}.release_overrides[{index}] 必须是对象")
        albums = raw_override.get("match_albums", [])
        titles = raw_override.get("match_titles", [])
        if not isinstance(albums, list):
            albums = []
        if not isinstance(titles, list):
            titles = []
        if not albums and not titles:
            raise ContractError(f"{canonical_name}.release_overrides[{index}] 缺少匹配条件")
        override: dict[str, Any] = {
            "match_albums": [normalized_name(item) for item in albums if normalized_name(item)],
            "match_titles": [normalized_name(item) for item in titles if normalized_name(item)],
        }
        if not override["match_albums"] and not override["match_titles"]:
            raise ContractError(f"{canonical_name} 的覆盖匹配条件不能为空")
        if "style_mix" in raw_override:
            override["style_mix"] = _normalize_style_mix(
                raw_override["style_mix"],
                f"{canonical_name}.release_overrides[{index}].style_mix",
                known_style_refs,
            )
        if "style_axes" in raw_override:
            override["style_axes"] = _normalize_style_axes(
                raw_override.get("style_axes"),
                f"{canonical_name}.release_overrides[{index}].style_axes",
            )
        summary = normalized_text(raw_override.get("summary"))
        if summary:
            override["summary"] = summary
        override_sources = raw_override.get("sources", [])
        if "confidence" in raw_override:
            if raw_override["confidence"] not in {"high", "medium", "low"}:
                raise ContractError(f"{canonical_name} 的覆盖规则 confidence 无效")
            override["confidence"] = raw_override["confidence"]
        if isinstance(override_sources, list):
            override["sources"] = [
                normalized_text(source)
                for source in override_sources
                if normalized_text(source)
            ]
        profile["release_overrides"].append(override)
    return profile


def load_style_profile_catalog(path: Path, taxonomy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise ContractError(f"找不到逐艺人风格画像：{path}")
    payload = _load_json_object(path)
    entries = payload.get("artists", payload)
    if not isinstance(entries, dict):
        raise ContractError(f"逐艺人风格画像必须包含 artists 对象：{path}")
    known_style_refs = set(taxonomy["known_style_refs"])
    catalog: dict[str, dict[str, Any]] = {}
    for key, raw_profile in entries.items():
        if not isinstance(raw_profile, dict):
            raise ContractError(f"风格画像不是对象：{key}")
        profile = _normalize_style_profile(
            raw_profile,
            fallback_name=normalized_text(key),
            known_style_refs=known_style_refs,
        )
        names = [profile["canonical_name"], *profile["aliases"], normalized_text(key)]
        for name in names:
            marker = normalized_name(name)
            if not marker:
                continue
            if marker in catalog and catalog[marker]["canonical_name"] != profile["canonical_name"]:
                raise ContractError(f"风格画像别名冲突：{name}")
            catalog[marker] = profile
    return catalog


def resolve_style_profile_path(path: Path) -> Path:
    if path.is_file():
        return path
    fallback = path.with_name("artist_style_profiles.example.json")
    if path.name == "artist_style_profiles.json" and fallback.is_file():
        return fallback
    raise ContractError(f"找不到逐艺人风格画像：{path}")


def _unclassified_profile(name: str) -> dict[str, Any]:
    return {
        "canonical_name": normalized_text(name),
        "aliases": [],
        "classification_status": "unclassified",
        "confidence": "low",
        "style_mix": [],
        "style_axes": dict.fromkeys(STYLE_AXIS_IDS),
        "summary": f"{normalized_text(name)} 的公开资料未达到细分风格核验阈值，本次不将其自动归入摇滚、金属核或其他粗分类。",
        "boundaries": ["不把未核验艺人自动归入摇滚或金属核。"],
        "sources": [],
        "release_overrides": [],
    }


def _style_profile_for(name: str, catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return catalog.get(normalized_name(name), _unclassified_profile(name))


def _release_override(profile: dict[str, Any], track: dict[str, Any]) -> dict[str, Any] | None:
    album_marker = normalized_name(track.get("album"))
    title_marker = normalized_name(track.get("title"))
    matches = []
    for override in profile.get("release_overrides", []):
        albums, titles = override.get("match_albums", []), override.get("match_titles", [])
        if (not albums or album_marker in albums) and (not titles or title_marker in titles):
            matches.append(override)
    return max(matches, key=lambda item: (bool(item.get("match_titles")), bool(item.get("match_albums"))), default=None)


def _style_assignment(track: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    override = _release_override(profile, track)
    effective_mix = override.get("style_mix", profile["style_mix"]) if override else profile["style_mix"]
    effective_axes = override.get("style_axes", profile["style_axes"]) if override else profile["style_axes"]
    effective_summary = override.get("summary", profile["summary"]) if override else profile["summary"]
    effective_sources = list(profile.get("sources", []))
    if override:
        for source in override.get("sources", []):
            if source not in effective_sources:
                effective_sources.append(source)
    refs = [item["style_ref"] for item in effective_mix]
    primary_ref = next((item["style_ref"] for item in effective_mix if item["role"] == "primary"), "")
    provenance = {}
    for field in ("style_mix", "style_axes"):
        source = override if override and field in override else profile
        provenance[field] = {
            "scope": ("track" if override.get("match_titles") else "release") if source is override else "artist",
            "confidence": source.get("confidence", profile["confidence"]),
            "sources": list(source.get("sources", [])),
        }
    confidence_order = {"low": 0, "medium": 1, "high": 2}
    confidence = min((item["confidence"] for item in provenance.values()), key=confidence_order.get)
    status = "classified" if effective_mix and all(value is not None for value in effective_axes.values()) else "unclassified"
    return {
        "position": track.get("position"),
        "track_key": track["track_key"],
        "title": track["title"],
        "artist": track["artist"],
        "album": track.get("album", ""),
        "classification_status": status,
        "confidence": confidence,
        "primary_style_ref": primary_ref,
        "style_refs": refs,
        "style_mix": effective_mix,
        "style_axes": effective_axes,
        "applied_scope": "release_override" if override else "artist_profile",
        "rationale": effective_summary,
        "sources": effective_sources,
        "field_provenance": provenance,
    }


def load_preferred_artists(path: Path) -> list[str]:
    if not path.is_file():
        return []
    result: list[str] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContractError(f"无法读取偏好艺人清单：{path}：{exc}") from exc
    for raw in lines:
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        marker = normalized_name(name)
        if marker and marker not in seen:
            seen.add(marker)
            result.append(name)
    return result


def load_relationship_catalog(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    payload = _load_json_object(path)
    entries = payload.get("artists", payload)
    if not isinstance(entries, dict):
        raise ContractError(f"音乐人关系资料必须包含 artists 对象：{path}")
    catalog: dict[str, dict[str, Any]] = {}
    for key, raw_entry in entries.items():
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        canonical = normalized_text(entry.get("canonical_name") or key)
        if not canonical:
            continue
        entry["canonical_name"] = canonical
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        entry["aliases"] = [normalized_text(alias) for alias in aliases if normalized_text(alias)]
        marker_names = [canonical, *entry["aliases"], normalized_text(key)]
        for name in marker_names:
            marker = normalized_name(name)
            if marker:
                catalog[marker] = entry
    return catalog


def resolve_artist(name: str, catalog: dict[str, dict[str, Any]]) -> str:
    entry = catalog.get(normalized_name(name))
    return normalized_text(entry.get("canonical_name")) if entry else normalized_text(name)


def _source_urls(entry: dict[str, Any]) -> list[str]:
    raw_sources = entry.get("sources", [])
    if not isinstance(raw_sources, list):
        return []
    return [normalized_text(source) for source in raw_sources if normalized_text(source)]


def _person_ref(name: str) -> str:
    return f"person:{artist_key(name)}"


def _project_ref(name: str) -> str:
    return f"project:{artist_key(name)}"


def _artist_ref(name: str) -> str:
    return f"artist:{artist_key(name)}"


def _copy_relation_list(entry: dict[str, Any], field: str) -> list[dict[str, Any]]:
    value = entry.get(field, [])
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        copied = {
            key: item[key]
            for key in ("name", "role", "status", "relation", "person", "confidence", "sources", "evidence_items",
                        "begin", "end", "cross_checked", "source_conflict", "external_ids", "retrieved_at")
            if key in item
        }
        name = normalized_text(copied.get("name"))
        if not name:
            continue
        copied["name"] = name
        if not isinstance(copied.get("sources"), list):
            copied["sources"] = []
        copied["sources"] = [
            normalized_text(source)
            for source in copied["sources"]
            if normalized_text(source)
        ]
        result.append(copied)
    return result


def _entity(
    name: str,
    *,
    primary_count: int,
    credited_count: int,
    preferred: bool,
    catalog: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    entry = catalog.get(normalized_name(name), {})
    canonical = normalized_text(entry.get("canonical_name") or name)
    members = _copy_relation_list(entry, "members")
    vocalists = _copy_relation_list(entry, "lead_vocalists")
    collaborators = _copy_relation_list(entry, "collaborators")
    related_projects = _copy_relation_list(entry, "related_projects")
    sources = _source_urls(entry)
    for fact in (*members, *vocalists, *collaborators, *related_projects):
        for source in fact.get("sources", []):
            if source not in sources:
                sources.append(source)
    relation_status = "confirmed" if members or vocalists or collaborators or related_projects else "unmapped"
    if relation_status == "confirmed" and entry.get("research_origin") == "agent":
        relation_status = "researched"
    refs = [_artist_ref(canonical)]
    for member in members:
        refs.append(_person_ref(member["name"]))
    for vocalist in vocalists:
        refs.append(_person_ref(vocalist["name"]))
    for collaborator in collaborators:
        refs.append(_artist_ref(collaborator["name"]))
    for project in related_projects:
        refs.append(_project_ref(project["name"]))
    refs = list(dict.fromkeys(refs))
    entity = {
        "entity_ref": _artist_ref(canonical),
        "name": canonical,
        "aliases": list(entry.get("aliases", [])),
        "entity_type": normalized_text(entry.get("entity_type") or "unknown"),
        "primary_track_count": primary_count,
        "credited_track_count": credited_count,
        "is_preferred": preferred,
        "relation_status": relation_status,
        "members": members,
        "lead_vocalists": vocalists,
        "collaborators": collaborators,
        "related_projects": related_projects,
        "sources": sources,
        "analysis_refs": refs,
    }
    if entry.get("research_origin") == "agent":
        entity["research_origin"] = "agent"
        entity["verification_scope"] = "pending_independent_verification"
    return entity, refs


def _copy_track(track: dict[str, Any], resolved_artist: str, resolved_artists: list[str]) -> dict[str, Any]:
    title = normalized_text(track.get("title"))
    links = track.get("links", {})
    if not isinstance(links, dict):
        links = {}
    result = {
        "position": track.get("position"),
        "title": title,
        "artist": resolved_artist,
        "artists": resolved_artists,
        "album": normalized_text(track.get("album")),
        "duration": normalized_text(track.get("duration")),
        "platform_track_id": normalized_text(track.get("platform_track_id")),
        "track_key": track_key(title, resolved_artist),
        "links": {
            normalized_text(platform): normalized_text(link)
            for platform, link in links.items()
            if normalized_text(platform) and normalized_text(link)
        },
    }
    if track.get("album_link"):
        result["album_link"] = normalized_text(track.get("album_link"))
    return result


def _style_distribution(
    assignments: list[dict[str, Any]],
    taxonomy: dict[str, Any],
    *,
    source_count: int,
    classified_count: int,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter(
        assignment["primary_style_ref"]
        for assignment in assignments
        if assignment["classification_status"] == "classified" and assignment["primary_style_ref"]
    )
    styles_by_ref = {item["style_ref"]: item for item in taxonomy["styles"].values()}
    result: list[dict[str, Any]] = []
    for rank, (style_ref, count) in enumerate(
        sorted(counts.items(), key=lambda entry: (-entry[1], entry[0])),
        1,
    ):
        style = styles_by_ref[style_ref]
        result.append(
            {
                "rank": rank,
                "style_ref": style_ref,
                "style_id": style["style_id"],
                "label": style["label"],
                "count": count,
                "share": round(count / source_count, 6) if source_count else 0,
                "classified_share": round(count / classified_count, 6) if classified_count else 0,
            }
        )
    return result


def build_overlap_style_distribution(
    assignments: list[dict[str, Any]],
    taxonomy: dict[str, Any],
    *,
    source_count: int,
    classified_count: int,
    primary_distribution: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Count every distinct style tag attached to a classified track.

    ``style_distribution`` intentionally remains a mutually-exclusive primary
    style view for audit and backwards compatibility.  This companion view is
    the user-facing preference signal: one track may increment several styles,
    so its percentages are coverage values and are not expected to sum to 1.
    """

    counts: Counter[str] = Counter()
    for assignment in assignments:
        if assignment.get("classification_status") != "classified":
            continue
        refs = [
            ref
            for ref in dict.fromkeys(assignment.get("style_refs", []))
            if isinstance(ref, str) and ref
        ]
        counts.update(refs)

    styles_by_ref = {item["style_ref"]: item for item in taxonomy["styles"].values()}
    primary_rank = {
        item.get("style_ref"): int(item.get("rank") or 999999)
        for item in (primary_distribution or [])
        if isinstance(item, dict) and item.get("style_ref")
    }
    result: list[dict[str, Any]] = []
    ordered = sorted(
        counts.items(),
        key=lambda entry: (-entry[1], primary_rank.get(entry[0], 999999), entry[0]),
    )
    for rank, (style_ref, count) in enumerate(ordered, 1):
        style = styles_by_ref.get(style_ref)
        if not style:
            continue
        result.append(
            {
                "rank": rank,
                "style_ref": style_ref,
                "style_id": style["style_id"],
                "label": style["label"],
                "count": count,
                "share": round(count / source_count, 6) if source_count else 0,
                "classified_share": round(count / classified_count, 6) if classified_count else 0,
                "overlap": True,
            }
        )
    return result


def apply_public_style_evidence(packet: dict[str, Any], source_tags: dict[str, Any], *,
                                minimum_share: float | None = None) -> dict[str, Any]:
    """Bind fetched public tags to a Step 2 packet without inventing listening axes.

    `source_tags` is collect_tags' {provider, records} result. Each record
    retains its track_key and every fetched `evidence` layer (track, album,
    artist); a layer carries scope, source, url, retrieved_at, identity_status,
    status and tags [{tag, style_ref}]. Only supported, timestamped, URL-backed
    mapped refs are eligible. An artist layer is background, never direct song
    evidence. For artist_summary (>=1000 tracks), *only* artist layers are used;
    track assignments remain identity placeholders, not track analyses.
    """
    from urllib.parse import urlparse

    result = deepcopy(packet)
    mode = result.get("analysis_mode")
    processed_count = result["source_track_count"]
    playlist_count = result.get("source_playlist_track_count", processed_count)
    if (isinstance(playlist_count, bool) or not isinstance(playlist_count, int)
            or playlist_count < 1 or playlist_count < processed_count):
        raise ContractError("原始歌单曲目数必须是正整数且不小于本次处理曲目数")
    if mode == "artist_summary" and playlist_count < 1000:
        raise ContractError("artist_summary 仅适用于至少 1000 首歌")
    if mode != "artist_summary" and playlist_count >= 1000:
        raise ContractError("1000 首及以上必须只做歌手层级分析")
    records = source_tags.get("records") if isinstance(source_tags, dict) else None
    tracks = result["favorite_tracks"]
    if not isinstance(records, list) or len(records) != len(tracks):
        raise ContractError("公开资料记录必须与歌单行数一致")
    if [record.get("track_key") for record in records if isinstance(record, dict)] != [
        track["track_key"] for track in tracks
    ]:
        raise ContractError("公开资料记录必须按顺序对应本次歌曲身份")
    definitions = result["style_analysis"]["style_definitions"]
    known = {definition["style_ref"] for definition in definitions}
    by_ref = {definition["style_ref"]: definition for definition in definitions}
    scope_order = ("track", "album", "artist")

    def valid_layers(record: dict[str, Any], track: dict[str, Any]) -> list[dict[str, Any]]:
        raw_layers = record.get("evidence")
        # Backward-compatible input for a previously fetched single-layer
        # record. No timestamp or URL means no evidence, not a guessed style.
        if not isinstance(raw_layers, list):
            raw_layers = [record]
        layers = []
        for item in raw_layers:
            if not isinstance(item, dict) or item.get("scope") not in scope_order:
                continue
            if item.get("identity_status") == "mismatch" or item.get("status") not in (None, "supported"):
                continue
            subject = item.get("subject")
            expected = {"artist": track["artist"]}
            if item["scope"] == "track":
                expected["track"] = track["title"]
            elif item["scope"] == "album":
                expected["album"] = track.get("album", "")
            if not isinstance(subject, dict) or not all(
                normalized_name(subject.get(key)) == normalized_name(value) and normalized_name(value)
                for key, value in expected.items()
            ):
                continue
            url, stamp = item.get("url"), item.get("retrieved_at")
            if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                continue
            try:
                parse_timestamp(stamp, "公开风格资料 retrieved_at")
            except ContractError:
                continue
            tags = item.get("tags")
            if not isinstance(tags, list):
                continue
            kept = [{"tag": tag["tag"], "style_ref": tag["style_ref"]} for tag in tags
                    if isinstance(tag, dict) and isinstance(tag.get("tag"), str) and tag["tag"].strip()
                    and tag.get("style_ref") in known]
            if kept:
                layers.append({"scope": item["scope"], "provider": item.get("source") or source_tags.get("provider"),
                               "url": url, "retrieved_at": stamp, "identity_status": item.get("identity_status", "request_only"),
                               "tags": kept})
        return layers

    layers_by_track = [valid_layers(record, track) for record, track in zip(records, tracks)]
    artist_layers: dict[str, list[dict[str, Any]]] = {}
    for track, layers in zip(tracks, layers_by_track):
        marker = normalized_name(track["artist"])
        existing = artist_layers.setdefault(marker, [])
        for layer in layers:
            if layer["scope"] == "artist" and layer not in existing:
                existing.append(layer)

    def refs_for(layers: list[dict[str, Any]]) -> list[str]:
        return list(dict.fromkeys(tag["style_ref"] for layer in layers for tag in layer["tags"]))

    def mix_for(refs: list[str]) -> list[dict[str, Any]]:
        # The weights are deterministic tag shares, not sound intensity or
        # a listener's probability of liking the style.
        return [{"style_ref": ref, "role": "primary" if index == 0 else "secondary",
                 "weight": round(1 / len(refs), 6)} for index, ref in enumerate(refs)]

    assignments = []
    counts = Counter()
    for index, (track, layers) in enumerate(zip(tracks, layers_by_track), 1):
        chosen_scope = next((scope for scope in scope_order
                             if mode != "artist_summary" and any(layer["scope"] == scope for layer in layers)), "unknown")
        selected = [layer for layer in layers if layer["scope"] == chosen_scope]
        refs = refs_for(selected)
        direct_refs = refs if chosen_scope == "track" else []
        counts[chosen_scope] += 1
        assignments.append({
            "position": index, "track_key": track["track_key"], "title": track["title"],
            "artist": track["artist"], "album": track.get("album", ""),
            "classification_status": "classified" if direct_refs else "unclassified",
            "confidence": "medium" if direct_refs else "low", "primary_style_ref": direct_refs[0] if direct_refs else "",
            "style_refs": direct_refs, "style_mix": mix_for(direct_refs),
            "background_style_refs": refs if chosen_scope in ("album", "artist") else [],
            "applied_scope": (chosen_scope + "_background" if chosen_scope in ("album", "artist") else chosen_scope),
            "rationale": ("公开曲目风格标签" if chosen_scope == "track" else
                          "所属专辑的风格背景，不等同于单曲听感" if chosen_scope == "album" else
                          "艺人的整体风格背景，不等同于单曲事实" if chosen_scope == "artist" else
                          "未取得可归属的公开风格资料" if mode != "artist_summary" else
                          "千首及以上仅分析歌手，不逐曲评价"),
            "sources": [layer["url"] for layer in selected], "evidence_items": selected,
        })

    profiles = result["style_analysis"]["artist_profiles"]
    for profile in profiles:
        layers = artist_layers.get(normalized_name(profile["artist"]), [])
        refs = refs_for(layers)
        profile.update(classification_status="classified" if refs else "unclassified",
                       confidence="medium" if refs else "low", primary_style_ref=refs[0] if refs else "",
                       style_refs=refs, style_mix=mix_for(refs), sources=[layer["url"] for layer in layers],
                       evidence_items=layers, summary="公开艺人风格背景" if refs else "暂无可核验的艺人风格标签")
        profile.pop("style_axes", None)
        profile.pop("style_weights", None)

    profile_coverage = result["style_analysis"]["profile_coverage"]
    artist_count = sum(bool(profile["style_refs"]) for profile in profiles)
    primary_counts = {normalized_name(row["artist"]): row["count"] for row in result["primary_distribution"]}
    weighted = sum(primary_counts.get(normalized_name(profile["artist"]), 0)
                   for profile in profiles if profile["style_refs"])
    total = result["source_track_count"]
    direct = counts["track"]
    profile_coverage.update(classified_artist_count=artist_count,
                            unclassified_artist_count=len(profiles) - artist_count,
                            degraded=(direct < total if mode != "artist_summary" else weighted < total),
                            classified_track_share=round(direct / total, 6) if total and mode != "artist_summary" else 0.0,
                            assignment_scopes=dict(counts))

    style = result["style_analysis"]
    classified = counts["track"]
    style.update(evidence_model="sourced_tags_v1", analysis_mode=mode,
                 artist_profiles=profiles, artist_profile_count=len(profiles),
                 classified_track_count=classified, unclassified_track_count=total - classified,
                 active_style_refs=list(dict.fromkeys(ref for item in [*assignments, *profiles]
                                                     for ref in [*item["style_refs"], *item.get("background_style_refs", [])])),
                 source_coverage={"mode": "artist_only" if mode == "artist_summary" else "track_with_context",
                                  "track_evidence_count": counts["track"], "album_background_count": counts["album"],
                                  "artist_background_count": counts["artist"], "no_style_evidence_count": counts["unknown"],
                                  "artist_count": len(profiles), "sourced_artist_count": artist_count,
                                  "weighted_artist_track_count": weighted})
    style.pop("axis_definitions", None)
    style.pop("style_axes", None)
    taxonomy = {"styles": {d["style_ref"]: d for d in definitions}}
    style["style_distribution"] = _style_distribution(assignments, taxonomy, source_count=total,
                                                      classified_count=classified)
    style["overlap_style_distribution"] = build_overlap_style_distribution(assignments, taxonomy,
                    source_count=total, classified_count=classified,
                    primary_distribution=style["style_distribution"])
    style["interest_model"] = dict(MODEL_CONFIG)
    style["interest_profiles"] = build_interest_profiles(assignments)
    style["dominant_style_mix"] = [{**row, "weight": round(row["count"] / classified, 6) if classified else 0}
                                    for row in style["style_distribution"]]
    for scope in ("album", "artist"):
        background_counts: Counter[str] = Counter(
            item["background_style_refs"][0] for item in assignments
            if item["applied_scope"] == scope + "_background" and item["background_style_refs"]
        )
        style[scope + "_background_style_distribution"] = [
            {"style_ref": ref, "label": by_ref[ref]["label"], "count": count,
             "share_of_playlist": round(count / total, 6) if total else 0}
            for ref, count in sorted(background_counts.items(), key=lambda entry: (-entry[1], entry[0]))
        ]
    if mode == "artist_summary":
        # This is a weighted *artist* rollup. It is not a claim that any
        # individual song in the playlist has been analyzed or tagged.
        artist_weighted: Counter[str] = Counter()
        for profile in profiles:
            if profile["style_refs"]:
                artist_weighted[profile["primary_style_ref"]] += primary_counts.get(normalized_name(profile["artist"]), 0)
        style["artist_style_distribution"] = [
            {"style_ref": ref, "label": by_ref[ref]["label"], "artist_weighted_track_count": count,
             "share_of_playlist": round(count / total, 6) if total else 0}
            for ref, count in sorted(artist_weighted.items(), key=lambda entry: (-entry[1], entry[0]))
        ]
        style["frequency_basis"] = "primary_artist_style_weighted_by_playlist_song_count"
    result["track_style_assignments"] = assignments
    # A second reconciliation can replace a stale provider result. Remove
    # every old style ref before adding only the currently supported ones.
    relationship_refs = [ref for ref in result["analysis_ref_ids"] if not ref.startswith("style:")]
    result["analysis_ref_ids"] = list(dict.fromkeys([*relationship_refs, *style["active_style_refs"]]))
    quality = result["recommendation_policy"].setdefault("analysis_quality", {})
    quality_key = "min_artist_weight_share" if mode == "artist_summary" else "min_track_or_album_share"
    threshold = (quality.get(quality_key, 0.3 if mode == "artist_summary" else 0.5)
                 if minimum_share is None else minimum_share)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        raise ContractError("公开资料覆盖下限必须大于 0 且不超过 1")
    result["recommendation_policy"]["analysis_quality"] = {quality_key: threshold}
    try:
        fingerprint = stable_hash({"basis": packet["analysis_id"], "tags": source_tags,
                                   "model": "sourced_tags_v1"})
    except (TypeError, ValueError) as exc:
        raise ContractError("公开来源结果含非 JSON 字段，不能写入分析包") from exc
    result["source_tags"] = deepcopy(source_tags)
    result["analysis_id"] = f"analysis-{fingerprint[:20]}"
    return validate_analysis_packet(result)


def _aggregate_style_axes(assignments: list[dict[str, Any]]) -> dict[str, float | None]:
    classified = [
        assignment
        for assignment in assignments
        if assignment["classification_status"] == "classified"
    ]
    if not classified:
        return dict.fromkeys(STYLE_AXIS_IDS)
    return {
        axis: round(
            sum(float(assignment["style_axes"][axis]) for assignment in classified) / len(classified),
            2,
        )
        for axis in STYLE_AXIS_IDS
    }


def _style_profile_output(
    name: str,
    profile: dict[str, Any],
    *,
    primary_count: int,
    credited_count: int,
) -> dict[str, Any]:
    primary_style_ref = next(
        (item["style_ref"] for item in profile["style_mix"] if item["role"] == "primary"),
        "",
    )
    return {
        "artist": name,
        "entity_ref": _artist_ref(name),
        "primary_track_count": primary_count,
        "credited_track_count": credited_count,
        "is_core_artist": primary_count > 0,
        "classification_status": profile["classification_status"],
        "confidence": profile["confidence"],
        "primary_style_ref": primary_style_ref,
        "style_mix": profile["style_mix"],
        "style_axes": profile["style_axes"],
        "summary": profile["summary"],
        "boundaries": profile["boundaries"],
        "sources": profile["sources"],
        "release_override_count": len(profile.get("release_overrides", [])),
    }


def _compile_research_inputs(bundle: dict[str, Any], snapshot: dict[str, Any]) -> tuple[dict, dict, dict]:
    """Keep Agent facts per track; derive artist summaries for existing consumers."""
    rows = {item["position"]: item for batch in bundle["batches"] for item in batch["track_profiles"]}
    assignments, grouped = {}, {}
    for position, track in enumerate(snapshot["tracks"], 1):
        row = rows[position]
        evidence = [{**item, "verification_result": "unverified"} for item in row["evidence_items"]]
        sources = list(dict.fromkeys(item["url"] for item in evidence))
        assignment = {
            "position": position, "track_key": row["track_key"], "title": track["title"], "artist": track["artist"],
            "album": track.get("album", ""), "classification_status": row["classification_status"], "confidence": row["confidence"],
            "primary_style_ref": next((item["style_ref"] for item in row["style_mix"] if item["role"] == "primary"), ""),
            "style_refs": [item["style_ref"] for item in row["style_mix"]], "style_mix": deepcopy(row["style_mix"]),
            "style_axes": deepcopy(row.get("style_axes", dict.fromkeys(STYLE_AXIS_IDS))), "applied_scope": "agent_" + row["scope"],
            "rationale": row["summary"], "sources": sources, "evidence_items": evidence,
            "field_provenance": {field: {"scope": row["scope"], "confidence": row["confidence"], "sources": sources,
                                         "origin": "agent_research", "verification_scope": "pending_independent_verification"}
                                 for field in ("style_mix", "style_axes")},
        }
        assignments[position] = assignment
        grouped.setdefault(normalized_name(track["artist"]), []).append(assignment)
    catalog = {}
    for items in grouped.values():
        artist = items[0]["artist"]
        classified = [item for item in items if item["classification_status"] == "classified"]
        if not classified:
            catalog[normalized_name(artist)] = _unclassified_profile(artist)
            continue
        mix: Counter[str] = Counter()
        for item in classified:
            for style in item["style_mix"]:
                mix[style["style_ref"]] += style["weight"]
        ordered = sorted(mix, key=lambda ref: (-mix[ref], ref))
        catalog[normalized_name(artist)] = {
            "canonical_name": artist, "aliases": [], "classification_status": "classified",
            "confidence": min((item["confidence"] for item in classified), key={"low": 0, "medium": 1, "high": 2}.get),
            "style_mix": [{"style_ref": ref, "role": "primary" if index == 0 else "secondary",
                           "weight": round(mix[ref] / len(classified), 6)} for index, ref in enumerate(ordered)],
            "style_axes": _aggregate_style_axes(classified),
            "summary": f"由本次 Agent 研究的 {len(classified)}/{len(items)} 首曲目聚合，不代表艺人全部作品或音频实测。",
            "boundaries": ["Agent 提供的公开事实尚未独立核验；画像不得解释为喜欢概率。"],
            "sources": list(dict.fromkeys(url for item in classified for url in item["sources"])), "release_overrides": [],
        }
    relations = {}
    for batch in bundle["batches"]:
        for row in batch["artist_relations"]:
            entry = {"canonical_name": row["artist"], "entity_type": row["entity_type"], "aliases": [], "research_origin": "agent"}
            for field in ("lead_vocalists", "related_projects"):
                entry[field] = []
                for fact in row[field]:
                    evidence = [{**item, "verification_result": "unverified"} for item in fact["evidence_items"]]
                    entry[field].append({**fact, "evidence_items": evidence, "sources": list(dict.fromkeys(item["url"] for item in evidence))})
            relations[normalized_name(row["artist"])] = entry
    return assignments, catalog, relations


def _research_relation_catalog(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read relation facts without constructing legacy style/axis estimates."""
    relations = {}
    for batch in bundle["batches"]:
        for row in batch["artist_relations"]:
            entry = {"canonical_name": row["artist"], "entity_type": row["entity_type"],
                     "aliases": [], "research_origin": "agent"}
            for field in ("lead_vocalists", "related_projects"):
                entry[field] = []
                for fact in row[field]:
                    evidence = [{**item, "verification_result": "unverified"} for item in fact["evidence_items"]]
                    entry[field].append({**fact, "evidence_items": evidence,
                                         "sources": list(dict.fromkeys(item["url"] for item in evidence))})
            relations[normalized_name(row["artist"])] = entry
    return relations


def analyze_snapshot(
    snapshot_path: Path,
    *,
    preferred_path: Path,
    relation_path: Path,
    output_path: Path,
    markdown_path: Path | None = None,
    manifest_path: Path | None = None,
    style_taxonomy_path: Path | None = None,
    style_profile_path: Path | None = None,
    policy_path: Path | None = None,
    as_of_date: str | None = None,
    research_bundle_path: Path | None = None,
    analysis_mode: str | None = None,
) -> dict[str, Any]:
    snapshot = validate_playlist_snapshot(read_json(snapshot_path), require_complete=True)
    scoring_date = (
        parse_as_of_date(as_of_date).isoformat() if as_of_date is not None
        else parse_timestamp(snapshot["captured_at"], "captured_at").date().isoformat()
    )
    policy = load_recommendation_policy(policy_path)
    if research_bundle_path is not None and analysis_mode is None:
        # New research results cannot self-certify style URLs. Public source
        # labels must be reconciled by the collector before recommendation.
        analysis_mode = "public_facts_only"
    policy_summary = {
        "mode": "explicit" if policy_path is not None else "default",
        "file_name": policy_path.name if policy_path is not None else None,
        "sha256": stable_hash(policy),
    }
    public_facts = analysis_mode == "public_facts_only"
    preferred_names = [] if research_bundle_path is not None else load_preferred_artists(preferred_path)
    taxonomy_path = style_taxonomy_path or DEFAULT_STYLE_TAXONOMY_PATH
    taxonomy = load_style_taxonomy(taxonomy_path)
    research_assignments = None
    research_bundle = None
    if research_bundle_path is not None:
        research_bundle = validate_research_bundle(read_json(research_bundle_path), snapshot, taxonomy, sha256_path(taxonomy_path))
        if public_facts:
            relation_catalog = _research_relation_catalog(research_bundle)
        else:
            research_assignments, style_catalog, relation_catalog = _compile_research_inputs(research_bundle, snapshot)
        profile_path, profile_catalog_mode = research_bundle_path, "agent_research"
        relation_hash = stable_hash(relation_catalog)
    else:
        relation_catalog = load_relationship_catalog(relation_path)
        relation_hash = sha256_path(relation_path) if relation_path.is_file() else ""
        if public_facts:
            # An identity-only source packet must not load any estimated style
            # catalog, including the legacy example fallback.
            profile_path = style_profile_path or DEFAULT_STYLE_PROFILE_PATH
            profile_catalog_mode = "research"
        else:
            profile_path = resolve_style_profile_path(style_profile_path or DEFAULT_STYLE_PROFILE_PATH)
            profile_catalog_mode = ("example_fallback" if profile_path.name == "artist_style_profiles.example.json"
                                    else "private" if profile_path == DEFAULT_STYLE_PROFILE_PATH else "explicit")
            style_catalog = load_style_profile_catalog(profile_path, taxonomy)

    raw_tracks = snapshot["tracks"]
    resolved_tracks: list[dict[str, Any]] = []
    primary_counter: Counter[str] = Counter()
    credited_counter: Counter[str] = Counter()
    for position, raw_track in enumerate(raw_tracks, 1):
        if research_assignments is not None:
            raw_track = {**raw_track, "position": position}
        raw_artist = normalized_text(raw_track["artist"])
        resolved_artist = resolve_artist(raw_artist, relation_catalog)
        raw_artists = raw_track.get("artists", [raw_artist])
        if not isinstance(raw_artists, list):
            raw_artists = [raw_artist]
        resolved_artists: list[str] = []
        seen_artists: set[str] = set()
        for credited in [
            resolved_artist,
            *[resolve_artist(str(item), relation_catalog) for item in raw_artists],
        ]:
            marker = normalized_name(credited)
            if marker and marker not in seen_artists:
                seen_artists.add(marker)
                resolved_artists.append(credited)
        primary_counter[resolved_artist] += 1
        for credited in resolved_artists:
            credited_counter[credited] += 1
        resolved_tracks.append(_copy_track(raw_track, resolved_artist, resolved_artists))

    preferred_by_marker = {normalized_name(name): name for name in preferred_names}
    entity_names = set(primary_counter) | set(credited_counter) | set(preferred_names)
    entities: list[dict[str, Any]] = []
    analysis_ref_ids: list[str] = []
    for name in sorted(entity_names, key=lambda value: (-primary_counter[value], -credited_counter[value], value.casefold())):
        entity, refs = _entity(
            name,
            primary_count=primary_counter.get(name, 0),
            credited_count=credited_counter.get(name, 0),
            preferred=normalized_name(name) in preferred_by_marker,
            catalog=relation_catalog,
        )
        entities.append(entity)
        for ref in refs:
            if ref not in analysis_ref_ids:
                analysis_ref_ids.append(ref)

    primary_distribution = [
        {
            "rank": rank,
            "artist": name,
            "entity_ref": _artist_ref(name),
            "count": count,
            "share": round(count / len(resolved_tracks), 6) if resolved_tracks else 0,
        }
        for rank, (name, count) in enumerate(
            sorted(primary_counter.items(), key=lambda entry: (-entry[1], entry[0].casefold())),
            1,
        )
    ]
    credited_distribution = [
        {
            "rank": rank,
            "artist": name,
            "entity_ref": _artist_ref(name),
            "count": count,
            "share": round(count / len(resolved_tracks), 6) if resolved_tracks else 0,
        }
        for rank, (name, count) in enumerate(
            sorted(credited_counter.items(), key=lambda entry: (-entry[1], entry[0].casefold())),
            1,
        )
    ]

    preferred = []
    for name in preferred_names:
        resolved = resolve_artist(name, relation_catalog)
        preferred.append(
            {
                "name": resolved,
                "entity_ref": _artist_ref(resolved),
                "source": "preferred_artists.txt",
            }
        )
        if _artist_ref(resolved) not in analysis_ref_ids:
            analysis_ref_ids.append(_artist_ref(resolved))

    if public_facts:
        track_style_assignments = []
        for position, track in enumerate(resolved_tracks, 1):
            track_style_assignments.append({
                "position": position, "track_key": track["track_key"],
                "title": track["title"], "artist": track["artist"], "album": track.get("album", ""),
                "classification_status": "unclassified", "confidence": "low",
                "primary_style_ref": "", "style_refs": [], "style_mix": [],
                "background_style_refs": [], "applied_scope": "unknown",
                "rationale": "尚未取得可核验的公开风格资料", "sources": [], "evidence_items": [],
            })
        artist_style_profiles = [{
            "artist": name, "entity_ref": _artist_ref(name),
            "primary_track_count": primary_counter.get(name, 0),
            "credited_track_count": credited_counter.get(name, 0),
            "is_core_artist": primary_counter.get(name, 0) > 0,
            "classification_status": "unclassified", "confidence": "low",
            "primary_style_ref": "", "style_refs": [], "style_mix": [],
            "summary": "暂无可核验的艺人风格标签", "boundaries": [],
            "sources": [], "evidence_items": [],
        } for name in sorted(entity_names, key=lambda value:
                             (-primary_counter[value], -credited_counter[value], value.casefold()))]
    else:
        track_style_assignments = []
        for track in resolved_tracks:
            assignment = (research_assignments[track["position"]] if research_assignments is not None
                          else _style_assignment(track, _style_profile_for(track["artist"], style_catalog)))
            if research_assignments is not None:
                if assignment["track_key"] != track["track_key"]:
                    assignment["source_track_key"] = assignment["track_key"]
                assignment = {**assignment, "track_key": track["track_key"], "artist": track["artist"]}
            track_style_assignments.append(assignment)
            for style_ref in assignment["style_refs"]:
                if style_ref not in analysis_ref_ids:
                    analysis_ref_ids.append(style_ref)
        artist_style_profiles = [
            _style_profile_output(
                name, _style_profile_for(name, style_catalog),
                primary_count=primary_counter.get(name, 0),
                credited_count=credited_counter.get(name, 0),
            )
            for name in sorted(entity_names, key=lambda value:
                               (-primary_counter[value], -credited_counter[value], value.casefold()))
        ]
    for profile in artist_style_profiles:
        for style_item in profile["style_mix"]:
            if style_item["style_ref"] not in analysis_ref_ids:
                analysis_ref_ids.append(style_item["style_ref"])

    source_track_count = snapshot["track_count"]
    classified_count = sum(
        1 for assignment in track_style_assignments if assignment["classification_status"] == "classified"
    )
    unclassified_count = source_track_count - classified_count
    style_distribution = _style_distribution(
        track_style_assignments,
        taxonomy,
        source_count=source_track_count,
        classified_count=classified_count,
    )
    overlap_style_distribution = build_overlap_style_distribution(
        track_style_assignments,
        taxonomy,
        source_count=source_track_count,
        classified_count=classified_count,
        primary_distribution=style_distribution,
    )
    active_style_refs: list[str] = []
    for assignment in track_style_assignments:
        for style_ref in assignment["style_refs"]:
            if style_ref not in active_style_refs:
                active_style_refs.append(style_ref)
    profile_sha = (stable_hash({"artists": {}}) if public_facts and research_bundle is None
                   else sha256_path(profile_path))
    style_analysis = {
        "taxonomy_version": taxonomy["taxonomy_version"],
        "taxonomy_sha256": sha256_path(taxonomy_path),
        "profile_catalog_sha256": profile_sha,
        "profile_catalog_mode": profile_catalog_mode,
        "known_style_refs": taxonomy["known_style_refs"],
        "active_style_refs": active_style_refs,
        "style_definitions": list(taxonomy["styles"].values()),
        "frequency_basis": "primary_artist_track_count_from_current_snapshot",
        "artist_profile_count": len(artist_style_profiles),
        "core_artist_profile_count": sum(1 for item in artist_style_profiles if item["is_core_artist"]),
        "classified_track_count": classified_count,
        "unclassified_track_count": unclassified_count,
        "style_distribution": style_distribution,
        "overlap_style_distribution": overlap_style_distribution,
        "dominant_style_mix": [
            {
                **item,
                "weight": round(item["count"] / classified_count, 6) if classified_count else 0,
            }
            for item in style_distribution
        ],
        "interest_model": dict(MODEL_CONFIG),
        "interest_profiles": build_interest_profiles(track_style_assignments),
        "artist_profiles": artist_style_profiles,
        "profile_coverage": {
            "required_artist_count": len(artist_style_profiles),
            "classified_artist_count": sum(
                1 for item in artist_style_profiles if item["classification_status"] == "classified"
            ),
            "unclassified_artist_count": sum(
                1 for item in artist_style_profiles if item["classification_status"] == "unclassified"
            ),
            "degraded": profile_catalog_mode == "example_fallback" or unclassified_count > 0 or any(
                item["classification_status"] == "unclassified" for item in artist_style_profiles
            ),
            "classified_track_share": round(classified_count / source_track_count, 6) if source_track_count else 0.0,
            "assignment_scopes": dict(Counter(item["applied_scope"] for item in track_style_assignments)),
        },
    }
    if not public_facts:
        style_analysis["axis_definitions"] = taxonomy["axis_definitions"]
        style_analysis["style_axes"] = _aggregate_style_axes(track_style_assignments)

    # Generation time is metadata; the explicit scoring date is a semantic input.
    identity_payload = {
        "as_of_date": scoring_date,
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_snapshot_input_sha256": snapshot.get("input_sha256", ""),
        "source_platform": snapshot["platform"],
        "source_playlist_id": snapshot.get("playlist_id", ""),
        "source_playlist_name": snapshot.get("playlist_name", ""),
        "preferred_artists": preferred,
        "relationship_catalog_sha256": relation_hash,
        "primary_distribution": primary_distribution,
        "credited_distribution": credited_distribution,
        "entities": entities,
        "favorite_track_keys": [track["track_key"] for track in resolved_tracks],
        "favorite_tracks": resolved_tracks,
        "style_taxonomy_sha256": style_analysis["taxonomy_sha256"],
        "style_profile_catalog_sha256": style_analysis["profile_catalog_sha256"],
        "track_style_assignments": track_style_assignments,
        "style_analysis": style_analysis,
        "recommendation_policy": policy,
        "analysis_mode": analysis_mode,
    }
    analysis_id = f"analysis-{stable_hash(identity_payload)[:20]}"
    packet = {
        "schema_version": SCHEMA_VERSION,
        "packet_type": "musician_analysis",
        "analysis_id": analysis_id,
        "as_of_date": scoring_date,
        "generated_at": utc_now(),
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_snapshot_input_sha256": snapshot.get("input_sha256", ""),
        "source_platform": snapshot["platform"],
        "source_playlist_id": snapshot.get("playlist_id", ""),
        "source_playlist_name": snapshot.get("playlist_name", ""),
        "source_track_count": source_track_count,
        "favorite_track_keys": [track["track_key"] for track in resolved_tracks],
        "favorite_tracks": resolved_tracks,
        "primary_distribution": primary_distribution,
        "credited_distribution": credited_distribution,
        "preferred_artists": preferred,
        "entities": entities,
        "analysis_ref_ids": analysis_ref_ids,
        "track_style_assignments": track_style_assignments,
        "style_analysis": style_analysis,
        "recommendation_policy": policy,
        "analysis_mode": analysis_mode,
        "input_manifest": {
            "snapshot_file_name": snapshot_path.name,
            "snapshot_sha256": sha256_path(snapshot_path),
            "preferred_file_name": preferred_path.name if research_bundle_path is None else None,
            "preferred_sha256": sha256_path(preferred_path) if research_bundle_path is None and preferred_path.is_file() else "",
            "relationship_file_name": research_bundle_path.name if research_bundle_path else relation_path.name,
            "relationship_sha256": relation_hash,
            "style_taxonomy_file_name": taxonomy_path.name,
            "style_taxonomy_sha256": style_analysis["taxonomy_sha256"],
            "style_profile_file_name": profile_path.name,
            "style_profile_sha256": style_analysis["profile_catalog_sha256"],
            "policy": policy_summary,
        },
    }
    if research_bundle is not None:
        packet["analysis_research"] = {
            "bundle_sha256": stable_hash(research_bundle), "snapshot_sha256": research_bundle["snapshot_sha256"],
            "batch_count": len(research_bundle["batches"]), "track_count": source_track_count,
            "evidence_verification": "pending_independent_verification", "publication_status": "draft",
        }
    if analysis_mode == "public_facts_only":
        # The initial Web packet has identities and relations but no verified
        # genre facts yet. In particular it must not expose a synthetic
        # listening-axis estimate while Last.fm requests are pending.
        pending_tags = {"provider": "lastfm", "axis_policy": "removed",
                        "records": [{"track_key": item["track_key"], "evidence": []}
                                    for item in resolved_tracks], "requests": []}
        packet = apply_public_style_evidence(packet, pending_tags)
        analysis_id = packet["analysis_id"]
    validate_analysis_packet(packet)
    write_json(output_path, packet)
    if markdown_path is not None:
        write_markdown(packet, markdown_path)
    if manifest_path is not None:
        write_json(
            manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "manifest_type": "analysis_manifest",
                "analysis_id": analysis_id,
                "as_of_date": scoring_date,
                "source_snapshot_id": snapshot["snapshot_id"],
                "source_track_count": len(resolved_tracks),
                "style_taxonomy_sha256": style_analysis["taxonomy_sha256"],
                "style_profile_catalog_sha256": style_analysis["profile_catalog_sha256"],
                "input_manifest": packet["input_manifest"],
                "policy": policy_summary,
                "output_file_name": output_path.name,
                "generated_at": packet["generated_at"],
            },
        )
    return packet


def write_markdown(packet: dict[str, Any], path: Path) -> None:
    if packet.get("style_analysis", {}).get("evidence_model") == "sourced_tags_v1":
        analysis = packet["style_analysis"]
        counts = analysis["source_coverage"]
        artist_only = packet.get("analysis_mode") == "artist_summary"
        lines = ["# 音乐资料分析", "", f"- 歌单歌曲数：{packet['source_track_count']}",
                 f"- 分析层级：{'仅歌手' if artist_only else '曲目、专辑及艺人背景'}", ""]
        if artist_only:
            lines += [f"- 有来源的歌手：{counts['sourced_artist_count']}/{counts['artist_count']}",
                      f"- 对应主艺人歌曲权重：{counts['weighted_artist_track_count']}/{packet['source_track_count']}",
                      "- 千首及以上不逐曲评价，逐曲分类数不适用。", "", "## 歌手风格分布（按歌单歌曲数加权）", ""]
            lines += [f"- {item['label']}：{item['artist_weighted_track_count']} 首主艺人权重"
                      for item in analysis.get("artist_style_distribution", [])]
        else:
            lines += [f"- 曲目级风格来源：{counts['track_evidence_count']}",
                      f"- 专辑背景资料：{counts['album_background_count']}",
                      f"- 仅艺人背景：{counts['artist_background_count']}",
                      f"- 无可映射风格资料：{counts['no_style_evidence_count']}", "",
                      "## 曲目风格分布（艺人背景单独标注）", ""]
            lines += [f"- {item['label']}：{item['count']} 首"
                      for item in analysis.get("style_distribution", [])]
        lines += ["", "来源 URL 与获取时间详见对应 track_style_assignments / artist_profiles 的 evidence_items。", ""]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    lines = [
        "# 音乐人分析",
        "",
        f"- 分析包：`{packet['analysis_id']}`",
        f"- 来源平台：{packet['source_platform']}",
        f"- 本次歌曲数：{packet['source_track_count']}",
        "- 数量依据：当前 Step 1 快照的动态字段，不使用固定总数",
        f"- 画像来源模式：{packet['style_analysis']['profile_catalog_mode']}；公开事实不等于已独立核验",
        "",
        "## 主艺人分布",
        "",
        "| 排名 | 艺人 | 歌曲数 | 占比 |",
        "| ---: | --- | ---: | ---: |",
    ]
    for item in packet["primary_distribution"]:
        lines.append(f"| {item['rank']} | {item['artist']} | {item['count']} | {item['share']:.2%} |")
    lines.extend(
        [
            "",
            "## 主风格分布（互斥审计）",
            "",
            "| 排名 | 细分风格 | 歌曲数 | 占本次清单 |",
            "| ---: | --- | ---: | ---: |",
        ]
    )
    for item in packet["style_analysis"]["style_distribution"]:
        lines.append(
            f"| {item['rank']} | {item['label']} (`{item['style_ref']}`) | "
            f"{item['count']} | {item['share']:.2%} |"
        )
    lines.extend(
        [
            "",
            "## 风格标签覆盖（前五，可重叠）",
            "",
            "占比为风格命中歌曲数 / 已分类歌曲数，不要求合计 100%。",
            "",
            "| 排名 | 细分风格 | 命中歌曲数 | 标签覆盖率 |",
            "| ---: | --- | ---: | ---: |",
        ]
    )
    classified_count = packet["style_analysis"]["classified_track_count"]
    for item in packet["style_analysis"].get("overlap_style_distribution", [])[:5]:
        lines.append(
            f"| {item['rank']} | {item['label']} (`{item['style_ref']}`) | "
            f"{item['count']} / {classified_count} | {item['classified_share']:.2%} |"
        )
    lines.extend(["", "## 整体听感轴", "", "| 听感轴 | 分数 |", "| --- | ---: |"])
    for axis, score in packet["style_analysis"]["style_axes"].items():
        display = "未知" if score is None else f"{score:.2f}"
        lines.append(f"| {STYLE_AXIS_LABELS.get(axis, axis)} | {display} |")
    lines.extend(["", "## 兴趣分组", ""])
    for interest in packet["style_analysis"].get("interest_profiles", []):
        representatives = "、".join(f"{item['title']} - {item['artist']}" for item in interest["representative_tracks"])
        lines.append(f"- {interest['interest_id']}：{interest['track_count']} 首；平衡后权重 {interest['share']:.1%}；代表曲目：{representatives}")
    lines.extend(["", "## 逐艺人风格画像", ""])
    for profile in packet["style_analysis"]["artist_profiles"]:
        mix = "、".join(
            f"{item['style_ref']}（{item['weight']:.0%}）"
            for item in profile["style_mix"]
        ) or "未分类"
        axis_text = "、".join(
            f"{STYLE_AXIS_LABELS.get(axis, axis)} " + ("未知" if score is None else f"{score:.0f}")
            for axis, score in profile["style_axes"].items()
        )
        lines.append(f"### {profile['artist']}（主艺人歌曲 {profile['primary_track_count']} 首）")
        lines.append(
            f"- 分类：{profile['classification_status']}；置信度：{profile['confidence']}；风格混合：{mix}"
        )
        lines.append(f"- 听感轴：{axis_text}")
        lines.append(f"- 总结：{profile['summary']}")
        lines.append(f"- 边界：{'；'.join(profile['boundaries'])}")
        if profile["sources"]:
            lines.append(f"- 来源：{'；'.join(profile['sources'])}")
        else:
            lines.append("- 来源：暂无达到阈值的公开风格来源")
        lines.append("")
    lines.extend(["", "## 主唱与其他项目", ""])
    for entity in packet["entities"]:
        if entity["relation_status"] == "unmapped":
            continue
        lines.append(f"### {entity['name']}")
        if entity["lead_vocalists"]:
            vocalists = "、".join(
                f"{item['name']}（{item.get('status', '状态未标注')}）"
                for item in entity["lead_vocalists"]
            )
            lines.append(f"- 主唱：{vocalists}")
        if entity["related_projects"]:
            projects = "、".join(
                f"{item['name']}（{item.get('relation', '关联项目')}）"
                for item in entity["related_projects"]
            )
            lines.append(f"- 关联项目：{projects}")
        if entity["sources"]:
            lines.append(f"- 来源：{'；'.join(entity['sources'])}")
        lines.append("")
    lines.extend(["## 未映射艺人", ""])
    unmapped = [entity["name"] for entity in packet["entities"] if entity["relation_status"] == "unmapped"]
    lines.append("、".join(unmapped) if unmapped else "无")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_coverage_report(packet: dict[str, Any], path: Path) -> dict[str, Any]:
    """Write an explicit warning/report artifact for degraded style coverage.

    The artifact records the profile catalog mode and every unclassified
    artist. It is emitted whenever the catalog fell back to the public
    example catalog or any current artist was left unclassified, so a live
    run can surface the degradation instead of hiding it.
    """

    style_analysis = packet["style_analysis"]
    if style_analysis.get("evidence_model") == "sourced_tags_v1":
        counts = style_analysis["source_coverage"]
        total = packet["source_track_count"]
        artist_only = packet.get("analysis_mode") == "artist_summary"
        numerator = (counts["weighted_artist_track_count"] if artist_only else
                     counts["track_evidence_count"] + counts["album_background_count"])
        quality = packet["recommendation_policy"]["analysis_quality"]
        threshold_field = "min_artist_weight_share" if artist_only else "min_track_or_album_share"
        threshold = quality[threshold_field]
        missing_profiles = [profile for profile in style_analysis["artist_profiles"]
                            if profile["classification_status"] == "unclassified"]
        missing_assignments = [item for item in packet["track_style_assignments"]
                               if item["applied_scope"] == "unknown"] if not artist_only else []
        report = {
            "schema_version": SCHEMA_VERSION, "artifact_type": "coverage_report",
            "analysis_id": packet["analysis_id"], "source_snapshot_id": packet["source_snapshot_id"],
            "evidence_model": "sourced_tags_v1", "analysis_mode": packet.get("analysis_mode"),
            "source_coverage": deepcopy(counts), "quality_metric": threshold_field,
            "quality_numerator": numerator, "quality_denominator": total,
            "quality_minimum_share": threshold, "quality_gate_passed": bool(total and numerator / total >= threshold),
            "degraded": bool(total and numerator < total),
            "degraded_reasons": (["artist_sources_missing"] if artist_only else ["track_or_album_sources_missing"])
                                if numerator < total else [],
            "review_queue": ([{"artist": profile["artist"], "status": "needs_public_artist_source",
                               "affected_track_count": profile["primary_track_count"],
                               "required_fields": ["artist_source_url", "retrieved_at", "mapped_style_tags"]}
                              for profile in missing_profiles] if artist_only else
                             [{"track_key": item["track_key"], "title": item["title"], "artist": item["artist"],
                               "status": "needs_public_track_or_album_source",
                               "required_fields": ["source_url", "retrieved_at", "mapped_style_tags"]}
                              for item in missing_assignments]),
            "next_action": ("歌手资料权重不足，不得发布；补充有来源的艺人标签"
                            if artist_only else "曲目/专辑资料不足，不得发布；补充有来源的标签")
                           if total and numerator / total < threshold else "质量门槛已满足；保留来源层级和缺口标注",
        }
        write_json(path, report)
        return report
    coverage = style_analysis["profile_coverage"]
    unclassified = [
        profile["artist"]
        for profile in style_analysis["artist_profiles"]
        if profile["classification_status"] == "unclassified"
    ]
    missing_tracks: dict[str, list[dict[str, Any]]] = {}
    for assignment in packet["track_style_assignments"]:
        if assignment["classification_status"] == "unclassified":
            missing_tracks.setdefault(assignment["artist"], []).append(assignment)
    report = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "coverage_report",
        "analysis_id": packet["analysis_id"],
        "source_snapshot_id": packet["source_snapshot_id"],
        "profile_catalog_mode": style_analysis["profile_catalog_mode"],
        "degraded": coverage["degraded"],
        "degraded_reasons": [
            (
                "example_fallback"
                if style_analysis["profile_catalog_mode"] == "example_fallback"
                else None
            ),
            ("unclassified_artists" if unclassified else None),
            ("unclassified_tracks" if missing_tracks else None),
        ],
        "unclassified_artist_count": len(unclassified),
        "unclassified_artists": sorted(unclassified),
        "classified_artist_count": coverage["classified_artist_count"],
        "required_artist_count": coverage["required_artist_count"],
        "classified_track_share": round(style_analysis["classified_track_count"] / packet["source_track_count"], 6) if packet["source_track_count"] else 0.0,
        "minimum_classified_share": packet["recommendation_policy"].get("analysis_quality", {}).get("min_classified_share", 0.5),
        "review_queue": [
            {"artist": name, "status": "needs_research", "affected_track_count": len(items),
             "tracks": [{key: item.get(key, "") for key in ("track_key", "title", "album")} for item in items],
             "required_fields": ["style_mix", "style_axes", "confidence", "sources"]}
            for name, items in sorted(
                missing_tracks.items(),
                key=lambda entry: (-len(entry[1]), entry[0]),
            ) if items
        ],
        "next_action": (
            ("分析 Agent 仍有证据不足的画像；补充研究后重新聚合，不自动降低门槛"
             if style_analysis["profile_catalog_mode"] == "agent_research"
             else "升级私有画像目录并重新归类未分类艺人；示例目录不可用于生产运行")
            if coverage["degraded"]
            else "画像覆盖率正常"
        ),
    }
    write_json(path, report)
    return report


def analyze_and_validate(
    snapshot_path: Path,
    *,
    preferred_path: Path,
    relation_path: Path,
    output_path: Path,
    markdown_path: Path | None = None,
    manifest_path: Path | None = None,
    style_taxonomy_path: Path | None = None,
    style_profile_path: Path | None = None,
    policy_path: Path | None = None,
    as_of_date: str | None = None,
    research_bundle_path: Path | None = None,
    analysis_mode: str | None = None,
) -> dict[str, Any]:
    packet = analyze_snapshot(
        snapshot_path,
        preferred_path=preferred_path,
        relation_path=relation_path,
        output_path=output_path,
        markdown_path=markdown_path,
        manifest_path=manifest_path,
        style_taxonomy_path=style_taxonomy_path,
        style_profile_path=style_profile_path,
        policy_path=policy_path,
        as_of_date=as_of_date,
        research_bundle_path=research_bundle_path,
        analysis_mode=analysis_mode,
    )
    return validate_analysis_packet(packet)
