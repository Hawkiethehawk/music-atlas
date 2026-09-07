"""Bounded deterministic interest grouping over reviewed descriptive profiles."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from contracts import STYLE_AXIS_IDS, normalized_name, stable_hash


CONFIDENCE = {"high": 1.0, "medium": 0.78, "low": 0.55}
MODEL_CONFIG = {"algorithm": "farthest_first_descriptive_v1", "max_interests": 3, "split_distance": 0.28}


def style_vector(item: dict[str, Any]) -> dict[str, float]:
    return {entry["style_ref"]: float(entry["weight"]) for entry in item.get("style_mix", [])}


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    divisor = math.sqrt(sum(x * x for x in left.values()) * sum(x * x for x in right.values()))
    return sum(value * right.get(key, 0) for key, value in left.items()) / divisor if divisor else 0.0


def profile_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    axes = [axis for axis in STYLE_AXIS_IDS if left.get("style_axes", {}).get(axis) is not None and right.get("style_axes", {}).get(axis) is not None]
    axis_distance = sum(abs(left["style_axes"][axis] - right["style_axes"][axis]) for axis in axes) / (100 * len(axes)) if axes else 1.0
    return 0.6 * axis_distance + 0.4 * (1 - cosine(style_vector(left), style_vector(right)))


def build_interest_profiles(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tracks = sorted((item for item in assignments if item.get("classification_status") == "classified"),
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
            "style_mix": [{"style_ref": ref, "weight": round(value / mass, 6)} for ref, value in sorted(mix.items())],
            "style_axes": {axis: round(sum(item["style_axes"][axis] * weight for item, weight in zip(group, weights)) / mass, 4) for axis in STYLE_AXIS_IDS},
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
    return saved if saved is not None else build_interest_profiles(packet.get("track_style_assignments", []))


def match_interest(candidate: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any] | None:
    profiles = interest_profiles(packet)
    return min(profiles, key=lambda item: (profile_distance(candidate, item), -item["share"], item["interest_id"])) if profiles else None
