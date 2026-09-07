"""Resolve discovery routes from the current packet, not Agent reference claims."""

from __future__ import annotations

from typing import Any

from contracts import normalized_name
from preference_model import interest_profiles


def resolve_candidate_route(candidate: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    artist = normalized_name(candidate.get("artist"))
    counts = {item["entity_ref"]: int(item["count"]) for item in packet.get("primary_distribution", [])}
    preferred = {item["entity_ref"] for item in packet.get("preferred_artists", [])}
    peak = max(counts.values(), default=1) or 1
    matches = []
    for entity in packet.get("entities", []):
        ref = entity["entity_ref"]
        affinity = max(counts.get(ref, 0) / peak, 1.0 if ref in preferred else 0.0)
        if affinity <= 0:
            continue
        names = [entity["name"], *entity.get("aliases", [])]
        if artist in {normalized_name(name) for name in names}:
            matches.append({"candidate_type": "artist_continuation", "anchor_ref": ref,
                            "anchor_artist": entity["name"], "path": [entity["name"], candidate["artist"]],
                            "strength": 1.0, "frequency_strength": affinity, "sources": [],
                            "verification_scope": "current_packet_artist_identity"})
        for relation in entity.get("related_projects", []):
            sources = relation.get("sources", [])
            if artist != normalized_name(relation.get("name")) or not sources or relation.get("confidence") not in {"high", "medium"}:
                continue
            strength = 0.9 if relation["confidence"] == "high" else 0.7
            matches.append({"candidate_type": "musician_relation", "anchor_ref": ref,
                            "anchor_artist": entity["name"],
                            "path": [entity["name"], relation.get("person") or relation.get("relation") or "related_project", relation["name"]],
                            "strength": strength, "frequency_strength": affinity * strength,
                            "sources": list(sources),
                            "verification_scope": "current_packet_agent_relation" if entity.get("research_origin") == "agent" else "current_packet_catalog_relation"})
    if matches:
        return max(matches, key=lambda item: (item["strength"], item["frequency_strength"], item["anchor_ref"]))
    active = set(packet.get("style_analysis", {}).get("active_style_refs", []))
    profiles = interest_profiles(packet) or [packet.get("style_analysis", {})]
    distances = []
    for profile in profiles:
        axes = profile.get("style_axes", {})
        common = [axis for axis in axes if axes[axis] is not None and candidate.get("style_axes", {}).get(axis) is not None]
        if common:
            distances.append(sum(abs(axes[axis] - candidate["style_axes"][axis]) for axis in common) / len(common))
    distance = min(distances, default=0)
    novel_style = any(ref not in active for ref in candidate.get("style_refs", []))
    return {"candidate_type": "exploration" if novel_style or distance >= 30 else "style_neighbor",
            "anchor_ref": None, "anchor_artist": None, "path": [], "strength": 0.0,
            "frequency_strength": 0.0, "sources": [], "verification_scope": "descriptive_style_distance"}
