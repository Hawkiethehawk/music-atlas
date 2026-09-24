"""Scale-aware analysis modes.

Playlists are analyzed at a resolution that matches their size:

- ``track_research`` (<= 200 tracks): per-track agent research, unchanged.
- ``taste_summary`` (201-999): one agent call over the ``歌名+歌手`` list
  (representative title sample above 500 tracks); the agent returns a
  structured taste profile plus an editorial review.
- ``artist_summary`` (>= 1000): one agent call over the artist distribution.

The agent never sees or submits per-track research at these sizes. The
program owns all statistics (duplicates, artist counts, layering), the
style ontology mapping and every contract check; the agent result is bound
to the current snapshot and rejected when it invents artists, titles or
style references.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from analysis_agent import load_style_taxonomy
from analysis_contracts import TASTE_MODES, validate_taste_summary_result
from contracts import (
    ContractError,
    DEFAULT_RECALL_MIX,
    SCHEMA_VERSION,
    artist_key,
    normalized_name,
    parse_timestamp,
    read_json,
    stable_hash,
    track_key,
    utc_now,
    validate_analysis_packet,
    validate_playlist_snapshot,
    write_json,
)
from musician_analyzer import apply_public_style_evidence, load_recommendation_policy

TRACK_RESEARCH_MAX = 200
# 超过这个规模就只统计歌手分布（不含歌名）：逐曲歌名清单既容易触发上游内容
# 审核，也会让 prompt 过长。
TASTE_SUMMARY_MAX = 999
TASTE_BATCH_SIZE = 10
TASTE_TITLE_LIST_FULL_MAX = 500
TASTE_TITLE_SAMPLE_MAX = 300
# 品味/歌手摘要模式的曲目分类覆盖门槛（用户批准的策略值，非逐曲研究的 50%）。
TASTE_MIN_CLASSIFIED_SHARE = 0.3
ROOT = Path(__file__).resolve().parent


def resolve_analysis_mode(track_count: int) -> str:
    """Map snapshot size to the analysis resolution. Never raises for size."""
    if isinstance(track_count, bool) or not isinstance(track_count, int) or track_count < 0:
        raise ContractError(f"快照曲目数无效：{track_count}")
    if track_count <= TRACK_RESEARCH_MAX:
        return "track_research"
    if track_count <= TASTE_SUMMARY_MAX:
        return "taste_summary"
    return "artist_summary"


def _credited_artists(track: dict[str, Any]) -> list[str]:
    names = [track["artist"]]
    for name in track.get("artists", []) or []:
        if name not in names:
            names.append(name)
    return names


def build_tracklist_stats(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Program-owned statistics: uniqueness, duplicates and artist counts."""
    tracks = snapshot["tracks"]
    primary_counter: Counter[str] = Counter()
    credited_counter: Counter[str] = Counter()
    track_counter: Counter[str] = Counter()
    track_meta: dict[str, dict[str, str]] = {}
    for track in tracks:
        primary_counter[track["artist"]] += 1
        for name in _credited_artists(track):
            credited_counter[name] += 1
        key = track_key(track["title"], track["artist"])
        track_counter[key] += 1
        track_meta.setdefault(key, {"title": track["title"], "artist": track["artist"]})
    duplicates = [
        {"title": track_meta[key]["title"], "artist": track_meta[key]["artist"], "count": count}
        for key, count in sorted(
            track_counter.items(),
            key=lambda entry: (-entry[1], normalized_name(entry[1] and track_meta[entry[0]]["title"])),
        )
        if count > 1
    ]
    artist_counts = [
        {"artist": name, "count": count}
        for name, count in sorted(
            credited_counter.items(),
            key=lambda entry: (-entry[1], normalized_name(entry[0])),
        )
    ]
    return {
        "total_rows": len(tracks),
        "unique_tracks": len(track_counter),
        "duplicate_rows": duplicates,
        "artist_counts": artist_counts,
        "primary_counter": dict(primary_counter),
        "credited_counter": dict(credited_counter),
        "track_keys": [track_key(track["title"], track["artist"]) for track in tracks],
    }


def _tracklist_lines(snapshot: dict[str, Any]) -> list[str]:
    lines = []
    for track in snapshot["tracks"]:
        credited = "/".join(_credited_artists(track))
        lines.append(f"歌名:{track['title']};歌手:{credited}")
    return lines


