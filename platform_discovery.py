"""程序化的公开平台事实采集与候选召回。

本模块是 Step 2/3 的事实边界：身份字段只来自本次实际访问的平台公开
接口。它不会推断风格、关系或排序；资料不足时由调用方保留 unknown。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from contracts import normalized_name, normalized_text, track_key, utc_now
from metadata_verify import SOURCE_PRIORITY, SOURCES, cover_url_status, name_similarity, netease_song_cover, verify_many


def _snapshot_song_source(track: dict[str, Any], platform: str) -> str | None:
    """只接受与快照歌曲 ID 对应的平台歌曲链接，不把歌单/专辑链接当歌曲来源。"""
    links = track.get("links")
    if not isinstance(links, dict):
        return None
    url = links.get(platform)
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    song_id = str(track.get("platform_track_id") or "").strip()
    if not song_id:
        return None
    query = parse_qs(parsed.query)
    path = parsed.path.rstrip("/")
    if platform == "apple_music" and host == "music.apple.com":
        linked_id = query.get("i", [""])[0] if "/album/" in path else path.rsplit("/", 1)[-1] if "/song/" in path else ""
    elif platform == "netease" and host == "music.163.com" and path == "/song":
        linked_id = query.get("id", [""])[0]
    elif platform == "qq" and host in {"y.qq.com", "i.y.qq.com"}:
        linked_id = (path.rsplit("/", 1)[-1] if "/songDetail/" in path
                     else query.get("songmid", [""])[0] if path.endswith("/playsong.html") else "")
    else:
        return None
    return url if linked_id == song_id else None


def collect_track_facts(snapshot: dict[str, Any], *, concurrency: int = 8) -> dict[str, Any]:
    """记录 Step 1 已取得的歌曲来源，仅对缺来源项独立检索。

    快照歌曲链接是来源记录，绝不冒充再次查询成功的 verified；失败项
    保留 unverified，不能从曲名、艺人推断公开来源。
    """
    tracks = list(snapshot.get("tracks") or [])
    platform = str(snapshot.get("platform") or "")
    source_urls = [_snapshot_song_source(track, platform) for track in tracks]
    # Apple Music playlist links are only source references. Do not resolve a
    # missing Apple link through another platform and claim it as Apple evidence.
    missing = ([index for index, url in enumerate(source_urls) if url is None]
               if platform != 'apple_music' else [])
    requested = [(str(tracks[index].get("title") or ""), str(tracks[index].get("artist") or ""))
                 for index in missing]
    searched = verify_many(requested, concurrency=concurrency) if requested else []
    verified = dict(zip(missing, searched))
    records = []
    for position, (track, source_url) in enumerate(zip(tracks, source_urls), 1):
        fact = verified.get(position - 1)
        independent_match = bool(
            fact and fact.get("source") != "skipped" and fact.get("url")
            and fact.get("title") and fact.get("artist") and fact.get("platform_track_id")
            and normalized_name(fact["title"]) == normalized_name(track.get("title"))
            and normalized_name(fact["artist"]) == normalized_name(track.get("artist"))
            and (fact.get("source") != platform or
                 str(fact["platform_track_id"]) == str(track.get("platform_track_id"))))
        record: dict[str, Any] = {
            "position": position,
            "track_key": track_key(track.get("title"), track.get("artist")),
            "requested": {"title": track.get("title"), "artist": track.get("artist"), "album": track.get("album", "")},
            "retrieved_at": utc_now(),
            "status": "verified" if independent_match else "source_recorded" if source_url else "unverified",
            "platform_fact": fact if independent_match else None,
            "source_url": fact["url"] if independent_match else source_url,
            "source_origin": "independent_search" if independent_match else "snapshot_song_url" if source_url else None,
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
        "style_refs": [], "style_mix": [],
        "style_confidence": "low",
        "relation_path": ["当前歌单", anchor["artist"], "平台公开歌曲记录"],
        "evidence_grade": "C", "evidence_items": [evidence],
        "discovery_source": url, "sources": [url], "platform_links": {platform: url},
        "metadata_verified": {"source": platform, "title": title, "artist": artist,
                              "album": project, "cover": hit.get("cover"), "platform_track_id": platform_id, "url": url,
                              "retrieved_at": retrieved_at},
    }


def discover_platform_candidates(packet: dict[str, Any], *, max_candidates: int = 80, concurrency: int = 4,
                                 progress: Callable[[int, int], None] | None = None, tags_client: Any | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按当前 Step 2 艺人分布并发查询平台，构造唯一的真实候选池。"""
    # 大歌单只对出现频率最高的一批艺人做平台搜索：控制召回耗时与后续编排体量。
    anchors = [item for item in packet.get("primary_distribution", [])
               if item.get("artist") and item.get("entity_ref")][:12]
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
    if tags_client is not None and candidates:
        # 平台记录只有歌曲身份，没有风格资料；沿用与相似艺人候选相同的 Last.fm
        # 标签机制补全，否则候选会因“缺少风格证据”被文案 Agent 全部排除。
        from lastfm_pipeline import attach_style_evidence, collect_tags
        records = collect_tags(
            {**packet, "favorite_tracks": [
                {"title": item.get("title"), "artist": item.get("artist"),
                 "album": (item.get("metadata_verified") or {}).get("album")}
                for item in candidates]},
            tags_client, concurrency=concurrency)["records"]
        for candidate, record in zip(candidates, records):
            attach_style_evidence(candidate, record)
    candidates.sort(key=lambda item: (normalized_name(item["artist"]), normalized_name(item["title"])))
    return candidates, {
        "schema_version": "2.0", "artifact_type": "platform_discovery_report", "analysis_id": packet["analysis_id"],
        "retrieved_at": retrieved_at, "query_count": len(jobs), "cache_hit_count": 0,
        "candidate_count": len(candidates), "failed_queries": failures,
        "style_tagged_count": sum(1 for item in candidates if (item.get("style_evidence") or {}).get("tags")),
    }


def hydrate_netease_covers(candidates: list[dict[str, Any]], *, concurrency: int = 8) -> None:
    """为最终入选的网易云候选补取专辑图。

    改用批量接口（一次最多 50 首）+ 并发校验封面可达性：
    逐首请求时每个候选要两次网络往返，实测 30 首需要近 50 秒。
    """

    netease_candidates = [item for item in candidates
                          if (item.get("metadata_verified") or {}).get("source") == "netease"]
    if not netease_candidates:
        return
    from metadata_verify import cover_url_status, netease_song_covers
    covers = netease_song_covers([str(item.get("platform_track_id") or "") for item in netease_candidates])

    def verify(item: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        cover = covers.get(str(item.get("platform_track_id") or ""))
        if not cover:
            return item, None
        try:
            reachable = cover_url_status(cover) == "verified"
        except Exception:  # noqa: BLE001
            reachable = False
        return item, cover if reachable else None

    with ThreadPoolExecutor(max_workers=min(max(1, concurrency), len(netease_candidates)),
                            thread_name_prefix="music-atlas-cover") as pool:
        for item, cover in pool.map(verify, netease_candidates):
            metadata = item.get("metadata_verified")
            if not isinstance(metadata, dict) or not cover:
                continue
            # 不覆盖已经由跨来源回退选出的可达封面。
            if not metadata.get("cover") or cover_url_status(metadata.get("cover")) != "verified":
                metadata["cover"] = cover
                metadata["cover_source"] = "netease"
