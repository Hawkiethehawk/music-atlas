"""程序化的公开平台事实采集与候选召回。

本模块是 Step 2/3 的事实边界：身份字段只来自本次实际访问的平台公开
接口。它不会推断风格、关系或排序；资料不足时由调用方保留 unknown。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from contracts import STYLE_AXIS_IDS, normalized_name, normalized_text, track_key, utc_now
from metadata_verify import SOURCE_PRIORITY, SOURCES, cover_url_status, name_similarity, netease_song_cover, verify_many


def collect_track_facts(snapshot: dict[str, Any], *, concurrency: int = 8) -> dict[str, Any]:
    """核验快照中的每首曲目并返回可审计的事实包。

    歌单读取器本身已给出公开链接；再次按曲名和艺人检索用于确认展示的
    曲名、艺人、专辑与版本没有在后续处理里漂移。失败项明确记录，不补写。
    """
    tracks = list(snapshot.get("tracks") or [])
    requested = [(str(item.get("title") or ""), str(item.get("artist") or "")) for item in tracks]
    verified = verify_many(requested, concurrency=concurrency)
    records = []
    for position, (track, fact) in enumerate(zip(tracks, verified), 1):
        source_url = next(iter((track.get("links") or {}).values()), None)
        record: dict[str, Any] = {
            "position": position,
            "track_key": track_key(track.get("title"), track.get("artist")),
            "requested": {"title": track.get("title"), "artist": track.get("artist"), "album": track.get("album", "")},
            "retrieved_at": utc_now(),
            "status": "verified" if fact and fact.get("source") != "skipped" else "source_recorded" if source_url else "unverified",
            "platform_fact": fact,
            "source_url": (fact or {}).get("url") or source_url,
        }
        records.append(record)
    return {
        "schema_version": "2.0", "artifact_type": "track_fact_bundle",
        "source_snapshot_id": snapshot["snapshot_id"], "retrieved_at": utc_now(),
        "track_count": len(tracks), "verified_count": sum(item["status"] == "verified" for item in records),
        "source_recorded_count": sum(item["status"] == "source_recorded" for item in records),
        "unverified_count": sum(item["status"] == "unverified" for item in records), "records": records,
    }


def _search_artist(source: str, artist: str) -> list[dict[str, Any]]:
    # The existing source adapters search "artist + title". Empty title is a
    # public artist query and returns the platform's actual song records.
    return SOURCES[source]("", artist)


def _candidate_from_hit(hit: dict[str, Any], *, source: str, anchor: dict[str, Any], retrieved_at: str) -> dict[str, Any] | None:
    title, artist, project, platform_id, url = (normalized_text(hit.get(key)) for key in ("title", "artist", "album", "platform_id", "url"))
    if not all((title, artist, platform_id, url)):
        return None
    # A query result must retain the requested primary artist. This stops a
    # collaborator or a similarly named artist from becoming a continuation.
    if name_similarity(anchor["artist"], artist) < 0.85:
        return None
    entity_ref = anchor["entity_ref"]
    platform = source
    evidence = {
        "claim_type": "track_identity",
        "claim": f"平台公开歌曲记录：{title} - {artist}（{project or '未提供专辑'}）",
        "url": url,
        "retrieved_at": retrieved_at,
        "source_identifier": f"{platform}:{platform_id}",
        "verification_result": "verified",
    }
    return {
        "canonical_track_id": f"platform:{platform}:{platform_id}",
        "platform_track_id": platform_id,
        "title": title,
        "artist": artist,
        "project": project or "未知专辑",
        "candidate_type": "artist_continuation",
        "analysis_refs": [entity_ref],
        "style_status": "unclassified",
        "style_refs": [], "style_mix": [], "style_axes": dict.fromkeys(STYLE_AXIS_IDS),
        "style_confidence": "low",
        "relation_path": ["当前歌单", anchor["artist"], "平台公开歌曲记录"],
        "evidence_grade": "C", "evidence_items": [evidence],
        "discovery_source": url, "sources": [url], "platform_links": {platform: url},
        "metadata_verified": {"source": platform, "title": title, "artist": artist,
                              "album": project, "cover": hit.get("cover"), "platform_track_id": platform_id, "url": url,
                              "retrieved_at": retrieved_at},
    }


def discover_platform_candidates(packet: dict[str, Any], *, max_candidates: int = 80, concurrency: int = 4,
                                 progress: Callable[[int, int], None] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按当前 Step 2 艺人分布并发查询平台，构造唯一的真实候选池。"""
    anchors = [item for item in packet.get("primary_distribution", []) if item.get("artist") and item.get("entity_ref")]
    # 高频艺人优先，且只请求每个艺人优先级最高的公开来源，避免为同一事实做三倍请求。
    jobs = [(anchor, SOURCE_PRIORITY[0]) for anchor in anchors]
    retrieved_at = utc_now()
    candidates: list[dict[str, Any]] = []
    exclusion = packet.get("playlist_exclusion") if isinstance(packet.get("playlist_exclusion"), dict) else {}
    seen_keys = set(packet.get("favorite_track_keys") or []) | set(exclusion.get("track_keys") or [])
    excluded_platform_ids = {normalized_text(value).casefold() for value in exclusion.get("platform_track_ids") or []}
    seen_ids: set[str] = set()
    failures: list[dict[str, str]] = []
    workers = max(1, min(concurrency, len(jobs) or 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="music-atlas-platform") as pool:
        futures = {pool.submit(_search_artist, source, str(anchor["artist"])): (anchor, source) for anchor, source in jobs}
        completed = 0
        for future in as_completed(futures):
            anchor, source = futures[future]
            completed += 1
            try:
                hits = future.result()
            except Exception as exc:  # one artist/source failure must remain observable, not fabricated
                failures.append({"artist": str(anchor["artist"]), "source": source, "error": str(exc)[:300]})
                hits = []
            for hit in hits:
                candidate = _candidate_from_hit(hit, source=source, anchor=anchor, retrieved_at=retrieved_at)
                if candidate is None:
                    continue
                key = track_key(candidate["title"], candidate["artist"])
                canonical = candidate["canonical_track_id"].casefold()
                if key in seen_keys or canonical in seen_ids or normalized_text(candidate["platform_track_id"]).casefold() in excluded_platform_ids:
                    continue
                seen_keys.add(key); seen_ids.add(canonical); candidates.append(candidate)
                if len(candidates) >= max_candidates:
                    break
            if progress is not None:
                progress(completed, len(jobs))
            if len(candidates) >= max_candidates:
                break
    candidates.sort(key=lambda item: (normalized_name(item["artist"]), normalized_name(item["title"])))
    return candidates, {
        "schema_version": "2.0", "artifact_type": "platform_discovery_report", "analysis_id": packet["analysis_id"],
        "retrieved_at": retrieved_at, "query_count": len(jobs), "cache_hit_count": 0,
        "candidate_count": len(candidates), "failed_queries": failures,
    }


def hydrate_netease_covers(candidates: list[dict[str, Any]], *, concurrency: int = 4) -> None:
    """为最终入选的网易云候选按已绑定歌曲 ID 补取专辑图。"""

    netease_candidates = [item for item in candidates if item.get("metadata_verified", {}).get("source") == "netease"]
    with ThreadPoolExecutor(max_workers=min(concurrency, len(netease_candidates) or 1), thread_name_prefix="music-atlas-cover") as pool:
        futures = {pool.submit(netease_song_cover, str(item["platform_track_id"])): item for item in netease_candidates}
        for future in as_completed(futures):
            try:
                cover = future.result()
            except Exception:  # noqa: BLE001
                cover = None
            if cover and cover_url_status(cover) == "verified":
                metadata = futures[future]["metadata_verified"]
                # 不覆盖已经由跨来源回退选出的可达封面。
                if not metadata.get("cover") or cover_url_status(metadata.get("cover")) != "verified":
                    metadata["cover"] = cover
                    metadata["cover_source"] = "netease"
