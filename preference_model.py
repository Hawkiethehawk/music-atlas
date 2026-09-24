"""Bounded deterministic interest grouping over reviewed descriptive profiles."""

from __future__ import annotations

import math
import threading
from collections import Counter, OrderedDict
from typing import Any

from contracts import normalized_name, stable_hash


CONFIDENCE = {"high": 1.0, "medium": 0.78, "low": 0.55}
MODEL_CONFIG = {"algorithm": "farthest_first_public_tags_v2", "max_interests": 3, "split_distance": 0.28}

# Only fallback packets need this cache: normal analysis packets already carry
# validated interest_profiles. Retain the packet itself while cached so id()
# cannot be reused for a different user/job. The bound limits retained packets.
_INTEREST_CACHE_LIMIT = 8
_interest_cache: OrderedDict[int, tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = OrderedDict()
_interest_cache_lock = threading.Lock()


def style_vector(item: dict[str, Any]) -> dict[str, float]:
    mix = item.get("style_mix") or []
    if mix:
        return {entry["style_ref"]: float(entry["weight"]) for entry in mix
                if isinstance(entry, dict) and entry.get("style_ref")}
    refs = [ref for ref in item.get("style_refs", []) if isinstance(ref, str) and ref]
    if refs:
        return {ref: 1.0 / len(refs) for ref in refs}
    evidence = item.get("style_evidence") or {}
    if (evidence.get("status") == "supported" and evidence.get("scope") in {"track", "album", "artist"}
            and evidence.get("url") in (item.get("sources") or []) and evidence.get("retrieved_at")):
        refs = [tag.get("style_ref") for tag in evidence.get("tags", [])
                if isinstance(tag, dict) and isinstance(tag.get("style_ref"), str)]
        counts = Counter(refs)
        return {ref: value / len(refs) for ref, value in counts.items()} if refs else {}
    return {}


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    divisor = math.sqrt(sum(x * x for x in left.values()) * sum(x * x for x in right.values()))
    return sum(value * right.get(key, 0) for key, value in left.items()) / divisor if divisor else 0.0


def profile_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    # Public tags express a documented relation, not an inferred listening axis.
    return 1.0 - cosine(style_vector(left), style_vector(right))


def build_interest_profiles(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tracks = sorted((item for item in assignments if item.get("classification_status") == "classified" and style_vector(item)),
                    key=lambda item: (item["track_key"], item.get("position") or 0))
    if not tracks:
        return []
    # At most three passes for seed selection; no all-pairs matrix or training.
    seeds = [tracks[0]]
    while len(seeds) < MODEL_CONFIG["max_interests"]:
        farthest = max(tracks, key=lambda item: min(profile_distance(item, seed) for seed in seeds))
        if min(profile_distance(farthest, seed) for seed in seeds) <= MODEL_CONFIG["split_distance"]:
            break
        seeds.append(farthest)
    groups: list[list[dict[str, Any]]] = [[] for _ in seeds]
    for track in tracks:
        best = min(range(len(seeds)), key=lambda index: profile_distance(track, seeds[index]))
        groups[best].append(track)
    artist_counts = Counter(normalized_name(item["artist"]) for item in tracks)
    profiles = []
    for group in groups:
        weights = [CONFIDENCE.get(item.get("confidence"), 0.55) / math.sqrt(artist_counts[normalized_name(item["artist"])]) for item in group]
        mass = sum(weights)
        mix: Counter[str] = Counter()
        for item, weight in zip(group, weights):
            for ref, value in style_vector(item).items():
                mix[ref] += weight * value
        profile = {
            "interest_id": "interest-" + stable_hash(sorted(item["track_key"] for item in group))[:12],
            "track_count": len(group), "effective_weight": round(mass, 6),
            "member_track_keys": [item["track_key"] for item in group],
            "style_mix": [{"style_ref": ref, "weight": round(value / sum(mix.values()), 6)} for ref, value in sorted(mix.items())],
        }
        representatives = sorted(group, key=lambda item: (profile_distance(item, profile), item["track_key"]))[:3]
        profile["representative_tracks"] = [{key: item.get(key, "") for key in ("track_key", "title", "artist", "album")} for item in representatives]
        profiles.append(profile)
    total_mass = sum(item["effective_weight"] for item in profiles)
    for profile in profiles:
        profile["share"] = round(profile["effective_weight"] / total_mass, 6)
    return sorted(profiles, key=lambda item: (-item["share"], item["interest_id"]))


def interest_profiles(packet: dict[str, Any]) -> list[dict[str, Any]]:
    saved = packet.get("style_analysis", {}).get("interest_profiles")
    if saved is not None:
        return saved
    assignments = packet.get("track_style_assignments", [])
    key = id(packet)
    with _interest_cache_lock:
        cached = _interest_cache.get(key)
        if cached is not None and cached[0] is packet and cached[1] is assignments:
            _interest_cache.move_to_end(key)
            return cached[2]
        profiles = build_interest_profiles(assignments)
        _interest_cache[key] = (packet, assignments, profiles)
        _interest_cache.move_to_end(key)
        if len(_interest_cache) > _INTEREST_CACHE_LIMIT:
            _interest_cache.popitem(last=False)
        return profiles


def match_interest(candidate: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any] | None:
    profiles = interest_profiles(packet)
    return min(profiles, key=lambda item: (profile_distance(candidate, item), -item["share"], item["interest_id"])) if profiles else None
