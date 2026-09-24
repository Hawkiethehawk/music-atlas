"""Resolve discovery routes from the current packet, not Agent reference claims."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

from contracts import normalized_name


# A packet is immutable during candidate selection. Keep the packet itself in
# this bounded cache: an id cannot be recycled while its context is retained,
# and no calculated indexes are written into the published JSON packet.
_ROUTE_CACHE_LIMIT = 8
_route_cache: OrderedDict[int, tuple[dict[str, Any], tuple[int, ...], dict[str, Any]]] = OrderedDict()
_route_cache_lock = threading.Lock()


def _packet_route_context(packet: dict[str, Any]) -> dict[str, Any]:
    fields = ("primary_distribution", "credited_distribution", "preferred_artists",
              "entities", "style_analysis", "source_tags")
    signature = tuple(id(packet.get(field)) for field in fields)
    key = id(packet)
    with _route_cache_lock:
        cached = _route_cache.get(key)
        if cached is not None and cached[0] is packet and cached[1] == signature:
            _route_cache.move_to_end(key)
            return cached[2]

        counts = {item["entity_ref"]: int(item["count"]) for item in packet.get("primary_distribution", [])}
        for item in packet.get("credited_distribution", []):
            ref = item.get("entity_ref")
            if ref:
                counts[ref] = max(counts.get(ref, 0), int(item.get("count") or 0))
        preferred = {item["entity_ref"] for item in packet.get("preferred_artists", [])}
        peak = max(counts.values(), default=1) or 1
        by_artist: dict[str, dict[str, Any]] = {}

        def add(marker: str, route: dict[str, Any]) -> None:
            old = by_artist.get(marker)
            # max() previously retained the first route when its key tied.
            rank = (route["strength"], route["frequency_strength"], route["anchor_ref"])
            if old is None or rank > (old["strength"], old["frequency_strength"], old["anchor_ref"]):
                by_artist[marker] = route

        for entity in packet.get("entities", []):
            ref = entity["entity_ref"]
            affinity = max(counts.get(ref, 0) / peak, 1.0 if ref in preferred else 0.0)
            if affinity <= 0:
                continue
            direct = {"candidate_type": "artist_continuation", "anchor_ref": ref,
                      "anchor_artist": entity["name"], "path": None,
                      "strength": 1.0, "frequency_strength": affinity, "sources": [],
                      "verification_scope": "current_packet_artist_identity"}
            for marker in {normalized_name(name) for name in [entity["name"], *entity.get("aliases", [])]}:
                add(marker, direct)
            for relation in entity.get("related_projects", []):
                sources = relation.get("sources", [])
                if not sources or relation.get("confidence") not in {"high", "medium"}:
                    continue
                strength = 0.9 if relation["confidence"] == "high" else 0.7
                related = {"candidate_type": "musician_relation", "anchor_ref": ref,
                           "anchor_artist": entity["name"],
                           "path": [entity["name"], relation.get("person") or relation.get("relation") or "related_project", relation["name"]],
                           "strength": strength, "frequency_strength": affinity * strength,
                           "sources": list(sources),
                           "verification_scope": "current_packet_agent_relation" if entity.get("research_origin") == "agent" else "current_packet_catalog_relation"}
                add(normalized_name(relation.get("name")), related)

        active = set(packet.get("style_analysis", {}).get("active_style_refs", []))
        if not active:
            active = {
                tag.get("style_ref")
                for record in (packet.get("source_tags") or {}).get("records", [])
                if isinstance(record, dict) and record.get("status") == "supported"
                for tag in record.get("tags", [])
                if isinstance(tag, dict) and tag.get("style_ref")
            }
        source_tag_names = {
            normalized_name(tag.get("tag"))
            for record in (packet.get("source_tags") or {}).get("records", [])
            if isinstance(record, dict) and record.get("status") == "supported"
            for tag in record.get("tags", [])
            if isinstance(tag, dict) and tag.get("tag")
        }
        context = {"by_artist": by_artist, "active": active, "source_tag_names": source_tag_names}
        _route_cache[key] = (packet, signature, context)
        _route_cache.move_to_end(key)
        if len(_route_cache) > _ROUTE_CACHE_LIMIT:
            _route_cache.popitem(last=False)
        return context


def resolve_candidate_route(candidate: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    artist = normalized_name(candidate.get("artist"))
    context = _packet_route_context(packet)
    selected = context["by_artist"].get(artist)
    if selected is not None:
        return {**selected,
                "path": ([selected["anchor_artist"], candidate["artist"]] if selected["path"] is None else list(selected["path"])),
                "sources": list(selected["sources"])}
    active = context["active"]
    source_tag_names = context["source_tag_names"]
    evidence = candidate.get("style_evidence") or {}
    tagged = (evidence.get("status") == "supported" and evidence.get("scope") in {"track", "album", "artist"}
              and evidence.get("url") in (candidate.get("sources") or []) and bool(evidence.get("retrieved_at")))
    sourced_style = any(item.get("claim_type") == "style" and item.get("url") in (candidate.get("sources") or [])
                        for item in candidate.get("evidence_items", []) if isinstance(item, dict))
    candidate_refs = set(candidate.get("style_refs") or []) if sourced_style else set()
    if not candidate_refs:
        candidate_refs = {
            tag.get("style_ref")
            for tag in evidence.get("tags", []) if tagged
            if isinstance(tag, dict) and tag.get("style_ref")
        }
    candidate_tag_names = {
        normalized_name(tag.get("tag"))
        for tag in evidence.get("tags", []) if tagged
        if isinstance(tag, dict) and tag.get("tag")
    }
    similarity = candidate.get("provider_similarity") or {}
    # 召回阶段将公开相似艺人列表较远的位置归为探索路径。重判时不能只看
    # 风格标签，否则会把有据可查的探索候选降级，导致三组严格配比无解。
    try:
        neighbor_rank = int(similarity.get("rank") or 0)
    except (TypeError, ValueError):
        neighbor_rank = 0
    similarity_url = similarity.get("url")
    evidenced_neighbor = (
        neighbor_rank > 0
        and normalized_name(similarity.get("artist")) == artist
        and bool(similarity.get("seed"))
        and isinstance(similarity_url, str)
        and similarity_url == "https://www.last.fm/music/" + quote(str(similarity["seed"]), safe="") + "/+similar"
        and similarity_url in (candidate.get("sources") or [])
    )
    novel_style = (bool(active) and any(ref not in active for ref in candidate_refs)) or (
        bool(source_tag_names) and any(name not in source_tag_names for name in candidate_tag_names)
    )
    # A verified top similar artist is the closest public style-neighbor route.
    # Its incidental new tag must not turn every large-playlist neighbor into
    # exploration; ranks farther away retain the exploration route.
    try:
        neighbor_match = float(similarity.get("match"))
    except (TypeError, ValueError):
        neighbor_match = None
    summary_mode = str(packet.get("analysis_mode") or "") in {"taste_summary", "artist_summary"}
    strong_first_neighbor = evidenced_neighbor and (
        neighbor_rank <= (2 if summary_mode else 1)
    ) and (
        neighbor_match is None or neighbor_match >= 0.8
    )
    route = ("style_neighbor" if strong_first_neighbor else
             "exploration" if (evidenced_neighbor and neighbor_rank > 1) or novel_style
             else "style_neighbor")
    return {"candidate_type": route,
            "anchor_ref": None, "anchor_artist": None, "path": [], "strength": 0.0,
            "frequency_strength": 0.0, "sources": [],
            "verification_scope": "public_artist_similarity" if evidenced_neighbor else "public_style_tags" if candidate_refs else "no_style_evidence"}
