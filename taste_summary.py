"""Scale-aware analysis modes.

Playlists are analyzed at a resolution that matches their size:

- ``track_research`` (<= 30 tracks): per-track agent research, unchanged.
- ``taste_summary`` (31-500): one agent call over the ``歌名+歌手`` list; the
  agent returns a structured taste profile plus an editorial review.
- ``artist_summary`` (>= 501): one agent call over the artist distribution.

The agent never sees or submits per-track research at these sizes. The
program owns all statistics (duplicates, artist counts, layering), the
style ontology mapping and every contract check; the agent result is bound
to the current snapshot and rejected when it invents artists, titles or
style references.
"""

from __future__ import annotations

import hashlib
import json
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
from musician_analyzer import load_recommendation_policy

TRACK_RESEARCH_MAX = 30
TASTE_SUMMARY_MAX = 500
TASTE_BATCH_SIZE = 10
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


def _artist_lines(stats: dict[str, Any]) -> list[str]:
    return [f"歌手:{item['artist']};曲目数:{item['count']}" for item in stats["artist_counts"]]


def _style_table(taxonomy: dict[str, Any]) -> list[str]:
    return [
        f"{item['style_ref']}|{item['label']}" for item in taxonomy["styles"].values()
    ]


def _axes_table(taxonomy: dict[str, Any]) -> list[str]:
    return [f"{code}|{label}" for code, label in taxonomy["axis_definitions"].items()]


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
        listing = "\n".join(_tracklist_lines(snapshot))
        listing_header = "以下为完整清单（歌名:xxx;歌手:yyy）："
    else:
        listing = "\n".join(_artist_lines(stats))
        listing_header = "以下为歌手分布清单（歌手:xxx;曲目数:N，按曲目数降序）："
    summary = {
        "total_rows": stats["total_rows"],
        "unique_tracks": stats["unique_tracks"],
        "duplicate_rows": stats["duplicate_rows"],
        "artist_counts": stats["artist_counts"],
    }
    return _render_template(template, {
        "{STATISTICS_SUMMARY}": json.dumps(summary, ensure_ascii=False, indent=1),
        "{LISTING_HEADER}": listing_header,
        "{LISTING}": listing,
        "{STYLE_TABLE}": "\n".join(_style_table(taxonomy)),
        "{AXES_TABLE}": "\n".join(_axes_table(taxonomy)),
        "{REQUEST_ID}": request_id,
        "{SNAPSHOT_ID}": snapshot["snapshot_id"],
    })


def _artist_layer(rank: int, total: int) -> str:
    if total <= 10 or rank <= max(1, total // 10):
        return "core"
    if rank <= max(2, total // 3):
        return "active"
    return "longtail"


def _assignment_for_track(track: dict[str, Any], position: int, mood_axes: dict[str, Any],
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
        "style_axes": {axis: None for axis in mood_axes},
        "applied_scope": "taste_unknown",
        "rationale": "品味摘要未覆盖该艺人，保留未知，不做猜测。",
        "sources": [],
        "evidence_items": [],
        "field_provenance": {
            field: {"scope": "unknown", "confidence": "low", "sources": [], "origin": "taste_summary",
                    "verification_scope": "pending_independent_verification"}
            for field in ("style_mix", "style_axes")
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
        "style_axes": dict(mood_axes),
        "applied_scope": "taste_artist",
        "rationale": cluster["scene"],
        "sources": [cluster["reference_url"]] if cluster["reference_url"] else [],
    }


def map_taste_to_packet(snapshot: dict[str, Any], bundle: dict[str, Any], taxonomy: dict[str, Any],
                        *, taxonomy_path: Path, policy: dict[str, Any],
                        policy_path: Path | None = None) -> dict[str, Any]:
    """Compile a validated taste bundle into the standard analysis packet.

    The packet keeps ``packet_type: musician_analysis`` so the recommendation
    stage, offline validation and the web view model continue to work; the
    per-track style facts are derived from artist-level cluster assignments
    and are marked ``origin: taste_summary`` everywhere.
    """
    tracks = snapshot["tracks"]
    mode = bundle["analysis_mode"]
    stats = build_tracklist_stats(snapshot)
    mood_axes = bundle["taste_profile"]["mood_axes"]
    # 只有带可检索来源的场景归属才驱动风格分配；无来源的归属仅用于展示，
    # 对应艺人保持 unclassified——不冒充已分类画像。
    cluster_by_artist = {
        normalized_name(cluster["artist"]): cluster
        for cluster in bundle["artist_clusters"] if cluster["reference_url"]
    }
    display_clusters = {
        normalized_name(cluster["artist"]): cluster for cluster in bundle["artist_clusters"]
    }
    assignments = [
        _assignment_for_track(track, index, mood_axes, cluster_by_artist)
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
            "is_core_artist": primary_counter.get(name, 0) > 0,
            "classification_status": "classified" if classified else "unclassified",
            "confidence": display["confidence"] if display else "low",
            "primary_style_ref": refs[0] if refs else "",
            "style_mix": [{"style_ref": ref, "role": "primary" if index == 0 else "secondary",
                           "weight": 1.0 / len(refs)} for index, ref in enumerate(refs)],
            "style_weights": {ref: 1.0 / len(refs) for ref in refs},
            "style_axes": dict(mood_axes),
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
        "axis_definitions": taxonomy["axis_definitions"],
        "frequency_basis": "primary_artist_track_count_from_current_snapshot",
        "artist_profiles": profiles,
        "artist_profile_count": len(profiles),
        "core_artist_profile_count": sum(1 for item in profiles if item["is_core_artist"]),
        "classified_track_count": classified_tracks,
        "unclassified_track_count": len(tracks) - classified_tracks,
        "style_distribution": style_distribution,
        "dominant_style_mix": dominant_style_mix,
        "style_axes": dict(mood_axes),
    }

    identity_payload = {
        "mode": mode,
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_snapshot_input_sha256": snapshot.get("input_sha256", ""),
        "as_of_date": snapshot.get("captured_at", "")[:10],
        "taste_bundle_sha256": stable_hash(bundle),
        "primary_distribution": primary_distribution,
        "style_analysis_refs": style_analysis["active_style_refs"],
        "mood_axes": mood_axes,
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
    return validate_analysis_packet(packet)


def _request_id(snapshot: dict[str, Any], mode: str) -> str:
    return f"{mode}-{stable_hash({'snapshot_id': snapshot['snapshot_id'], 'input_sha256': snapshot.get('input_sha256', ''), 'mode': mode})[:16]}"


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
        raw = execute(command, prompt, timeout=timeout)
        bundle = validate_taste_summary_result(raw, snapshot=snapshot, taxonomy=taxonomy, mode=mode)
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
