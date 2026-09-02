#!/usr/bin/env python3
"""Shared contracts and validation helpers for the weekly music workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


SCHEMA_VERSION = "1.0"


class ContractError(ValueError):
    """Raised when a pipeline artifact does not satisfy its contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"找不到 JSON 文件：{path}") from exc
    except OSError as exc:
        raise ContractError(f"无法读取 JSON 文件：{path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"JSON 格式错误：{path}：第 {exc.lineno} 行") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ContractError(f"无法计算文件摘要：{path}：{exc}") from exc
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalized_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def normalized_name(value: Any) -> str:
    text = normalized_text(value).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text, flags=re.UNICODE)


def artist_key(value: Any) -> str:
    compact = normalized_name(value)
    return compact or "unknown-artist"


def track_key(title: Any, artist: Any) -> str:
    return f"{normalized_text(title).casefold()}\u001f{normalized_text(artist).casefold()}"


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} 必须是 JSON 对象")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} 不能为空")
    return value.strip()


def _require_int(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{label} 必须是大于等于 {minimum} 的整数")
    return value


def _validate_http_url(value: Any, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        if allow_empty:
            return ""
        raise ContractError(f"{label} 必须是 HTTP(S) URL")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ContractError(f"{label} 必须是 HTTP(S) URL")
    return value.strip()


def _track_from_snapshot(item: Any, index: int) -> dict[str, Any]:
    track = _require_dict(item, f"tracks[{index}]")
    title = _require_text(track.get("title"), f"tracks[{index}].title")
    artist = _require_text(track.get("artist"), f"tracks[{index}].artist")
    artists = track.get("artists", [artist])
    if not isinstance(artists, list) or not artists:
        raise ContractError(f"tracks[{index}].artists 必须是非空数组")
    for artist_index, credited in enumerate(artists):
        _require_text(credited, f"tracks[{index}].artists[{artist_index}]")
    links = track.get("links", {})
    if links is not None:
        if not isinstance(links, dict):
            raise ContractError(f"tracks[{index}].links 必须是对象")
        for platform, link in links.items():
            _validate_http_url(link, f"tracks[{index}].links[{platform}]")
    return track


def validate_playlist_snapshot(value: Any, require_complete: bool = True) -> dict[str, Any]:
    snapshot = _require_dict(value, "PlaylistSnapshot")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"PlaylistSnapshot.schema_version 必须是 {SCHEMA_VERSION}")
    _require_text(snapshot.get("snapshot_id"), "snapshot_id")
    _require_text(snapshot.get("platform"), "platform")
    _require_text(snapshot.get("captured_at"), "captured_at")
    status = _require_text(snapshot.get("reader_status"), "reader_status")
    if require_complete and status != "complete":
        raise ContractError(f"Step 1 快照不是 complete：reader_status={status}")
    declared = _require_int(snapshot.get("declared_track_count"), "declared_track_count")
    actual = _require_int(snapshot.get("track_count"), "track_count")
    tracks = snapshot.get("tracks")
    if not isinstance(tracks, list):
        raise ContractError("tracks 必须是数组")
    if declared != actual:
        raise ContractError(
            "Step 1 数量不一致："
            f"declared_track_count={declared}, track_count={actual}"
        )
    if len(tracks) != actual:
        raise ContractError(
            "Step 1 歌曲数组数量不一致："
            f"len(tracks)={len(tracks)}, track_count={actual}"
        )
    for index, item in enumerate(tracks):
        _track_from_snapshot(item, index)
    input_hash = snapshot.get("input_sha256")
    if input_hash is not None:
        _require_text(input_hash, "input_sha256")
    return snapshot


def validate_analysis_packet(value: Any) -> dict[str, Any]:
    packet = _require_dict(value, "MusicianAnalysisPacket")
    if packet.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"MusicianAnalysisPacket.schema_version 必须是 {SCHEMA_VERSION}")
    if packet.get("packet_type") != "musician_analysis":
        raise ContractError("packet_type 必须是 musician_analysis")
    _require_text(packet.get("analysis_id"), "analysis_id")
    _require_text(packet.get("source_snapshot_id"), "source_snapshot_id")
    source_count = _require_int(packet.get("source_track_count"), "source_track_count")
    favorite_tracks = packet.get("favorite_tracks")
    if not isinstance(favorite_tracks, list):
        raise ContractError("favorite_tracks 必须是数组")
    if len(favorite_tracks) != source_count:
        raise ContractError(
            "分析包与 Step 1 数量不一致："
            f"len(favorite_tracks)={len(favorite_tracks)}, source_track_count={source_count}"
        )
    favorite_keys = packet.get("favorite_track_keys")
    if not isinstance(favorite_keys, list) or len(favorite_keys) != source_count:
        raise ContractError("favorite_track_keys 必须与 source_track_count 一致")
    for index, item in enumerate(favorite_tracks):
        track = _require_dict(item, f"favorite_tracks[{index}]")
        title = _require_text(track.get("title"), f"favorite_tracks[{index}].title")
        artist = _require_text(track.get("artist"), f"favorite_tracks[{index}].artist")
        if track.get("track_key") != track_key(title, artist):
            raise ContractError(f"favorite_tracks[{index}].track_key 与标题/艺人不一致")
    for field in ("primary_distribution", "credited_distribution", "entities", "preferred_artists"):
        if not isinstance(packet.get(field), list):
            raise ContractError(f"{field} 必须是数组")
    for field in ("primary_distribution", "credited_distribution"):
        total = 0
        for index, item in enumerate(packet[field]):
            distribution = _require_dict(item, f"{field}[{index}]")
            _require_text(distribution.get("artist"), f"{field}[{index}].artist")
            _require_text(distribution.get("entity_ref"), f"{field}[{index}].entity_ref")
            total += _require_int(distribution.get("count"), f"{field}[{index}].count")
        if field == "primary_distribution" and total != source_count:
            raise ContractError(
                "primary_distribution 总数与 source_track_count 不一致："
                f"{total} != {source_count}"
            )
    policy = _require_dict(packet.get("recommendation_policy"), "recommendation_policy")
    minimum = _require_int(policy.get("min_recommendations"), "min_recommendations", 1)
    maximum = _require_int(policy.get("max_recommendations"), "max_recommendations", minimum)
    if maximum < minimum:
        raise ContractError("max_recommendations 不能小于 min_recommendations")
    _require_int(policy.get("max_per_artist"), "max_per_artist", 1)
    _require_int(policy.get("max_per_project"), "max_per_project", 1)
    _require_int(policy.get("min_projects"), "min_projects", 1)
    ref_ids = packet.get("analysis_ref_ids")
    if not isinstance(ref_ids, list) or any(not isinstance(ref, str) or not ref for ref in ref_ids):
        raise ContractError("analysis_ref_ids 必须是非空字符串数组")
    return packet