def _representative_tracklist_lines(snapshot: dict[str, Any], stats: dict[str, Any]) -> list[str]:
    """Bound title input while retaining top primary artists and playlist span."""
    tracks = snapshot["tracks"]
    if len(tracks) <= TASTE_TITLE_LIST_FULL_MAX:
        return _tracklist_lines(snapshot)
    selected: set[int] = set()
    primary = sorted(stats["primary_counter"].items(),
                     key=lambda item: (-item[1], normalized_name(item[0])))[:13]
    for artist, _ in primary:
        positions = [index for index, track in enumerate(tracks) if track["artist"] == artist]
        take = min(6, len(positions))
        selected.update(positions[(index * len(positions)) // take] for index in range(take))
    slots = TASTE_TITLE_SAMPLE_MAX - len(selected)
    for slot in range(slots):
        target = ((2 * slot + 1) * len(tracks)) // (2 * slots)
        if target in selected:
            for distance in range(1, len(tracks)):
                left, right = target - distance, target + distance
                if left >= 0 and left not in selected:
                    target = left
                    break
                if right < len(tracks) and right not in selected:
                    target = right
                    break
        selected.add(target)
    return [f"歌名:{tracks[index]['title']};歌手:{'/'.join(_credited_artists(tracks[index]))}"
            for index in sorted(selected)]


def _artist_lines(stats: dict[str, Any], limit: int = 120) -> list[str]:
    """歌手分布清单：按参与曲目数降序，并区分主艺人数量与仅合作数量。

    大歌单可能有上千位歌手，只列出现次数最多的一批，其余归入长尾；
    标注主艺人数量可以避免把“只在合作曲里出现一次”的客串误当作偏好。
    """
    primary = stats.get("primary_counter") or {}
    rows = list(stats["artist_counts"])
    lines = []
    for item in rows[:limit]:
        name = item["artist"]
        primary_count = int(primary.get(name) or 0)
        if primary_count and primary_count < item["count"]:
            lines.append(f"歌手:{name};曲目数:{item['count']};主艺人{primary_count}首")
        elif not primary_count:
            lines.append(f"歌手:{name};曲目数:{item['count']};仅合作")
        else:
            lines.append(f"歌手:{name};曲目数:{item['count']}")
    if len(rows) > limit:
        lines.append(f"（其余 {len(rows) - limit} 位长尾歌手未列出，请在摘要中归入长尾层）")
    return lines


def _priority_primary_artists(stats: dict[str, Any], limit: int = 13) -> str:
    """Small, deterministic research priority from primary-artist frequency.

    Credited-artist frequency includes guests, and neither frequency nor this
    ordering is evidence for a genre or a reference URL.
    """
    rows = sorted(
        stats["primary_counter"].items(),
        key=lambda item: (-item[1], normalized_name(item[0])),
    )[:limit]
    return "\n".join(
        f"{index}. {name}: 主艺人 {count} 首"
        for index, (name, count) in enumerate(rows, 1)
    )


def _style_table(taxonomy: dict[str, Any]) -> list[str]:
    return [
        f"{item['style_ref']}|{item['label']}" for item in taxonomy["styles"].values()
    ]


def _render_template(template: Path, replacements: dict[str, str]) -> str:
    prompt = template.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        prompt = prompt.replace(marker, value)
    for marker in ("{STATISTICS_SUMMARY}", "{LISTING}", "{LISTING_HEADER}", "{STYLE_TABLE}",
                   "{AXES_TABLE}", "{REQUEST_ID}", "{SNAPSHOT_ID}"):
        if marker in prompt:
            raise ContractError(f"品味摘要模板占位符未替换完整：{template.name} 残留 {marker}")
    return prompt


def build_taste_prompt(mode: str, snapshot: dict[str, Any], taxonomy: dict[str, Any],
                       stats: dict[str, Any], *, request_id: str,
                       template_dir: Path | None = None) -> str:
    if mode not in TASTE_MODES:
        raise ContractError(f"未知的品味摘要模式：{mode}")
    base = template_dir or (ROOT / "prompts")
    template = base / f"{mode}.md"
    if mode == "taste_summary":
        lines = _representative_tracklist_lines(snapshot, stats)
        listing = "\n".join(lines)
        if len(snapshot["tracks"]) > TASTE_TITLE_LIST_FULL_MAX:
            listing_header = (f"以下是原歌单顺序中的 {len(lines)}/{len(snapshot['tracks'])} 首代表性曲目"
                              "（非完整歌名清单；只能引用此处确实列出的歌名）：")
        else:
            listing_header = "以下为完整歌名清单（歌名:xxx;歌手:yyy）："
    else:
        listing = "\n".join(_artist_lines(stats))
        listing_header = ("以下为歌手分布清单（歌手:xxx;曲目数:N[;主艺人M首][;仅合作]，"
                          "按参与曲目数降序；主艺人M首表示以该歌手署名的曲目数，仅合作表示只在合作曲里出现）：")
    summary = {
        "total_rows": stats["total_rows"],
        "unique_tracks": stats["unique_tracks"],
        "duplicate_rows": stats["duplicate_rows"],
        # 统计摘要也截断：上千位歌手会把 prompt 撑到 60 KB 以上，
        # 模型输出也容易因此被截断。
        "artist_counts": list(stats["artist_counts"])[:120],
        "artist_count_total": len(stats["artist_counts"]),
    }
    prompt = _render_template(template, {
        "{STATISTICS_SUMMARY}": json.dumps(summary, ensure_ascii=False, indent=1),
        "{LISTING_HEADER}": listing_header,
        "{LISTING}": listing,
        "{STYLE_TABLE}": "\n".join(_style_table(taxonomy)),
        "{REQUEST_ID}": request_id,
        "{SNAPSHOT_ID}": snapshot["snapshot_id"],
    })
    if mode == "taste_summary":
        # A 2,000-track listing can bury the few artists responsible for most
        # primary rows. Put a program-counted priority at the very beginning;
        # it never grants a style classification or a reference URL.
        prompt = (
            "【主艺人研究优先序（程序统计；不是音乐风格或来源证据）】\n"
            + _priority_primary_artists(stats) + "\n"
            "artist_clusters 的 12–15 位艺人先考虑这份主艺人优先序，再留少量位置给"
            "其他风格代表；不要因歌名清单取样而遗漏高频主艺人。"
            "仅对确有可检索来源的场景归属填写 reference_url，不能为了覆盖率编造来源。\n\n"
            + prompt
        )
    return prompt


def _artist_layer(rank: int, total: int) -> str:
    if total <= 10 or rank <= max(1, total // 10):
        return "core"
    if rank <= max(2, total // 3):
        return "active"
    return "longtail"


def _assignment_for_track(track: dict[str, Any], position: int,
                          cluster_by_artist: dict[str, dict[str, Any]]) -> dict[str, Any]:
    key = track_key(track["title"], track["artist"])
    cluster = cluster_by_artist.get(normalized_name(track["artist"]))
    base = {
        "position": position,
        "track_key": key,
        "title": track["title"],
        "artist": track["artist"],
        "album": track.get("album", ""),
        "classification_status": "unclassified",
        "confidence": "low",
        "primary_style_ref": "",
        "style_refs": [],
        "style_mix": [],
        "applied_scope": "taste_unknown",
        "rationale": "品味摘要未覆盖该艺人，保留未知，不做猜测。",
        "sources": [],
        "evidence_items": [],
        "field_provenance": {
            field: {"scope": "unknown", "confidence": "low", "sources": [], "origin": "taste_summary",
                    "verification_scope": "pending_independent_verification"}
            for field in ("style_mix",)
        },
    }
    if not cluster:
        return base
    refs = list(cluster["style_refs"])
    return {
        **base,
        "classification_status": "classified",
        "confidence": cluster["confidence"],
        "primary_style_ref": refs[0],
        "style_refs": refs,
        "style_mix": [{"style_ref": ref, "role": "primary" if index == 0 else "secondary",
                       "weight": 1.0 / len(refs)} for index, ref in enumerate(refs)],
        "applied_scope": "taste_artist",
        "rationale": cluster["scene"],
        "sources": [cluster["reference_url"]] if cluster["reference_url"] else [],
    }


def map_taste_to_packet(snapshot: dict[str, Any], bundle: dict[str, Any], taxonomy: dict[str, Any],
                        *, taxonomy_path: Path, policy: dict[str, Any],
                        policy_path: Path | None = None,
                        source_playlist_track_count: int | None = None) -> dict[str, Any]:
    """Compile a validated taste bundle into the standard analysis packet.

    The packet keeps ``packet_type: musician_analysis`` so the recommendation
    stage, offline validation and the web view model continue to work; the
    per-track style facts are derived from artist-level cluster assignments
    and are marked ``origin: taste_summary`` everywhere.
    """
    tracks = snapshot["tracks"]
    mode = bundle["analysis_mode"]
    stats = build_tracklist_stats(snapshot)
    # Agent 写入一个网页地址不证明该网页在本次被读取，更不能给曲目分类。
    # 真正的归属只在 apply_public_style_evidence 中由平台实际返回的标签决定。
    cluster_by_artist: dict[str, dict[str, Any]] = {}
    display_clusters = {
        normalized_name(cluster["artist"]): cluster for cluster in bundle["artist_clusters"]
    }
    assignments = [
        _assignment_for_track(track, index, cluster_by_artist)
        for index, track in enumerate(tracks)
    ]

    entities = []
    profiles = []
    analysis_ref_ids: list[str] = []
    # 同名艺人变体（大小写/空格差异）按 artist_key 归并计数，
    # 保留最早出现的原始写法作为显示名，避免重复 entity_ref。
    primary_counter: Counter[str] = Counter()
    primary_display: dict[str, str] = {}
    credited_counter: Counter[str] = Counter()
    credited_display: dict[str, str] = {}
    for track in tracks:
        key = artist_key(track["artist"])
        primary_counter[key] += 1
        primary_display.setdefault(key, track["artist"])
        for name in _credited_artists(track):
            ckey = artist_key(name)
            credited_counter[ckey] += 1
            credited_display.setdefault(ckey, name)
    for key in sorted(credited_counter, key=lambda value: (-credited_counter[value], value)):
        name = credited_display[key]
        marker = normalized_name(name)
        cluster = cluster_by_artist.get(marker)
        refs = list(cluster["style_refs"]) if cluster else []
        entity_ref = f"artist:{key}"
        entity = {
            "entity_ref": entity_ref,
            "name": name,
            "aliases": [],
            "entity_type": "unknown",
            "primary_track_count": primary_counter.get(key, 0),
            "credited_track_count": credited_counter.get(key, 0),
            "is_preferred": False,
            "relation_status": "unmapped",
            "lead_vocalists": [],
            "related_projects": [],
            "sources": [],
            "analysis_refs": [entity_ref] + refs,
        }
        entities.append(entity)
        if entity_ref not in analysis_ref_ids:
            analysis_ref_ids.append(entity_ref)
        for ref in refs:
            if ref not in analysis_ref_ids:
                analysis_ref_ids.append(ref)

        classified = bool(cluster)
        display = display_clusters.get(marker)
        profile = {
            "artist": name,
            "entity_ref": entity["entity_ref"],
            "primary_track_count": primary_counter.get(key, 0),
            "credited_track_count": credited_counter.get(key, 0),
            "is_core_artist": primary_counter.get(key, 0) > 0,
            "classification_status": "classified" if classified else "unclassified",
            "confidence": display["confidence"] if display else "low",
            "primary_style_ref": refs[0] if refs else "",
            "style_mix": [{"style_ref": ref, "role": "primary" if index == 0 else "secondary",
                           "weight": 1.0 / len(refs)} for index, ref in enumerate(refs)],
            "style_weights": {ref: 1.0 / len(refs) for ref in refs},
            "summary": display["scene"] if display else "品味摘要未覆盖该艺人。",
            "boundaries": [],
            "sources": [cluster["reference_url"]] if classified else [],
        }
        profiles.append(profile)

    primary_distribution = [
        {"rank": rank, "artist": primary_display[key], "entity_ref": f"artist:{key}", "count": count,
         "share": round(count / len(tracks), 6) if tracks else 0}
        for rank, (key, count) in enumerate(
            sorted(primary_counter.items(), key=lambda entry: (-entry[1], entry[0])), 1)
    ]
    credited_distribution = [
        {"rank": rank, "artist": credited_display[key], "entity_ref": f"artist:{key}", "count": count}
        for rank, (key, count) in enumerate(
            sorted(credited_counter.items(), key=lambda entry: (-entry[1], entry[0])), 1)
    ]

    classified_tracks = sum(1 for item in assignments if item["classification_status"] == "classified")
    distribution_counter = Counter(
        item["primary_style_ref"] for item in assignments
        if item["classification_status"] == "classified" and item["primary_style_ref"]
    )
    style_distribution = [
        {"style_ref": ref, "count": count,
         "weight": round(count / classified_tracks, 6) if classified_tracks else 0.0}
        for ref, count in sorted(distribution_counter.items(), key=lambda entry: (-entry[1], entry[0]))
    ]
    dominant_style_mix = [
        {**item, "weight": round(count / classified_tracks, 6) if classified_tracks else 0.0}
        for ref, count in sorted(distribution_counter.items(), key=lambda entry: (-entry[1], entry[0]))
        for item in [{"style_ref": ref, "count": count}]
    ]
    classified_artists = sum(1 for item in profiles if item["classification_status"] == "classified")

    style_analysis = {
        "taxonomy_version": taxonomy["taxonomy_version"],
        "taxonomy_sha256": stable_hash(taxonomy),
        "profile_catalog_sha256": stable_hash({"artist_clusters": bundle["artist_clusters"]}),
        "profile_catalog_mode": "taste_summary",
        "analysis_mode": mode,
        "profile_coverage": {
            "required_artist_count": len(profiles),
            "classified_artist_count": classified_artists,
            "unclassified_artist_count": len(profiles) - classified_artists,
            "degraded": classified_artists < len(profiles),
            "classified_track_share": round(classified_tracks / len(tracks), 6) if tracks else 0.0,
            "assignment_scopes": dict(Counter(item["applied_scope"] for item in assignments)),
        },
        "known_style_refs": taxonomy["known_style_refs"],
        "active_style_refs": sorted(distribution_counter),
        "style_definitions": list(taxonomy["styles"].values()),
        "frequency_basis": "primary_artist_track_count_from_current_snapshot",
        "artist_profiles": profiles,
        "artist_profile_count": len(profiles),
        "core_artist_profile_count": sum(1 for item in profiles if item["is_core_artist"]),
        "classified_track_count": classified_tracks,
        "unclassified_track_count": len(tracks) - classified_tracks,
        "style_distribution": style_distribution,
        "dominant_style_mix": dominant_style_mix,
    }

    identity_payload = {
        "mode": mode,
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_snapshot_input_sha256": snapshot.get("input_sha256", ""),
        "as_of_date": snapshot.get("captured_at", "")[:10],
        "taste_bundle_sha256": stable_hash(bundle),
        "primary_distribution": primary_distribution,
        "style_analysis_refs": style_analysis["active_style_refs"],
    }
    packet = {
        "schema_version": SCHEMA_VERSION,
        "packet_type": "musician_analysis",
        "analysis_id": f"analysis-{stable_hash(identity_payload)[:20]}",
        "analysis_mode": mode,
        "taste_summary": bundle,
        "as_of_date": snapshot.get("captured_at", "")[:10],
        "generated_at": utc_now(),
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_snapshot_input_sha256": snapshot.get("input_sha256", ""),
        "source_platform": snapshot["platform"],
        "source_playlist_id": snapshot.get("playlist_id", ""),
        "source_playlist_name": snapshot.get("playlist_name", ""),
        "source_track_count": len(tracks),
        "source_playlist_track_count": source_playlist_track_count or len(tracks),
        "favorite_track_keys": stats["track_keys"],
        "favorite_tracks": [
            {"track_key": key, "title": track["title"], "artist": track["artist"],
             "album": track.get("album", "")}
            for key, track in zip(stats["track_keys"], tracks)
        ],
        "primary_distribution": primary_distribution,
        "credited_distribution": credited_distribution,
        "preferred_artists": [],
        "entities": entities,
        "analysis_ref_ids": analysis_ref_ids,
        "track_style_assignments": assignments,
        "style_analysis": style_analysis,
        "recommendation_policy": policy,
        "input_manifest": {
            "snapshot_file_name": "snapshot.json",
            "mode": mode,
            "taste_bundle_sha256": stable_hash(bundle),
            "policy": {"mode": "explicit" if policy_path is not None else "default"},
        },
    }
    if mode != "track_research":
        # 品味/歌手摘要没有关系研究：程序独占地调整策略副本——
        # 1) 召回配额限制到有依据的类型（不让推荐 Skill 虚构音乐人关系候选），
        #    剩余配额按原比例归一化；
        # 2) 覆盖门槛降为 30%（用户 2026-09-11 批准）：真实验证显示场景归属
        #    带可检索来源的覆盖率约 43-44%，50% 的逐曲门槛对摘要模式过严。
        policy = deepcopy(policy)
        retained = [(kind, ratio) for kind, ratio in DEFAULT_RECALL_MIX if kind != "musician_relation"]
        total = sum(ratio for _, ratio in retained)
        policy["recall_mix"] = [
            {"candidate_type": kind, "target_ratio": round(ratio / total, 6)}
            for kind, ratio in retained
        ]
        policy["analysis_quality"] = {"min_classified_share": TASTE_MIN_CLASSIFIED_SHARE}
        packet["recommendation_policy"] = policy
    # 先输出带完整身份与统计的未取证包；Web 在公开标签返回后再次调用
    # apply_public_style_evidence，再过质量门槛。不存在 Agent URL 驱动的
    # 临时“已分类”状态，也不把八轴字段带入新分析包。
    pending_tags = {"provider": "lastfm", "axis_policy": "removed",
                    "records": [{"track_key": track["track_key"], "evidence": []}
                                for track in packet["favorite_tracks"]], "requests": []}
    return apply_public_style_evidence(packet, pending_tags)


def build_sourced_summary_packet(snapshot: dict[str, Any], taxonomy: dict[str, Any], *,
                                 taxonomy_path: Path, source_tags: dict[str, Any] | None = None,
                                 policy_path: Path | None = None,
                                 source_playlist_track_count: int | None = None) -> dict[str, Any]:
    """Build 201+ song analysis from public evidence, without a style-guessing Agent.

    Call once without source_tags to obtain the bounded discovery input, then
    collect public tags and call apply_public_style_evidence on that packet.
    A supplied source_tags result is immediately reconciled here. The large
    mode contains no per-song research even when per-track records are present.
    """
    snapshot = validate_playlist_snapshot(snapshot, require_complete=True)
    playlist_count = source_playlist_track_count or (snapshot.get("reader") or {}).get(
        "source_track_count") or snapshot["track_count"]
    if isinstance(playlist_count, bool) or not isinstance(playlist_count, int) or playlist_count < snapshot["track_count"]:
        raise ContractError("原始歌单规模不能小于本次处理数量")
    mode = resolve_analysis_mode(playlist_count)
    if mode == "track_research":
        raise ContractError("逐曲模式使用 analyze_snapshot，不走歌手摘要构造器")
    # This is a structural shell only. The programme supplies all counts;
    # no model-internal claim, URL, synthetic mood, or false genre passes into
    # the final packet. Legacy mapper discards its temporary axis placeholders
    # before validation through apply_public_style_evidence.
    bundle = {
        "analysis_mode": mode,
        "artist_clusters": [],
        "taste_profile": {},
        "editorial_review": {"headline": "资料待核验", "review": "", "inner_world": "", "humor_notes": []},
        "style_tags": [],
        "knowledge_basis": {"model_internal": "未采用", "web_verified": "由来源记录单独核验", "inference": "未采用"},
        "limitations": [], "uncertainties": [],
    }
    packet = map_taste_to_packet(snapshot, bundle, taxonomy, taxonomy_path=taxonomy_path,
                                 policy=load_recommendation_policy(policy_path), policy_path=policy_path,
                                 source_playlist_track_count=playlist_count)
    if source_tags is not None:
        packet = apply_public_style_evidence(packet, source_tags)
    return packet


def attach_sourced_editorial(packet: dict[str, Any]) -> dict[str, Any]:
    """Describe only the public facts actually reconciled into this packet.

    This is deliberately deterministic. A generated URL or an Agent's prior
    knowledge cannot become a source-backed style claim. Interest membership
    is a presentation grouping and never increases the evidence coverage.
    """
    from agent_lastfm import ABSTRACT_ISLAND_NAMES

    result = deepcopy(packet)
    style = result["style_analysis"]
    if style.get("evidence_model") != "sourced_tags_v1":
        raise ContractError("来源文案只能使用已核对的公开资料分析包")
    mode = result["analysis_mode"]
    if mode not in TASTE_MODES:
        raise ContractError("来源摘要只适用于品味或歌手模式")
    total = result["source_track_count"]
    coverage = style["source_coverage"]
    definitions = {row["style_ref"]: row["label"] for row in style["style_definitions"]}
    if mode == "artist_summary":
        distribution = style.get("artist_style_distribution", [])
        count_field = "artist_weighted_track_count"
        top = [row["style_ref"] for row in distribution[:3]]
        labels = "、".join(definitions[ref] for ref in top) or "尚无可映射标签"
        summary = (
            f"这份歌单共 {total} 首，按规模规则只分析歌手，不分析单曲。"
            f"本次取回可映射公开标签的歌手 {coverage['sourced_artist_count']}/{coverage['artist_count']} 位，"
            f"按他们在歌单中的主艺人曲目数加权，覆盖 {coverage['weighted_artist_track_count']}/{total} 首。"
            f"有来源的歌手风格标签中，较常见的是{labels}；这是艺人层级的资料，"
            "不等同于其中每首作品的风格、声音特征或听者偏好强度。未取得标签的歌手保持未知。"
        )
        artist_refs = {
            normalized_name(profile["artist"]): profile.get("style_refs") or []
            for profile in style["artist_profiles"]
        }
        refs_by_track = [artist_refs.get(normalized_name(track["artist"]), [])
                         for track in result["favorite_tracks"]]
    else:
        distribution = style.get("style_distribution", [])
        count_field = "count"
        top = [row["style_ref"] for row in distribution[:3]]
        labels = "、".join(definitions[ref] for ref in top) or "尚无可映射标签"
        summary = (
            f"这份歌单共 {total} 首，本次可核对的曲目标签覆盖 {coverage['track_evidence_count']} 首；"
            f"另有 {coverage['album_background_count']} 首仅有专辑背景，"
            f"{coverage['artist_background_count']} 首仅有艺人背景。"
            f"已核对的单曲风格标签中，较常见的是{labels}。"
            "专辑资料只描述所属发行，艺人资料只描述艺人，均不能冒充单曲听感。"
            "没有取得可靠标签的部分保留未知；兴趣分组仅整理已有来源，不增加风格证据覆盖。"
        )
        refs_by_track = [row.get("style_refs") or [] for row in result["track_style_assignments"]]
    if len(summary) > 300:
        raise ContractError("来源摘要超过文案保护上限")

    # Reserve the last island for an explicit evidence gap when needed. For
    # artist-only mode the unqueried artists are omitted rather than assigned
    # a fabricated song-level style. Smaller lists retain the gap group so
    # every source row still has a stable display location.
    has_unknown = any(not refs for refs in refs_by_track)
    style_slots = 2 if has_unknown else 3
    lead_refs = top[:style_slots]
    record_groups: list[list[int]] = [[], [], []]
    for index, refs in enumerate(refs_by_track):
        if not refs:
            if mode != "artist_summary":
                record_groups[2].append(index)
            continue
        matches = [slot for slot, ref in enumerate(lead_refs) if ref in refs]
        # A less frequent label goes to the catch-all group, not under a
        # dominant label it never matched.
        slot = matches[0] if matches else 2
        record_groups[slot].append(index)

    islands = []
    for slot, name in enumerate(ABSTRACT_ISLAND_NAMES):
        ref = lead_refs[slot] if slot < len(lead_refs) else None
        if ref:
            description = (f"这一组的可核对公开标签包含{definitions[ref]}；"
                           "分组仅反映已取得的标签，未标记条目不据此作风格判断。")
        else:
            description = ("这一组收纳其他已核对的公开标签及资料缺口；"
                           "无来源的条目仅作位置记录，不据此推断作品或艺人的声音。")
        ids = record_groups[slot]
        islands.append({"id": f"agent-island-{slot + 1}", "name": name,
                        "summary": description, "record_ids": ids})

    tags = []
    denominator = max(1, coverage["weighted_artist_track_count"] if mode == "artist_summary"
                      else style["classified_track_count"])
    for row in distribution[:12]:
        ref = row["style_ref"]
        matched = [profile["artist"] for profile in style["artist_profiles"]
                   if ref in (profile.get("style_refs") or [])]
        tags.append({"tag": ref, "weight": min(100, round(row[count_field] / denominator * 100)),
                     "matched_artists": matched[:8]})
    review = {"headline": "有来源的风格轮廓", "review": summary,
              "inner_world": "资料只说明公开标签能支持的范围；缺口保持未知。", "humor_notes": []}
    result["taste_summary"] = {
        "analysis_mode": mode, "generated_at": utc_now(), "overall_summary": summary,
        "islands": [{"name": island["name"], "summary": island["summary"],
                     "artists": list(dict.fromkeys(result["favorite_tracks"][rid]["artist"]
                                                    for rid in island["record_ids"]))}
                    for island in islands],
        "style_tags": tags, "editorial_review": review,
        "knowledge_basis": {"model_internal": "未采用", "web_verified": "本次实际获取的公开标签",
                            "inference": "仅统计与分组"},
        "limitations": ["资料未覆盖的对象保持未知；专辑与艺人标签不是单曲事实。"],
    }
    result["agent_islands"] = islands
    result["agent_copy_version"] = 1
    result["overall_summary"] = summary
    return validate_analysis_packet(result)


def _request_id(snapshot: dict[str, Any], mode: str) -> str:
    return f"{mode}-{stable_hash({'snapshot_id': snapshot['snapshot_id'], 'input_sha256': snapshot.get('input_sha256', ''), 'mode': mode})[:16]}"


def _normalize_style_tags(bundle: Any, taxonomy: dict[str, Any]) -> None:
    """把 Agent 的 style_tags 规范到契约范围（1–12 条、只引用风格本体）。

    Agent 常把 style_tags 留空或写超条数；一个可用的画像不应该因为这一个字段
    被整体拒绝，所以优先按艺人聚类聚合补齐。
    """
    if not isinstance(bundle, dict):
        return
    known = set(taxonomy.get("known_style_refs") or [])
    clusters = [item for item in (bundle.get("artist_clusters") or []) if isinstance(item, dict)]
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (bundle.get("style_tags") or []):
        if not isinstance(item, dict):
            continue
        ref = item.get("tag")
        if not isinstance(ref, str) or ref not in known or ref in seen:
            continue
        seen.add(ref)
        cleaned.append(item)
    if 1 <= len(cleaned) <= 12:
        bundle["style_tags"] = cleaned
        return
    counts: dict[str, int] = {}
    artists_by_ref: dict[str, list[str]] = {}
    for cluster in clusters:
        artist = cluster.get("artist")
        for ref in (cluster.get("style_refs") or []):
            if ref in known:
                counts[ref] = counts.get(ref, 0) + 1
                if isinstance(artist, str) and artist:
                    artists_by_ref.setdefault(ref, []).append(artist)
    total = max(1, len(clusters))
    bundle["style_tags"] = [
        {"tag": ref, "weight": min(100, max(1, int(round(count / total * 100)))),
         "matched_artists": list(dict.fromkeys(artists_by_ref.get(ref) or []))[:8]}
        for ref, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]
    ]


def _normalize_summary_text(bundle: Any) -> None:
    """overall_summary 超长时截断、过短时用锐评正文兜底：一次重试约花 1–2 分钟，不值得。"""
    if not isinstance(bundle, dict):
        return
    text = bundle.get("overall_summary")
    if not isinstance(text, str):
        return
    text = text.strip()
    if len(text) > 300:
        cut = text[:300]
        for mark in ("。", "；", "，", " "):
            index = cut.rfind(mark)
            if index >= 160:
                cut = cut[:index + 1]
                break
        text = cut
    if len(text) < 80:
        review = bundle.get("editorial_review") if isinstance(bundle.get("editorial_review"), dict) else {}
        fallback = str(review.get("review") or review.get("headline") or "").strip()
        if len(fallback) >= 80:
            text = fallback[:300]
    bundle["overall_summary"] = text


def _normalize_clusters(bundle: Any, taxonomy: dict[str, Any]) -> None:
    """只保留有有效本体引用的艺人聚类，不凭空补风格。

    契约要求每个聚类至少一个本体引用；模型偶尔会写出不存在的 style:... 引用，
    无法映射的艺人保持未知；若整包没有有效聚类，交给契约校验触发重试。
    """
    if not isinstance(bundle, dict):
        return
    known_set = set(taxonomy.get("known_style_refs") or [])
    cleaned = []
    for cluster in (bundle.get("artist_clusters") or []):
        if not isinstance(cluster, dict):
            continue
        refs = [ref for ref in (cluster.get("style_refs") or [])
                if isinstance(ref, str) and ref in known_set]
        if refs:
            cluster["style_refs"] = list(dict.fromkeys(refs))
            cleaned.append(cluster)
    bundle["artist_clusters"] = cleaned


def _normalize_semantic_themes(bundle: Any, snapshot: dict[str, Any]) -> None:
    """Discard unsupported theme references without inventing replacement songs.

    A theme with fewer than three playlist songs has insufficient evidence and
    is discarded. The contract still rejects fewer than two supported themes.
    """
    if not isinstance(bundle, dict) or not isinstance(bundle.get("semantic_themes"), list):
        return
    title_keys = {
        normalized_name(track["title"])
        for track in snapshot.get("tracks", [])
        if isinstance(track, dict) and isinstance(track.get("title"), str)
    }
    themes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for theme in bundle["semantic_themes"]:
        if not isinstance(theme, dict) or not isinstance(theme.get("tracks"), list):
            continue
        marker = normalized_name(theme.get("theme"))
        if not marker or marker in seen:
            continue
        supported = [
            title for title in theme["tracks"]
            if isinstance(title, str) and normalized_name(title) in title_keys
        ]
        if len(supported) < 3:
            continue
        themes.append({**theme, "tracks": supported})
        seen.add(marker)
    if len(themes) >= 2:
        bundle["semantic_themes"] = themes[:6]


def run_taste_analysis(snapshot_path: Path, taxonomy_path: Path, directory: Path, *,
                       command: str | None, timeout: int = 600,
                       execute: Callable[..., dict] | None = None,
                       progress: Callable[[dict[str, Any]], None] | None = None,
                       policy_path: Path | None = None,
                       template_dir: Path | None = None) -> tuple[dict[str, Any], Path]:
    """Run the scale-appropriate single-call analysis and return (packet, bundle_path)."""
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ContractError("analysis-timeout 必须为正整数秒数")
    if execute is None:
        from skill_runner import run_external_skill
        execute = run_external_skill
    snapshot = validate_playlist_snapshot(read_json(snapshot_path))
    mode = resolve_analysis_mode(snapshot["track_count"])
    if mode == "track_research":
        raise ContractError(
            f"快照仅 {snapshot['track_count']} 首，应使用逐曲研究（execute_analysis_research），而不是品味摘要")
    taxonomy = load_style_taxonomy(taxonomy_path)
    stats = build_tracklist_stats(snapshot)
    request_id = _request_id(snapshot, mode)
    prompt = build_taste_prompt(mode, snapshot, taxonomy, stats, request_id=request_id, template_dir=template_dir)

    directory.mkdir(parents=True, exist_ok=True)
    bundle_path = directory / "taste_summary.json"
    prompt_path = directory / "taste_prompt.md"
    manifest_path = directory / "taste_manifest.json"
    prompt_path.write_text(prompt, encoding="utf-8")

    if bundle_path.is_file():
        cached = read_json(bundle_path)
        try:
            bundle = validate_taste_summary_result(cached, snapshot=snapshot, taxonomy=taxonomy, mode=mode)
            if bundle.get("request_id") != request_id:
                raise ContractError("品味摘要缓存与当前快照不匹配")
        except ContractError:
            bundle = None
        else:
            bundle = bundle
    else:
        bundle = None

    if bundle is None:
        if not command or not str(command).strip():
            raise ContractError("品味摘要模式需要配置分析执行器（--analysis-command）")
        if progress:
            progress({"event": "task_started", "stage": "analysis", "task_kind": mode,
                      "task_id": f"taste-{mode}", "task_index": 1, "task_total": 1,
                      "task_status": "running", "mode": mode,
                      "track_completed": 0, "track_total": snapshot["track_count"],
                      "message": f"按歌单规模采用{'品味摘要' if mode == 'taste_summary' else '歌手摘要'}模式（单任务）"})
        # 大歌单的摘要响应可达数十 KB，模型偶尔会截断或偏离结构；保留每次原始
        # 响应并允许两轮修复，避免一次差输出直接判负整个任务。
        bundle = None
        last_error: Exception | None = None
        deadline = time.monotonic() + timeout
        for attempt in range(1, 4):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise ContractError("品味摘要重试的阶段时间预算已耗尽")
            raw = execute(command, prompt, timeout=max(1, math.ceil(remaining)))
            write_json(directory / f"taste_response_{attempt}.json", raw if isinstance(raw, (dict, list)) else {"raw": str(raw)[:2000]})
            _normalize_clusters(raw, taxonomy)
            _normalize_style_tags(raw, taxonomy)
            _normalize_summary_text(raw)
            if mode == "taste_summary":
                _normalize_semantic_themes(raw, snapshot)
            try:
                bundle = validate_taste_summary_result(raw, snapshot=snapshot, taxonomy=taxonomy, mode=mode)
                break
            except ContractError as error:
                last_error = error
                if attempt == 3:
                    break
                if progress:
                    progress({"event": "stage_detail", "stage": "analysis", "mode": mode,
                              "message": f"摘要校验未通过，正在重试（第 {attempt} 次）：{error}"})
        if bundle is None:
            raise ContractError(f"摘要响应连续 3 次未通过契约校验：{last_error}")
        bundle["request_id"] = request_id
        write_json(bundle_path, bundle)
        if progress:
            progress({"event": "task_completed", "stage": "analysis", "task_kind": mode,
                      "task_id": f"taste-{mode}", "task_index": 1, "task_total": 1,
                      "task_status": "validated", "mode": mode,
                      "completed": 1, "total": 1,
                      "track_completed": snapshot["track_count"], "track_total": snapshot["track_count"],
                      "message": "品味摘要已通过契约校验"})
    else:
        if progress:
            progress({"event": "task_completed", "stage": "analysis", "task_kind": mode,
                      "task_id": f"taste-{mode}", "task_index": 1, "task_total": 1,
                      "task_status": "validated", "mode": mode, "reused_result": True,
                      "message": "复用已完成并通过校验的品味摘要"})

    write_json(manifest_path, {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "taste_summary_manifest",
        "mode": mode,
        "request_id": request_id,
        "source_snapshot_id": snapshot["snapshot_id"],
        "snapshot_sha256": stable_hash(snapshot),
        "taxonomy_sha256": stable_hash(taxonomy),
        "prompt_characters": len(prompt),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_path": str(prompt_path),
        "bundle_path": str(bundle_path),
        "timeout_seconds": timeout,
        "template": f"prompts/{mode}.md",
        "generated_at": utc_now(),
    })

    policy = load_recommendation_policy(policy_path)
    packet = map_taste_to_packet(snapshot, bundle, taxonomy, taxonomy_path=taxonomy_path, policy=policy,
                                 policy_path=policy_path)
    return packet, bundle_path


def summarize_review(bundle: dict[str, Any]) -> dict[str, Any]:
    """Read-only projection for the web view model."""
    parse_timestamp(bundle["generated_at"], "品味摘要 generated_at")
    review = bundle["editorial_review"]
    return {
        "mode": bundle["analysis_mode"],
        "headline": review["headline"],
        "review": review["review"],
        "inner_world": review["inner_world"],
        "humor_notes": [dict(note) for note in review["humor_notes"]],
        "style_tags": [
            {"tag": item["tag"], "weight": item["weight"], "matched_artists": list(item["matched_artists"])}
            for item in bundle["style_tags"]
        ],
        "knowledge_basis": dict(bundle["knowledge_basis"]),
        "limitations": list(bundle["limitations"]),
    }
