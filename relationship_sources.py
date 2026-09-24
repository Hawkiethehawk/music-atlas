"""Bounded public artist relationships for Music Atlas.

MusicBrainz is the factual primary source. Discogs is used only when a
MusicBrainz URL relationship provides an unambiguous Discogs artist id.
Wikidata is used only to resolve an input name to external ids; it never
creates a relationship on its own.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from contracts import normalized_name, utc_now

try:
    import fcntl  # Linux: coordinate MusicBrainz calls across isolated Web jobs.
except ImportError:  # pragma: no cover - Windows development fallback.
    fcntl = None

USER_AGENT = "MusicAtlas/1.0 (https://github.com/Hawkiethehawk/music-atlas)"
DISCOGS_ID = re.compile(r"discogs\.com/(?:[a-z]{2}/)?artist/(\d+)", re.I)
COLLABORATION_TYPES = {
    "collaboration",
    "supporting musician",
    "instrumental supporting musician",
    "vocal supporting musician",
}
MUSICBRAINZ_INTERVAL = 1.05
_FALLBACK_MB_LOCK = threading.Lock()
_FALLBACK_MB_LAST_REQUEST = 0.0


class RelationshipClient:
    """Small cached HTTP client with provider-specific limits and retries."""

    def __init__(self, cache_dir: Path, *, seconds: float = 80, max_requests: int = 30,
                 rate_lock_path: Path | None = None):
        self.cache_dir = Path(cache_dir)
        self.rate_lock_path = Path(rate_lock_path or Path(tempfile.gettempdir()) / "music-atlas-musicbrainz-rate.lock")
        self.deadline = time.monotonic() + seconds
        self.max_requests = max_requests
        self.requests = 0
        self.events: list[dict] = []
        self.last_request: dict[str, float] = {}
        self._lock = threading.RLock()

    def _event(self, event: dict) -> None:
        with self._lock:
            self.events.append(event)

    def _musicbrainz_delay(self) -> float | None:
        """Reserve one host-wide request slot without holding a lock during HTTP I/O."""
        if fcntl is None:
            global _FALLBACK_MB_LAST_REQUEST
            with _FALLBACK_MB_LOCK:
                now = time.monotonic()
                slot = max(now, _FALLBACK_MB_LAST_REQUEST + MUSICBRAINZ_INTERVAL)
                if slot >= self.deadline:
                    return None
                _FALLBACK_MB_LAST_REQUEST = slot
                return slot - now
        self.rate_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.rate_lock_path.open("a+", encoding="ascii") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                try:
                    previous = float(handle.read().strip() or 0)
                except ValueError:
                    previous = 0.0
                now = time.time()
                slot = max(now, previous + MUSICBRAINZ_INTERVAL)
                delay = slot - now
                if time.monotonic() + delay >= self.deadline:
                    return None
                handle.seek(0)
                handle.truncate()
                handle.write(str(slot))
                handle.flush()
                return delay
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, url: str) -> dict | None:
        cache_path = self.cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("url") == url and 0 <= time.time() - float(cached["saved"]) < 7 * 86400:
                self._event({"url": url, "status": "cache_hit", "retrieved_at": cached["retrieved_at"]})
                return cached["data"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

        host = (urlparse(url).hostname or "").casefold()
        for attempt in range(3):
            with self._lock:
                if self.requests >= self.max_requests or time.monotonic() >= self.deadline:
                    self._event({"url": url, "status": "budget_exhausted"})
                    return None
                if host == "musicbrainz.org":
                    delay = self._musicbrainz_delay()
                else:
                    delay = min(1.5, 0.4 * (2 ** (attempt - 1))) if attempt else 0.0
                if delay is None or time.monotonic() + delay >= self.deadline:
                    self._event({"url": url, "status": "budget_exhausted"})
                    return None
                self.requests += 1
                self.last_request[host] = time.monotonic() + delay
            if delay:
                time.sleep(delay)
            started = time.monotonic()
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                timeout = max(0.2, min(8.0, self.deadline - started))
                with urlopen(request, timeout=timeout) as response:
                    data = json.loads(response.read(4_000_000).decode("utf-8"))
                    status = response.status
                if not isinstance(data, dict) or data.get("error"):
                    raise ValueError("provider returned an invalid object")
                stamp = utc_now()
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({
                    "url": url, "saved": time.time(), "retrieved_at": stamp, "data": data,
                }, ensure_ascii=False), encoding="utf-8")
                self._event({"url": url, "status": "ok", "http_status": status,
                             "retrieved_at": stamp, "seconds": round(time.monotonic() - started, 3)})
                return data
            except HTTPError as exc:
                self._event({"url": url, "status": "failed", "http_status": exc.code,
                             "attempt": attempt + 1, "error": str(exc)[:160]})
                if exc.code not in {429, 500, 502, 503, 504}:
                    return None
            except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                self._event({"url": url, "status": "failed", "attempt": attempt + 1,
                             "error": str(exc)[:160]})
        return None

    def wikidata_identity(self, name: str) -> dict | None:
        search_url = "https://www.wikidata.org/w/api.php?" + urlencode({
            "action": "wbsearchentities", "search": name, "language": "en", "type": "item",
            "limit": 8, "format": "json",
        })
        found = self.get(search_url) or {}
        qids = [item.get("id") for item in found.get("search", [])
                if normalized_name(item.get("label")) == normalized_name(name) and item.get("id")]
        if not qids:
            return None
        entity_url = "https://www.wikidata.org/w/api.php?" + urlencode({
            "action": "wbgetentities", "ids": "|".join(qids[:8]), "props": "claims|labels|aliases",
            "languages": "en", "format": "json",
        })
        entities = (self.get(entity_url) or {}).get("entities", {})
        matches = []
        for qid in qids:
            entity = entities.get(qid, {})
            names = {normalized_name(entity.get("labels", {}).get("en", {}).get("value"))}
            names.update(normalized_name(item.get("value")) for item in entity.get("aliases", {}).get("en", []))
            mbids = _claim_values(entity, "P434")
            if normalized_name(name) in names and len(mbids) == 1:
                matches.append({"qid": qid, "mbid": str(mbids[0]),
                                "discogs_id": str(_claim_values(entity, "P1953")[0]) if _claim_values(entity, "P1953") else None})
        unique = {item["mbid"]: item for item in matches}
        return next(iter(unique.values())) if len(unique) == 1 else None

    def musicbrainz_search(self, name: str) -> dict | None:
        url = "https://musicbrainz.org/ws/2/artist/?" + urlencode({
            "query": f'artist:"{name}"', "limit": 8, "fmt": "json",
        })
        data = self.get(url) or {}
        exact = [item for item in data.get("artists", [])
                 if normalized_name(item.get("name")) == normalized_name(name)
                 and int(item.get("score", 0) or 0) >= 95]
        ids = {item.get("id") for item in exact if item.get("id")}
        if len(ids) != 1:
            return None
        match = next(item for item in exact if item.get("id") in ids)
        return {"mbid": match["id"], "qid": None, "discogs_id": None}

    def resolve_artist(self, name: str) -> dict | None:
        identity = self.wikidata_identity(name) or self.musicbrainz_search(name)
        if not identity:
            return None
        url = f"https://musicbrainz.org/ws/2/artist/{identity['mbid']}?inc=artist-rels+url-rels&fmt=json"
        record = self.get(url)
        if not record or normalized_name(record.get("name")) != normalized_name(name):
            return None
        return {**identity, "record": record, "url": f"https://musicbrainz.org/artist/{identity['mbid']}"}

    def musicbrainz_artist(self, mbid: str) -> dict | None:
        url = f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=artist-rels+url-rels&fmt=json"
        record = self.get(url)
        if not record:
            return None
        return {"mbid": mbid, "record": record, "url": f"https://musicbrainz.org/artist/{mbid}",
                "discogs_id": _discogs_id(record)}

    def discogs_artist(self, artist_id: str | None) -> dict | None:
        if not artist_id or not str(artist_id).isdigit():
            return None
        return self.get(f"https://api.discogs.com/artists/{artist_id}")


def _claim_values(entity: dict, prop: str) -> list:
    values = []
    for claim in entity.get("claims", {}).get(prop, []):
        snak = claim.get("mainsnak", {})
        if claim.get("rank") == "deprecated" or snak.get("snaktype") != "value":
            continue
        value = snak.get("datavalue", {}).get("value")
        if value is not None:
            values.append(value)
    return values


def _discogs_id(record: dict) -> str | None:
    for relation in record.get("relations", []):
        if relation.get("target-type") != "url" or relation.get("type") != "discogs":
            continue
        match = DISCOGS_ID.search(str(relation.get("url", {}).get("resource") or ""))
        if match:
            return match.group(1)
    return None


def _discogs_marker(name: object) -> str:
    # Discogs appends disambiguation suffixes such as “Palms (4)”.
    return normalized_name(re.sub(r"\s+\(\d+\)$", "", str(name or "").strip()))


def _status(relation: dict) -> str:
    return "historical" if relation.get("ended") else "active"


def _cross_status(mb_status: str, discogs_item: dict | None) -> tuple[str, bool]:
    if not discogs_item or not isinstance(discogs_item.get("active"), bool):
        return mb_status, False
    discogs_status = "active" if discogs_item["active"] else "historical"
    return (mb_status, False) if discogs_status == mb_status else ("unknown", True)


def _role(relation: dict) -> str:
    ignored = {"original", "additional", "guest", "founder"}
    values = [str(item).strip() for item in relation.get("attributes", [])
              if str(item).strip().casefold() not in ignored]
    return ", ".join(values) or "member"


def _member_sort_key(item: dict) -> tuple:
    role = str(item.get("role") or "").casefold()
    return (item.get("status") != "active", "lead vocal" not in role, "vocal" not in role,
            normalized_name(item.get("name")))


def select_island_seeds(packet: dict, limit: int = 9, per_island: int = 2) -> list[str]:
    """Choose representative artists per Agent island (distinct, at most per_island each)."""

    tracks = packet.get("favorite_tracks", [])
    rank = {normalized_name(row.get("artist")): index
            for index, row in enumerate(packet.get("primary_distribution", []))}
    used: set[str] = set()
    seeds: list[str] = []
    for island in packet.get("agent_islands", []):
        counts = Counter()
        display: dict[str, str] = {}
        for record_id in island.get("record_ids", []):
            if type(record_id) is not int or not 0 <= record_id < len(tracks):
                continue
            artist = str(tracks[record_id].get("artist") or "").strip()
            marker = normalized_name(artist)
            if marker:
                counts[marker] += 1
                display.setdefault(marker, artist)
        ordered = sorted(counts, key=lambda marker: (-counts[marker], rank.get(marker, 10**6), marker))
        picked = 0
        for marker in ordered:
            if marker in used:
                continue
            used.add(marker)
            seeds.append(display[marker])
            picked += 1
            if picked >= per_island or len(seeds) >= limit:
                break
        if len(seeds) >= limit:
            break
    for row in packet.get("primary_distribution", []):
        artist = str(row.get("artist") or "").strip()
        marker = normalized_name(artist)
        if artist and marker not in used:
            used.add(marker)
            seeds.append(artist)
        if len(seeds) >= limit:
            break
    return seeds[:limit]


def collect_relationships(seed_artists: list[str], client: RelationshipClient,
                          *, member_expansions: int = 2, projects_per_member: int = 4,
                          workers: int = 3) -> dict:
    """Collect auditable member, collaboration and shared-project paths."""

    artists: dict[str, dict] = {}
    unresolved: list[str] = []

    def collect_seed(seed: str) -> tuple[str, dict | None]:
        identity = client.resolve_artist(seed)
        if not identity:
            return seed, None
        record = identity["record"]
        base_url = identity["url"]
        discogs_id = identity.get("discogs_id") or _discogs_id(record)
        discogs = client.discogs_artist(discogs_id)
        discogs_members = {_discogs_marker(item.get("name")): item for item in (discogs or {}).get("members", [])}
        members = []
        collaborators = []
        for relation in record.get("relations", []):
            target = relation.get("artist") or {}
            if not target.get("name") or not target.get("id"):
                continue
            if relation.get("type") == "member of band" and relation.get("direction") == "backward":
                marker = normalized_name(target["name"])
                cross = discogs_members.get(marker)
                sources = [base_url, f"https://musicbrainz.org/artist/{target['id']}"]
                if cross and discogs_id:
                    sources.append(f"https://www.discogs.com/artist/{discogs_id}")
                status, source_conflict = _cross_status(_status(relation), cross)
                members.append({
                    "name": target["name"], "role": _role(relation), "status": status,
                    "begin": relation.get("begin"), "end": relation.get("end"),
                    "confidence": "high" if cross and not source_conflict else "medium",
                    "cross_checked": bool(cross), "source_conflict": source_conflict,
                    "retrieved_at": utc_now(),
                    "external_ids": {"musicbrainz": target["id"], **({"discogs": str(cross["id"])} if cross else {})},
                    "sources": list(dict.fromkeys(sources)),
                })
            elif relation.get("type") in COLLABORATION_TYPES:
                collaborators.append({
                    "name": target["name"], "relation": relation["type"], "status": _status(relation),
                    "begin": relation.get("begin"), "end": relation.get("end"), "confidence": "high",
                    "retrieved_at": utc_now(), "external_ids": {"musicbrainz": target["id"]},
                    "sources": [base_url, f"https://musicbrainz.org/artist/{target['id']}"],
                })
        members.sort(key=_member_sort_key)
        related_projects: list[dict] = []
        for member in members[:max(0, member_expansions)]:
            mbid = member.get("external_ids", {}).get("musicbrainz")
            person = client.musicbrainz_artist(mbid) if mbid else None
            if not person:
                continue
            person_record = person["record"]
            person_discogs_id = person.get("discogs_id")
            person_discogs = client.discogs_artist(person_discogs_id)
            discogs_groups = {_discogs_marker(item.get("name")): item for item in (person_discogs or {}).get("groups", [])}
            projects = []
            for relation in person_record.get("relations", []):
                target = relation.get("artist") or {}
                if relation.get("type") != "member of band" or relation.get("direction") != "forward":
                    continue
                name = str(target.get("name") or "").strip()
                target_id = target.get("id")
                if not name or not target_id or normalized_name(name) == normalized_name(seed):
                    continue
                cross = discogs_groups.get(normalized_name(name))
                status, source_conflict = _cross_status(_status(relation), cross)
                sources = [base_url, person["url"], f"https://musicbrainz.org/artist/{target_id}"]
                if cross and person_discogs_id:
                    sources.append(f"https://www.discogs.com/artist/{person_discogs_id}")
                projects.append({
                    "name": name, "person": member["name"], "relation": "shared_member",
                    "role": _role(relation), "status": status,
                    "begin": relation.get("begin"), "end": relation.get("end"),
                    "confidence": "high" if cross and not source_conflict else "medium",
                    "cross_checked": bool(cross), "source_conflict": source_conflict,
                    "retrieved_at": utc_now(),
                    "external_ids": {"musicbrainz": target_id, **({"discogs": str(cross["id"])} if cross else {})},
                    "sources": list(dict.fromkeys(sources)),
                })
            projects.sort(key=lambda item: (item["status"] != "active", not item["cross_checked"], normalized_name(item["name"])))
            related_projects.extend(projects[:max(0, projects_per_member)])
        deduplicated = {}
        for project in related_projects:
            marker = normalized_name(project["name"])
            current = deduplicated.get(marker)
            if current is None or (project["confidence"] == "high" and current["confidence"] != "high"):
                deduplicated[marker] = project
        related_projects = list(deduplicated.values())
        sources = [base_url]
        if discogs_id:
            sources.append(f"https://www.discogs.com/artist/{discogs_id}")
        for item in [*members, *collaborators, *related_projects]:
            sources.extend(item.get("sources", []))
        vocalists = [{key: value for key, value in member.items() if key != "external_ids"}
                     for member in members if "vocal" in str(member.get("role") or "").casefold()
                     and "background" not in str(member.get("role") or "").casefold()]
        return seed, {
            "canonical_name": record.get("name") or seed,
            "entity_type": "band" if record.get("type") == "Group" else "solo_artist" if record.get("type") == "Person" else "unknown",
            "external_ids": {"musicbrainz": identity["mbid"], **({"wikidata": identity["qid"]} if identity.get("qid") else {}),
                             **({"discogs": str(discogs_id)} if discogs_id else {})},
            "members": members,
            "lead_vocalists": vocalists,
            "collaborators": collaborators,
            "related_projects": related_projects,
            "sources": list(dict.fromkeys(sources)),
            "retrieved_at": utc_now(),
            "research_origin": "public_catalog",
        }
    # Independent artists overlap their Wikidata/Discogs waits; the shared client
    # still enforces its HTTP count/deadline and MusicBrainz's host-wide cadence.
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(seed_artists) or 1))) as pool:
        for seed, artist in pool.map(collect_seed, seed_artists):
            if artist is None:
                unresolved.append(seed)
            else:
                artists[seed] = artist
    return {
        "schema_version": "2.0", "catalog_type": "live_public_relations",
        "generated_at": utc_now(), "seed_artists": seed_artists,
        "artists": artists, "unresolved_artists": unresolved,
        "requests": list(client.events),
    }
