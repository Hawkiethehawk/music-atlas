#!/usr/bin/env python3
"""Deterministic Step 2 musician analysis.

The analyzer consumes one completed PlaylistSnapshot and static relationship
facts. It never fetches a platform page, reads recommendation history, or
selects recommendation songs.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from contracts import (
    ContractError,
    SCHEMA_VERSION,
    artist_key,
    normalized_name,
    normalized_text,
    read_json,
    sha256_path,
    stable_hash,
    track_key,
    utc_now,
    validate_playlist_snapshot,
    write_json,
)


DEFAULT_POLICY: dict[str, Any] = {
    "min_recommendations": 10,
    "max_recommendations": 12,
    "max_per_artist": 2,
    "max_per_project": 3,
    "min_projects": 6,
    "exclude_current_favorites": True,
    "cross_platform_links_allowed": True,
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


def _load_json_object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ContractError(f"{path} 必须是 JSON 对象")
    return value


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
            for key in ("name", "role", "status", "relation", "person", "confidence", "sources")
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
    vocalists = _copy_relation_list(entry, "lead_vocalists")
    related_projects = _copy_relation_list(entry, "related_projects")
    sources = _source_urls(entry)
    for fact in (*vocalists, *related_projects):
        for source in fact.get("sources", []):
            if source not in sources:
                sources.append(source)
    relation_status = "confirmed" if vocalists or related_projects else "unmapped"
    refs = [_artist_ref(canonical)]
    for vocalist in vocalists:
        refs.append(_person_ref(vocalist["name"]))
    for project in related_projects:
        refs.append(_project_ref(project["name"]))
    entity = {
        "entity_ref": _artist_ref(canonical),
        "name": canonical,
        "entity_type": normalized_text(entry.get("entity_type") or "unknown"),
        "primary_track_count": primary_count,
        "credited_track_count": credited_count,
        "is_preferred": preferred,
        "relation_status": relation_status,
        "lead_vocalists": vocalists,
        "related_projects": related_projects,
        "sources": sources,
        "analysis_refs": refs,
    }
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


def analyze_snapshot(
    snapshot_path: Path,
    *,
    preferred_path: Path,
    relation_path: Path,
    output_path: Path,
    markdown_path: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    snapshot = validate_playlist_snapshot(read_json(snapshot_path), require_complete=True)
    preferred_names = load_preferred_artists(preferred_path)
    catalog = load_relationship_catalog(relation_path)

    raw_tracks = snapshot["tracks"]
    resolved_tracks: list[dict[str, Any]] = []
    primary_counter: Counter[str] = Counter()
    credited_counter: Counter[str] = Counter()
    for raw_track in raw_tracks:
        raw_artist = normalized_text(raw_track["artist"])
        resolved_artist = resolve_artist(raw_artist, catalog)
        raw_artists = raw_track.get("artists", [raw_artist])
        if not isinstance(raw_artists, list):
            raw_artists = [raw_artist]
        resolved_artists: list[str] = []
        seen_artists: set[str] = set()
        for credited in [resolved_artist, *[resolve_artist(str(item), catalog) for item in raw_artists]]:
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
            catalog=catalog,
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
        resolved = resolve_artist(name, catalog)
        preferred.append(
            {
                "name": resolved,
                "entity_ref": _artist_ref(resolved),
                "source": "preferred_artists.txt",
            }
        )
        if _artist_ref(resolved) not in analysis_ref_ids:
            analysis_ref_ids.append(_artist_ref(resolved))

    # Hash the meaningful inputs only. Timestamp and output paths do not alter
    # the analysis identity, so an agent can verify that it received one packet.
    identity_payload = {
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_snapshot_input_sha256": snapshot.get("input_sha256", ""),
        "preferred_artists": preferred,
        "relationship_catalog_sha256": sha256_path(relation_path) if relation_path.is_file() else "",
        "primary_distribution": primary_distribution,
        "credited_distribution": credited_distribution,
        "entities": entities,
        "favorite_track_keys": [track["track_key"] for track in resolved_tracks],
    }
    analysis_id = f"analysis-{stable_hash(identity_payload)[:20]}"
    packet = {
        "schema_version": SCHEMA_VERSION,
        "packet_type": "musician_analysis",
        "analysis_id": analysis_id,
        "generated_at": utc_now(),
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_snapshot_input_sha256": snapshot.get("input_sha256", ""),
        "source_platform": snapshot["platform"],
        "source_playlist_id": snapshot.get("playlist_id", ""),
        "source_playlist_name": snapshot.get("playlist_name", ""),
        "source_track_count": len(resolved_tracks),
        "favorite_track_keys": [track["track_key"] for track in resolved_tracks],
        "favorite_tracks": resolved_tracks,
        "primary_distribution": primary_distribution,
        "credited_distribution": credited_distribution,
        "preferred_artists": preferred,
        "entities": entities,
        "analysis_ref_ids": analysis_ref_ids,
        "recommendation_policy": DEFAULT_POLICY,
        "input_manifest": {
            "snapshot_file_name": snapshot_path.name,
            "snapshot_sha256": sha256_path(snapshot_path),
            "preferred_file_name": preferred_path.name,
            "preferred_sha256": sha256_path(preferred_path) if preferred_path.is_file() else "",
            "relationship_file_name": relation_path.name,
            "relationship_sha256": sha256_path(relation_path) if relation_path.is_file() else "",
        },
    }
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
                "source_snapshot_id": snapshot["snapshot_id"],
                "source_track_count": len(resolved_tracks),
                "input_manifest": packet["input_manifest"],
                "output_file_name": output_path.name,
                "generated_at": packet["generated_at"],
            },
        )
    return packet


def write_markdown(packet: dict[str, Any], path: Path) -> None:
    lines = [
        "# 音乐人分析",
        "",
        f"- 分析包：`{packet['analysis_id']}`",
        f"- 来源平台：{packet['source_platform']}",
        f"- 本次歌曲数：{packet['source_track_count']}",
        "- 数量依据：当前 Step 1 快照的动态字段，不使用固定总数",
        "",
        "## 主艺人分布",
        "",
        "| 排名 | 艺人 | 歌曲数 | 占比 |",
        "| ---: | --- | ---: | ---: |",
    ]
    for item in packet["primary_distribution"]:
        lines.append(f"| {item['rank']} | {item['artist']} | {item['count']} | {item['share']:.2%} |")
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


def analyze_and_validate(
    snapshot_path: Path,
    *,
    preferred_path: Path,
    relation_path: Path,
    output_path: Path,
    markdown_path: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    packet = analyze_snapshot(
        snapshot_path,
        preferred_path=preferred_path,
        relation_path=relation_path,
        output_path=output_path,
        markdown_path=markdown_path,
        manifest_path=manifest_path,
    )
    from contracts import validate_analysis_packet

    return validate_analysis_packet(packet)
