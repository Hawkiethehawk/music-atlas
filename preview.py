"""Early, explicitly provisional recommendations for one isolated web job.

This preview only uses real public platform song records. It does not claim
style, relationship, cover or final quota validation. The normal workflow
continues to publish the definitive three groups after all checks pass.
"""

from collections import Counter
from typing import Any

from contracts import normalized_name, track_key
from platform_discovery import discover_platform_candidates


def preview_overall_description(snapshot: dict[str, Any], source_track_count: int) -> str:
    """A factual playlist-level description while the style audit is pending."""
    artists: Counter[str] = Counter()
    names: dict[str, str] = {}
    for track in snapshot.get("tracks") or []:
        name = str(track.get("artist") or "").strip()
        key = normalized_name(name)
        if key:
            artists[key] += 1
            names.setdefault(key, name)
    analyzed = len(snapshot.get("tracks") or [])
    scope = f"原歌单共 {source_track_count} 首，本次分析前 {analyzed} 首" if analyzed < source_track_count else f"歌单共 {analyzed} 首"
    if artists:
        leaders = "、".join(names[key] for key, _ in artists.most_common(3))
        scope += f"，涉及 {len(artists)} 位艺人；曲目中出现较多的是 {leaders}。"
    else:
        scope += "。"
    return scope + "这是基于歌单曲目清单的初步描述，风格与推荐理由将在正式核验后补齐。"


def preview_recommendations(snapshot: dict[str, Any], full_tracks: list[dict[str, Any]],
                            history: dict[str, Any], *, count: int = 10) -> list[dict[str, str]]:
    frequency: Counter[str] = Counter()
    display: dict[str, str] = {}
    for track in snapshot.get("tracks") or []:
        name = str(track.get("artist") or "").strip()
        key = normalized_name(name)
        if key:
            frequency[key] += 1
            display.setdefault(key, name)
    anchors = [{"artist": display[key], "entity_ref": f"preview-artist-{index}"}
               for index, (key, _total) in enumerate(frequency.most_common(6))]
    if not anchors:
        return []
    excluded_keys = {track_key(track.get("title"), track.get("artist")) for track in full_tracks
                     if isinstance(track, dict) and track.get("title") and track.get("artist")}
    excluded_ids = {str(track.get("platform_track_id") or "").strip() for track in full_tracks
                    if isinstance(track, dict) and str(track.get("platform_track_id") or "").strip()}
    packet = {
        "analysis_id": f"preview:{snapshot['snapshot_id']}",
        "primary_distribution": anchors,
        "favorite_track_keys": sorted(excluded_keys),
        "playlist_exclusion": {"track_keys": sorted(excluded_keys), "platform_track_ids": sorted(excluded_ids)},
    }
    candidates, _report = discover_platform_candidates(
        packet, max_candidates=48, concurrency=6, tags_client=None)
    recent_keys = set(history.get("track_keys") or [])
    recent_ids = set(history.get("canonical_track_ids") or [])
    lanes: dict[str, list[dict[str, Any]]] = {anchor["artist"]: [] for anchor in anchors}
    for item in candidates:
        key = track_key(item.get("title"), item.get("artist"))
        if key in excluded_keys or key in recent_keys or item.get("canonical_track_id") in recent_ids:
            continue
        if str(item.get("platform_track_id") or "") in excluded_ids:
            continue
        if not (item.get("metadata_verified") or {}).get("url"):
            continue
        lane = next((name for name in lanes if normalized_name(name) == normalized_name(item.get("artist"))), None)
        if lane:
            lanes[lane].append(item)
    selected: list[dict[str, str]] = []
    used: set[str] = set()
    # Round-robin avoids an early preview dominated by one favorite artist.
    while len(selected) < count and any(lanes.values()):
        for lane in lanes.values():
            if not lane:
                continue
            item = lane.pop(0)
            key = track_key(item["title"], item["artist"])
            if key in used:
                continue
            used.add(key)
            selected.append({
                "title": item["title"], "artist": item["artist"],
                "url": item["metadata_verified"]["url"],
                "platform": item["metadata_verified"]["source"],
            })
            if len(selected) >= count:
                break
    return selected