def _source_is_apple_personalization(value: str) -> bool:
    lowered = value.casefold()
    return "music.apple.com" in lowered or "apple music" in lowered and "personal" in lowered


def validate_recommendation_bundle(value: Any, packet: dict[str, Any]) -> dict[str, Any]:
    analysis = validate_analysis_packet(packet)
    bundle = _require_dict(value, "RecommendationBundle")
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"RecommendationBundle.schema_version 必须是 {SCHEMA_VERSION}")
    if bundle.get("bundle_type") != "recommendation_bundle":
        raise ContractError("bundle_type 必须是 recommendation_bundle")
    if bundle.get("analysis_id") != analysis.get("analysis_id"):
        raise ContractError("RecommendationBundle 必须引用本次 Step 2 的 analysis_id")
    _require_text(bundle.get("generated_at"), "generated_at")
    status = _require_text(bundle.get("status"), "status")
    if status not in {"ready", "insufficient_evidence"}:
        raise ContractError("status 必须是 ready 或 insufficient_evidence")
    recommendations = bundle.get("recommendations")
    if not isinstance(recommendations, list):
        raise ContractError("recommendations 必须是数组")
    policy = analysis["recommendation_policy"]
    minimum = int(policy["min_recommendations"])
    maximum = int(policy["max_recommendations"])
    if status == "ready" and not minimum <= len(recommendations) <= maximum:
        raise ContractError(
            f"ready 状态的推荐数量必须在 {minimum} 到 {maximum} 之间，实际为 {len(recommendations)}"
        )
    if status == "insufficient_evidence" and recommendations:
        raise ContractError("insufficient_evidence 状态不能携带推荐歌曲")
    if status == "insufficient_evidence":
        _require_text(bundle.get("message"), "message")

    known_refs = set(analysis["analysis_ref_ids"])
    favorite_keys = set(str(key) for key in analysis["favorite_track_keys"])
    seen_keys: set[str] = set()
    artist_counts: Counter[str] = Counter()
    project_counts: Counter[str] = Counter()
    for index, item in enumerate(recommendations):
        recommendation = _require_dict(item, f"recommendations[{index}]")
        title = _require_text(recommendation.get("title"), f"recommendations[{index}].title")
        artist = _require_text(recommendation.get("artist"), f"recommendations[{index}].artist")
        project = _require_text(recommendation.get("project"), f"recommendations[{index}].project")
        artist_counts[artist.casefold()] += 1
        project_counts[project.casefold()] += 1
        if artist_counts[artist.casefold()] > int(policy["max_per_artist"]):
            raise ContractError(
                f"recommendations[{index}] 超过单艺人上限：{artist}"
            )
        if project_counts[project.casefold()] > int(policy["max_per_project"]):
            raise ContractError(
                f"recommendations[{index}] 超过单项目上限：{project}"
            )
        candidate_key = track_key(title, artist)
        if candidate_key in favorite_keys:
            raise ContractError(f"recommendations[{index}] 命中当前喜爱歌曲：{title} - {artist}")
        if candidate_key in seen_keys:
            raise ContractError(f"recommendations[{index}] 与前面推荐重复：{title} - {artist}")
        seen_keys.add(candidate_key)

        refs = recommendation.get("analysis_refs")
        if not isinstance(refs, list) or not refs:
            raise ContractError(f"recommendations[{index}].analysis_refs 不能为空")
        unknown_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in known_refs]
        if unknown_refs:
            raise ContractError(f"recommendations[{index}] 包含未知 analysis_refs：{unknown_refs}")
        relation_path = recommendation.get("relation_path")
        if not isinstance(relation_path, list) or len(relation_path) < 2:
            raise ContractError(f"recommendations[{index}].relation_path 至少需要两段")

        explanation = _require_dict(recommendation.get("explanation"), f"recommendations[{index}].explanation")
        for explanation_field in ("preference_basis", "artist_relation", "music_fit", "novelty", "text"):
            text = _require_text(explanation.get(explanation_field), f"recommendations[{index}].explanation.{explanation_field}")
            if explanation_field == "text" and len(text) < 24:
                raise ContractError(f"recommendations[{index}].explanation.text 太短，无法构成逐首说明")

        sources = recommendation.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ContractError(f"recommendations[{index}].sources 不能为空")
        for source_index, source in enumerate(sources):
            source_url = _validate_http_url(source, f"recommendations[{index}].sources[{source_index}]")
            if _source_is_apple_personalization(source_url):
                raise ContractError("Apple Music 不能作为候选发现、排序或推荐说明的来源")
        discovery_source = recommendation.get("discovery_source", "")
        if not isinstance(discovery_source, str) or not discovery_source.strip():
            raise ContractError(f"recommendations[{index}].discovery_source 不能为空")
        if _source_is_apple_personalization(discovery_source):
            raise ContractError("Apple Music 个性化推荐不能作为候选来源")

        links = recommendation.get("platform_links", {})
        if not isinstance(links, dict):
            raise ContractError(f"recommendations[{index}].platform_links 必须是对象")
        for platform, link in links.items():
            _validate_http_url(link, f"recommendations[{index}].platform_links[{platform}]")
    if status == "ready" and len(project_counts) < int(policy["min_projects"]):
        raise ContractError(
            "ready 状态的推荐项目覆盖数不足："
            f"需要至少 {policy['min_projects']} 个，实际为 {len(project_counts)} 个"
        )
    return bundle


def validate_recommendation_file(bundle_path: Path, analysis_path: Path) -> dict[str, Any]:
    packet = validate_analysis_packet(read_json(analysis_path))
    return validate_recommendation_bundle(read_json(bundle_path), packet)


def count_distribution(items: Iterable[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        value = normalized_text(item.get(field))
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    ordered = sorted(counts.items(), key=lambda entry: (-entry[1], entry[0].casefold()))
    return [
        {
            "rank": rank,
            "artist": name,
            "entity_ref": f"artist:{artist_key(name)}",
            "count": count,
        }
        for rank, (name, count) in enumerate(ordered, 1)
    ]
