"""Run the guarded Music Atlas workflow for the local web API.

The browser only supplies a public playlist URL.  External Skill executors
remain server configuration, and every stage still passes through the same
JSON contracts as the CLI.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from analysis_agent import execute_analysis_research
from agent_prompt import prepare_agent_context, prompt_size_telemetry
from contracts import (ContractError, normalized_name, read_json, stable_hash, track_key, utc_now,
                       validate_playlist_snapshot, write_json)
from musician_analyzer import analyze_and_validate, write_coverage_report
from platform_discovery import discover_platform_candidates, hydrate_netease_covers
from recommender import rank_bundle
from source_adapters import build_snapshot, save_snapshot
from workflow import ROOT, export_apple_playlist_file
from skill_runner import run_skill
from taste_summary import TASTE_BATCH_SIZE, resolve_analysis_mode
from web_view_model import export_web_payload
from review import review_candidates, review_groups
from playlist_source import (
    PUBLIC_HOSTS,
    SOURCE_KINDS,
    resolve_netease_source as _resolve_netease_source,
    source_id as _source_id,
    validate_url as _validate_url,
)

_EMIT_LOCK = threading.Lock()


def emit(event: str, **payload: object) -> None:
    message = {"event": event, "at": utc_now(), **payload}
    with _EMIT_LOCK:
        print(json.dumps(message, ensure_ascii=False), flush=True)


def emit_progress(message: dict[str, object]) -> None:
    """Forward detailed worker telemetry without exposing prompts or secrets."""

    if not isinstance(message, dict):
        return
    event = str(message.get("event") or "progress")
    payload = {key: value for key, value in message.items() if key != "event"}
    emit(event, **payload)


def _publish_payload(source: Path, target: Path) -> None:
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


DEFAULT_AWAIT_LIMIT_TIMEOUT_SECONDS = 1800
AWAIT_LIMIT_MAX_TIMEOUT_SECONDS = 86400
LIMIT_REQUEST_FILENAME = "requested_track_limit.json"
LIMIT_WAIT_POLL_SECONDS = 0.5
TRACK_PERCENTILE_OPTIONS = (0.25, 0.5, 1.0)
ATLAS_GROUP_COUNT = 3
MIN_RECOMMENDATION_CANDIDATES = 30

# 同一任务失败重试时可复用的阶段产物（断点续跑）。
RESUMABLE_ARTIFACTS = frozenset({
    "web_job_state.json", "snapshot.json", "analysis_manifest.json",
    "musician_analysis.json", "lastfm_analysis.json", "coverage_report.json",
    "track_facts.json", "public_relations.json", "public_style_profiles.json",
    "platform_discovery.json", "candidate_pool.json", "review_report.json",
    "recommendation_bundle.json", "web_payload.json", "web_job_report.json", "web_preview.json",
    "web_selection.json",
    "analysis_research", "agent", "source",
})
RESUMABLE_ARTIFACT_PREFIXES = ("recommendation_bundle_group_", "web_payload_group_",
                               "musician_analysis.", "web_config.snapshot.", "recommendation_policy.snapshot.")


def _is_resumable_artifact(name: str) -> bool:
    """判断目录项是否为已知阶段产物（允许断点续跑时目录非空）。"""
    return name in RESUMABLE_ARTIFACTS or any(name.startswith(prefix) for prefix in RESUMABLE_ARTIFACT_PREFIXES)

RECOMMENDATION_HISTORY_DAYS = 7
DEFAULT_WORKFLOW_TIME_BUDGET_SECONDS = 120
AGENT_TIME_BUDGET_SECONDS = 45
SMALL_ANALYSIS_TIME_BUDGET_SECONDS = 70
SUMMARY_AGENT_TIME_BUDGET_SECONDS = 78
SUMMARY_DOWNSTREAM_RESERVE_SECONDS = 30
DISCOVERY_TIME_BUDGET_SECONDS = 45


def analysis_verify_concurrency(track_count: int) -> int:
    """按歌单规模选择公开平台核验并发（实测平台允许较高并发，小歌单无需过度并发）。"""
    return 12 if track_count <= 50 else 20


def analysis_summary_timeout(track_count: int, base: int, remaining: float | None = None) -> int:
    """Allow a slower first summary, but leave time for ranking, review and publication."""
    configured = int(base or SUMMARY_AGENT_TIME_BUDGET_SECONDS)
    timeout = max(1, min(configured, SUMMARY_AGENT_TIME_BUDGET_SECONDS))
    if remaining is not None:
        available = int(remaining) - SUMMARY_DOWNSTREAM_RESERVE_SECONDS
        if available < 1:
            raise ContractError("Step 2 摘要剩余时间不足，无法为推荐、审计和发布预留 30 秒")
        timeout = min(timeout, available)
    return max(1, timeout)


def analysis_style_timeout(base: int, remaining: float) -> int:
    """Bound first-run style analysis while preserving Step 3/export time."""
    available = int(remaining) - SUMMARY_DOWNSTREAM_RESERVE_SECONDS
    if available < 1:
        raise ContractError("Step 2 文案分析剩余时间不足，无法为推荐、审计和发布预留 30 秒")
    return min(int(base or SMALL_ANALYSIS_TIME_BUDGET_SECONDS),
               SMALL_ANALYSIS_TIME_BUDGET_SECONDS, available)


def _workflow_remaining(deadline: float, stage: str, *, reserve: float = 0.0) -> float:
    remaining = deadline - time.monotonic() - reserve
    if remaining <= 0:
        raise ContractError(f"全流程 120 秒预算已耗尽（{stage}）")
    return remaining


def _commit_completed_workflow(
    *,
    payload_path: Path,
    current_data_path: Path | None,
    history_path: Path,
    history: dict[str, Any],
    groups: list[dict[str, Any]],
    playlist_identity: dict[str, Any],
    report_path: Path,
    report: dict[str, Any],
    deadline: float,
) -> None:
    """Stage the success artifacts, then commit them only inside the time budget."""
    token = f"{os.getpid()}.{time.time_ns()}"
    staged: list[Path] = []
    replacements: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        if current_data_path is not None:
            target = current_data_path.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            payload_stage = target.with_name(f".{target.name}.{token}.tmp")
            staged.append(payload_stage)
            shutil.copyfile(payload_path, payload_stage)
            replacements.append((payload_stage, target))

        history_stage = history_path.with_name(f".{history_path.name}.{token}.tmp")
        report_stage = report_path.with_name(f".{report_path.name}.{token}.tmp")
        staged.extend((history_stage, report_stage))
        _save_recommendation_history(history_stage, history, groups, playlist_identity)
        write_json(report_stage, report)

        for _temporary, target in replacements:
            if target.exists():
                backup = target.with_name(f".{target.name}.{token}.bak")
                shutil.copyfile(target, backup)
                staged.append(backup)
                backups[target] = backup
            else:
                backups[target] = None
        _workflow_remaining(deadline, "最终 Atlas 提交", reserve=0.25)

        replacements.extend(((history_stage, history_path), (report_stage, report_path)))
        for _temporary, target in replacements[len(backups):]:
            if target.exists():
                backup = target.with_name(f".{target.name}.{token}.bak")
                shutil.copyfile(target, backup)
                staged.append(backup)
                backups[target] = backup
            else:
                backups[target] = None
        for temporary, target in replacements:
            _workflow_remaining(deadline, "最终 Atlas 提交", reserve=0.25)
            os.replace(temporary, target)
            committed.append(target)
        _workflow_remaining(deadline, "提交确认")
    except BaseException:
        for target in reversed(committed):
            backup = backups.get(target)
            if backup is None:
                target.unlink(missing_ok=True)
            elif backup.exists():
                os.replace(backup, target)
        raise
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)


def taste_source_tags(packet: dict[str, Any]) -> dict[str, Any]:
    """摘要模式：从摘要分析包的逐曲分配构造 source_tags（不联网），供兴趣岛归纳与展示使用。"""
    assignments = packet.get("track_style_assignments") or []
    records: list[dict[str, Any]] = []
    for item in assignments:
        refs = [str(ref) for ref in (item.get("style_refs") or []) if str(ref).startswith("style:")]
        primary = str(item.get("primary_style_ref") or "")
        if primary and primary not in refs:
            refs = [primary, *refs]
        url = next((str(value) for value in (item.get("sources") or []) if str(value).startswith("http")), "")
        records.append({
            "track_key": str(item.get("track_key") or ""),
            "scope": "taste_artist" if refs else "unknown",
            "url": url,
            "tags": [{"tag": ref, "style_ref": ref} for ref in refs],
            "raw_tags": [],
            "retrieved_at": packet.get("generated_at"),
            "status": "supported" if refs else "unavailable",
        })
    return {"provider": "taste_summary", "axis_policy": "removed", "records": records, "requests": []}


def _playlist_history_path(source_report: dict[str, Any], snapshot: dict[str, Any], runtime_dir: Path) -> Path:
    # Web jobs are isolated by authenticated user.  A shared playlist URL may
    # legitimately produce different recommendations for different accounts,
    # so the seven-day de-duplication cache must never cross user boundaries.
    user_id = str(os.environ.get("MUSIC_ATLAS_USER_ID", "anonymous") or "anonymous")
    identity = f"{user_id}:{source_report.get('kind', '')}:{snapshot.get('playlist_id', '')}"
    digest = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()
    base = runtime_dir.parent.parent if runtime_dir.parent.name in {"web-jobs", "jobs"} else runtime_dir.parent
    return base / "recommendation-history" / f"{digest}.json"


def _recent_recommendation_history(path: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    threshold = now - timedelta(days=RECOMMENDATION_HISTORY_DAYS)
    if not path.is_file():
        return {"entries": [], "canonical_track_ids": set(), "track_keys": set()}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"一周推荐缓存无法解析，为避免重复推荐已停止：{exc}") from exc
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ContractError("一周推荐缓存结构无效，为避免重复推荐已停止")
    active: list[dict[str, Any]] = []
    ids: set[str] = set()
    keys: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("generated_at"), str):
            continue
        try:
            stamp = datetime.fromisoformat(entry["generated_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp < threshold:
            continue
        active.append(entry)
        ids.update(str(value) for value in entry.get("canonical_track_ids", []) if value)
        keys.update(str(value) for value in entry.get("track_keys", []) if value)
    return {"entries": active, "canonical_track_ids": ids, "track_keys": keys}


def _exclude_recent_recommendations(candidates: list[dict[str, Any]], history: dict[str, Any]) -> list[dict[str, Any]]:
    ids = history.get("canonical_track_ids", set())
    keys = history.get("track_keys", set())
    return [candidate for candidate in candidates
            if str(candidate.get("canonical_track_id") or "") not in ids
            and track_key(candidate.get("title"), candidate.get("artist")) not in keys]


def _merge_candidates(primary: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并候选池（primary 优先），按 canonical id 与 track_key 去重。"""
    seen_ids = {str(item.get("canonical_track_id") or "").casefold() for item in primary}
    seen_keys = {track_key(item.get("title"), item.get("artist")) for item in primary}
    merged = list(primary)
    for item in extra:
        canonical = str(item.get("canonical_track_id") or "").casefold()
        key = track_key(item.get("title"), item.get("artist"))
        if not canonical or canonical in seen_ids or key in seen_keys:
            continue
        seen_ids.add(canonical)
        seen_keys.add(key)
        merged.append(item)
    return merged


def _merge_prefetched_candidates(
    base: list[dict[str, Any]], relations: list[dict[str, Any]],
    history: dict[str, Any], hard_limit: int, *,
    supplemental: list[dict[str, Any]] | None = None,
    packet: dict[str, Any] | None = None,
    needed: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Retain scarce verified relations before bounding the in-run discovery pool."""
    combined = _merge_candidates(relations, _merge_candidates(base, supplemental or []))
    combined = _exclude_recent_recommendations(combined, history)
    if packet is not None and needed is not None:
        return _bounded_verified_routes(combined, packet, needed, hard_limit)
    return combined[:hard_limit]


def _bounded_verified_routes(
    rows: list[dict[str, Any]], packet: dict[str, Any],
    needed: dict[str, int], hard_limit: int,
) -> list[dict[str, Any]]:
    """Keep scarce routes when an independently verified window fills the pool."""
    from collections import Counter
    from candidate_routes import resolve_candidate_route

    bounded = rows[:hard_limit]
    if len(rows) <= hard_limit:
        return bounded

    def route(item):
        return str(resolve_candidate_route(item, packet).get("candidate_type")
                   or item.get("candidate_type"))

    counts = Counter(route(item) for item in bounded)
    for item in rows[hard_limit:]:
        kind = route(item)
        if counts[kind] >= needed.get(kind, 0):
            continue
        for index in range(len(bounded) - 1, -1, -1):
            old = route(bounded[index])
            if counts[old] > needed.get(old, 0):
                bounded[index] = item
                counts[old] -= 1
                counts[kind] += 1
                break
    return bounded


def _prefetch_large_playlist_window(
    packet: dict[str, Any], history: dict[str, Any], *,
    hard_limit: int, concurrency: int, deadline: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Research artists 17-32 in this run while Step 2 validates the same snapshot.

    This is only a source-bound, verified candidate pool; it cannot publish
    anything before the final Step 2 packet passes its gate.
    """
    from lastfm_pipeline import LastFM, discover

    distribution = packet.get("primary_distribution") or []
    if len(distribution) < 32 or int(packet.get("source_track_count") or 0) < 500:
        return [], {}
    remaining = deadline - time.monotonic() - 12
    if remaining < 8:
        return [], {}
    probe = dict(packet)
    probe["primary_distribution"] = [*distribution[16:32], *distribution[:16], *distribution[32:]]
    # Relation evidence has its own prefetch lane; this window adds only public
    # similarity/track identities, never guesses a relation from a name.
    probe["entities"] = []
    client = LastFM(ROOT / "runtime" / "lastfm-cache", seconds=min(20, remaining))
    rows, report = discover(
        probe, client, max_candidates=min(60, hard_limit),
        similar_limit=4, top_track_limit=3,
        excluded_track_keys=history.get("track_keys", set()),
        excluded_canonical_track_ids=history.get("canonical_track_ids", set()),
        concurrency=concurrency,
    )
    return rows, {"anchor_start": 16, "verified_count": len(rows),
                  "verification_rejected_count": report.get("verification_rejected_count", 0)}


def _fill_prefetch_route_shortfall(
    packet: dict[str, Any], prefetched: list[dict[str, Any]],
    history: dict[str, Any], needed: dict[str, int], *,
    hard_limit: int, concurrency: int, deadline: float, on_attempt=None,
    skip_anchor_starts: frozenset[int] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep verified prefetch rows and research new anchors for missing routes."""
    from collections import Counter
    from candidate_routes import resolve_candidate_route
    from lastfm_pipeline import LastFM, discover

    def route(item):
        return str(resolve_candidate_route(item, packet).get("candidate_type")
                   or item.get("candidate_type"))

    def shortage(rows):
        counts = Counter(route(item) for item in rows)
        return {kind: count - counts[kind] for kind, count in needed.items()
                if counts[kind] < count}

    candidates = _exclude_recent_recommendations(prefetched, history)[:hard_limit]
    missing = shortage(candidates)
    attempts = []
    if not missing:
        return candidates, {"attempts": attempts, "route_shortfall": missing}

    if "musician_relation" in missing:
        # Similar-artist windows cannot produce source-backed musician relations.
        # Only deepen the verified public projects already attached to this
        # packet; do not substitute a style neighbor for the strict route quota.
        projects = [project for entity in packet.get("entities", [])
                    for project in entity.get("related_projects", [])
                    if project.get("name") and project.get("person")
                    and project.get("sources")]
        remaining = deadline - time.monotonic() - 12
        if not projects or remaining < 8:
            return candidates, {"attempts": attempts, "route_shortfall": missing}
        client = LastFM(ROOT / "runtime" / "lastfm-cache", seconds=min(18, remaining))
        discovered, report = discover(
            packet, client, max_candidates=min(60, hard_limit),
            relation_project_limit=16, relation_top_track_limit=12,
            include_similarity=False,
            excluded_track_keys=history.get("track_keys", set()),
            excluded_canonical_track_ids=history.get("canonical_track_ids", set()),
            concurrency=concurrency,
        )
        merged = _exclude_recent_recommendations(
            _merge_candidates(candidates, discovered), history)
        candidates = _bounded_verified_routes(merged, packet, needed, hard_limit)
        missing = shortage(candidates)
        attempts.append({"route": "musician_relation", "verified_count": len(discovered),
                         "candidate_count": len(candidates), "route_shortfall": dict(missing),
                         "verification_rejected_count": report.get("verification_rejected_count", 0)})
        if "musician_relation" in missing or not missing:
            return candidates, {"attempts": attempts, "route_shortfall": missing}

    distribution = packet.get("primary_distribution") or []
    # The prefetch already queried the first 16 artists; avoid repeating them.
    windows = [(start, distribution[start:start + 16])
               for start in range(16, min(len(distribution), 64), 16)
               if start not in skip_anchor_starts]
    if not windows and distribution and len(distribution) <= 16:
        windows = [(0, distribution)]  # use a deeper lane for small rosters
    for start, anchors in windows:
        remaining = deadline - time.monotonic() - 12  # rank/review/publication reserve
        if remaining < 8:
            break
        if on_attempt:
            on_attempt(start, dict(missing))
        probe = dict(packet)
        probe["primary_distribution"] = [*anchors, *distribution[:start],
                                         *distribution[start + len(anchors):]]
        client = LastFM(ROOT / "runtime" / "lastfm-cache", seconds=min(20, remaining))
        discovered, report = discover(
            probe, client, max_candidates=min(60, hard_limit),
            similar_limit=12 if start == 0 else 4,
            top_track_limit=8 if start == 0 else 3,
            excluded_track_keys=history.get("track_keys", set()),
            excluded_canonical_track_ids=history.get("canonical_track_ids", set()),
            concurrency=concurrency,
        )
        merged = _exclude_recent_recommendations(
            _merge_candidates(candidates, discovered), history)
        candidates = _bounded_verified_routes(merged, packet, needed, hard_limit)
        missing = shortage(candidates)
        attempts.append({"anchor_start": start, "verified_count": len(discovered),
                         "candidate_count": len(candidates), "route_shortfall": dict(missing),
                         "verification_rejected_count": report.get("verification_rejected_count", 0)})
        if not missing:
            break
    return candidates, {"attempts": attempts, "route_shortfall": missing}


def _reallocate_route_mix_for_available_evidence(
    packet: dict[str, Any], candidates: list[dict[str, Any]], *,
    group_count: int = ATLAS_GROUP_COUNT,
) -> dict[str, Any] | None:
    """将缺失的候选路线配额安全转给仍有公开证据的路线。

    ``strict_recall_mix`` 约束的是每组的候选类型配比，而不是要求系统
    为不存在的关系证据造出关系候选。关系资料在小歌单上可能完全没有
    无歧义结果；只要候选总量足够，就按原策略的相对权重把该路线的空缺
    转给可用路线，并保持三组使用同一套有效配额。

    返回的报告会保留原始配额、可用数量与重分配后的配额，便于运行报告
    解释“关系资料缺失但推荐仍完成”的原因。无缺口时返回 ``None``。
    """
    if packet.get("strict_recall_mix") is not True:
        return None
    if group_count <= 0 or not isinstance(packet.get("recommendation_policy"), dict):
        return None

    from collections import Counter
    from candidate_routes import resolve_candidate_route
    from contracts import recall_mix_ratios, target_counts

    target = int(packet["recommendation_policy"]["target_recommendations"])
    original_quota = target_counts(target, packet)

    def route(item: dict[str, Any]) -> str:
        resolved = resolve_candidate_route(item, packet)
        return str(resolved.get("candidate_type") or item.get("candidate_type") or "")

    available = Counter(route(item) for item in candidates)
    requested_total = {kind: count * group_count for kind, count in original_quota.items()}
    shortfall = {
        kind: requested - available[kind]
        for kind, requested in requested_total.items()
        if available[kind] < requested
    }
    if not shortfall:
        return None

    # 每组都必须能拿到同样数量的该路线；余量不足三组时不把它硬塞进
    # 某一组，避免三组之间出现不可解释的关系路线倾斜。
    capacities = {
        kind: available[kind] // group_count
        for kind in original_quota
    }
    effective = {
        kind: min(original_quota[kind], capacities.get(kind, 0))
        for kind in original_quota
    }
    remaining = target - sum(effective.values())

    # 缺口优先回填到原配置权重更高的路线；用“权重 / 下一槽位”
    # 的确定性评分近似最大余数法，同时尊重每条路线的真实容量。
    weights = dict(recall_mix_ratios(packet))
    order = [kind for kind, _ratio in recall_mix_ratios(packet)]
    for kind in original_quota:
        if kind not in order:
            order.append(kind)
        weights.setdefault(kind, 0.0)
    while remaining > 0:
        eligible = [kind for kind in order if effective.get(kind, 0) < capacities.get(kind, 0)]
        if not eligible:
            # 候选总数可能足够，但某些路线各自不足三首，无法构造三组
            # 相同配额。交给调用方切换到非严格顺序选择，仍只使用真实候选。
            packet["route_reallocation"] = {
                "status": "unbalanced_available_routes",
                "original_quota_per_group": dict(original_quota),
                "available_route_count": dict(available),
                "route_shortfall": dict(shortfall),
                "effective_quota_per_group": None,
                "source_backed_only": True,
            }
            packet["strict_recall_mix"] = False
            return packet["route_reallocation"]
        kind = max(
            eligible,
            key=lambda value: (
                weights.get(value, 0.0) / (effective.get(value, 0) + 1),
                weights.get(value, 0.0),
                -order.index(value),
            ),
        )
        effective[kind] = effective.get(kind, 0) + 1
        remaining -= 1

    original_mix = [
        {"candidate_type": kind, "target_ratio": round(ratio, 6)}
        for kind, ratio in recall_mix_ratios(packet)
    ]
    packet["recommendation_policy"]["recall_mix"] = [
        {"candidate_type": kind, "target_ratio": round(effective[kind] / target, 6)}
        for kind in order if effective.get(kind, 0) > 0
    ]
    report = {
        "status": "reallocated",
        "original_recall_mix": original_mix,
        "original_quota_per_group": dict(original_quota),
        "available_route_count": dict(available),
        "route_shortfall": dict(shortfall),
        "effective_quota_per_group": dict(effective),
        "source_backed_only": True,
        "reason": "公开资料未提供足够的该候选路线，空缺按原配置权重转给可用路线",
    }
    packet["route_reallocation"] = report
    return report


def _candidate_discovery_limits(
    initial_limit: int,
    required: int,
    history: dict[str, Any],
    *,
    hard_limit: int = 1200,
) -> list[int]:
    """计算逐轮候选预算，所有扩容都不得突破绝对硬上限。"""
    if hard_limit < required:
        raise ContractError(f"候选硬上限 {hard_limit} 小于工作流最低需求 {required}")
    history_ids = {str(value) for value in history.get("canonical_track_ids", set()) if value}
    history_keys = {str(value) for value in history.get("track_keys", set()) if value}
    history_size = len(history_ids | history_keys)
    baseline = required + history_size
    initial = min(hard_limit, initial_limit)
    limits = [initial]
    if initial < min(200, hard_limit):
        limits.append(min(200, hard_limit))
    # 缓存排除较多时，单次 200 首召回可能在排除后只剩个位数。
    # 逐级扩大召回上限；上限只约束本次研究池，不影响原歌单和缓存的硬排除。
    if history_size > 150 and limits[-1] == 200:
        expanded = min(hard_limit, 360, max(240, baseline + 120))
        if expanded > limits[-1]:
            limits.append(expanded)
    if history_size > 300 or baseline > 360:
        for target in (480, 720, 960, 1200):
            expanded = min(hard_limit, max(target, baseline + 180))
            if expanded > limits[-1]:
                limits.append(expanded)
            if limits[-1] >= hard_limit:
                break
    return limits


def _candidate_discovery_profiles(
    initial_limit: int,
    required: int,
    history: dict[str, Any],
    *,
    hard_limit: int = 1200,
) -> list[dict[str, int]]:
    limits = _candidate_discovery_limits(initial_limit, required, history, hard_limit=hard_limit)
    profiles: list[dict[str, int]] = []
    for index, limit in enumerate(limits):
        if index == 0:
            profile = {"max_candidates": limit, "similar_limit": 4, "top_track_limit": 3,
                       "relation_project_limit": 4, "relation_top_track_limit": 2}
        elif index == 1:
            profile = {"max_candidates": limit, "similar_limit": 8, "top_track_limit": 6,
                       "relation_project_limit": 8, "relation_top_track_limit": 4}
        elif index == 2:
            profile = {"max_candidates": limit, "similar_limit": 12, "top_track_limit": 8,
                       "relation_project_limit": 12, "relation_top_track_limit": 5}
        else:
            depth = min(24, 12 + (index - 2) * 4)
            tracks = min(20, 8 + (index - 2) * 4)
            profile = {"max_candidates": limit, "similar_limit": depth, "top_track_limit": tracks,
                       "relation_project_limit": depth, "relation_top_track_limit": min(10, 5 + index)}
        profiles.append(profile)
    return profiles


def _reuse_candidate_pool(source_runtime: Path | None, packet: dict[str, Any]) -> list[dict[str, Any]] | None:
    """候选池断点复用：分析包未变时直接沿用上次召回的候选。

    候选召回含平台搜索与逐首风格标签补全，实测 15–50 秒；同一分析包重复运行时
    没有必要再召回一次。analysis_id 变化即视为失效，绝不误用旧候选。
    """
    if source_runtime is None:
        return None
    path = Path(source_runtime) / "candidate_pool.json"
    if not path.is_file():
        return None
    try:
        saved = read_json(path)
    except Exception:
        return None
    if not isinstance(saved, dict) or saved.get("analysis_id") != packet.get("analysis_id"):
        return None
    pool = saved.get("candidate_pool")
    return pool if isinstance(pool, list) and pool else None


def _save_candidate_pool(runtime_dir: Path, packet: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    """把召回结果落盘，供同分析包的后续运行复用（断点续跑）。"""
    try:
        write_json(Path(runtime_dir) / "candidate_pool.json", {
            "schema_version": "1.0",
            "analysis_id": packet.get("analysis_id"),
            "saved_at": utc_now(),
            "candidate_count": len(candidates),
            "candidate_pool": candidates,
        })
    except Exception as error:  # 落盘失败不影响主流程
        print(f'[resume] 候选池落盘失败：{error}', file=sys.stderr, flush=True)


def _configured_candidate_limits(args: argparse.Namespace, required: int) -> tuple[int, int]:
    initial = int(getattr(args, "initial_candidate_limit", getattr(args, "max_candidates", 80)))
    hard = int(getattr(args, "hard_candidate_limit", 1200))
    if initial <= 0 or hard <= 0 or initial > hard:
        raise ContractError("initial-candidate-limit 必须为正数且不得超过 hard-candidate-limit")
    if hard < required:
        raise ContractError(f"候选硬上限 {hard} 小于工作流最低需求 {required}")
    return initial, hard


def _discover_unique_candidates(
    packet: dict[str, Any],
    history: dict[str, Any],
    initial_limit: int,
    required: int,
    on_expand=None,
    *,
    hard_limit: int = 1200,
    max_rounds: int = 3,
    concurrency: int = 8,
    defer_relation: bool = False,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """多轮召回、核验并应用一周排除，直到满足三组 Atlas 的每一路配比。"""

    from lastfm_pipeline import LastFM, discover
    from platform_discovery import discover_platform_candidates
    from candidate_routes import resolve_candidate_route
    from collections import Counter
    from contracts import target_counts

    attempts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    discovery_report: dict[str, Any] = {}
    platform_candidates: list[dict[str, Any]] = []
    platform_report: dict[str, Any] = {}
    quota = (target_counts(int(packet["recommendation_policy"]["target_recommendations"]), packet)
             if isinstance(packet.get("recommendation_policy"), dict) else {})
    if defer_relation:
        quota.pop("musician_relation", None)
    needed = {kind: count * ATLAS_GROUP_COUNT for kind, count in quota.items()}
    shortage: dict[str, int] = {}
    profiles = _candidate_discovery_profiles(
        initial_limit, required, history, hard_limit=hard_limit,
    )[:max_rounds]
    client_seconds = DISCOVERY_TIME_BUDGET_SECONDS
    if deadline is not None:
        client_seconds = max(1, min(client_seconds, int(deadline - time.monotonic())))
    client = LastFM(ROOT / "runtime" / "lastfm-cache", seconds=client_seconds)
    for attempt_index, options in enumerate(profiles, 1):
        if deadline is not None:
            _workflow_remaining(deadline, "候选召回", reserve=2)
        if attempt_index == 1:
            # 艺人延伸：对歌单主要艺人做一次平台搜索（每艺人一次请求），与相似艺人结果合并。
            platform_candidates, platform_report = discover_platform_candidates(
                packet,
                max_candidates=options["max_candidates"],
                concurrency=concurrency,
                tags_client=client,
            )
        discovered, discovery_report = discover(
            packet,
            client,
            **options,
            excluded_track_keys=history.get("track_keys", set()),
            excluded_canonical_track_ids=history.get("canonical_track_ids", set()),
            concurrency=concurrency,
        )
        merged = _merge_candidates(discovered, platform_candidates)
        eligible = _exclude_recent_recommendations(merged, history)
        candidates = eligible[:hard_limit]
        available = Counter(str(resolve_candidate_route(item, packet).get("candidate_type") or item.get("candidate_type"))
                            for item in candidates)
        shortage = {kind: count - available[kind] for kind, count in needed.items()
                    if available[kind] < count}
        attempts.append({
            "attempt": attempt_index,
            **options,
            "discovered_candidate_count": len(merged),
            "platform_candidate_count": len(platform_candidates),
            "artist_continuation_count": sum(1 for item in candidates if item.get("candidate_type") == "artist_continuation"),
            "discovered_similarity_count": len(discovered),
            "history_excluded_count": int(discovery_report.get("history_excluded_count", 0) or 0) + len(merged) - len(eligible),
            "hard_limit_trimmed_count": max(0, len(eligible) - len(candidates)),
            "eligible_candidate_count": len(candidates),
            "route_shortfall": dict(shortage),
            "playlist_excluded_count": int(discovery_report.get("playlist_excluded_count", 0) or 0) + int(platform_report.get("playlist_excluded_count", 0) or 0),
            "verification_rejected_count": int(discovery_report.get("verification_rejected_count", 0) or 0),
        })
        if len(candidates) >= required and not shortage:
            break
        if attempt_index < len(profiles) and on_expand:
            on_expand(options["max_candidates"], profiles[attempt_index]["max_candidates"], len(candidates))
    return candidates, {
        **discovery_report,
        **attempts[-1],
        "platform_discovery": {
            **platform_report,
            "candidate_count": len(platform_candidates),
            "failed_queries": platform_report.get("failed_queries", []),
        },
        "history_window_days": RECOMMENDATION_HISTORY_DAYS,
        "discovery_attempts": attempts,
        "pool_expanded": len(attempts) > 1,
        "required_candidate_count": required,
        "final_shortfall": max(0, required - len(candidates)),
        "route_shortfall": shortage,
    }


def _candidate_prefetch_signature(packet: dict[str, Any]) -> str:
    """Only reuse parallel discovery if all its selection anchors stayed unchanged."""
    # Relationship research may canonicalize only the artist's display casing.
    # Be deliberately stricter than the general artist-key normalizer: a
    # punctuation or spacing change may denote a different public URL/identity.
    distribution = [
        {**item, "artist": str(item.get("artist") or "").casefold()}
        for item in packet.get("primary_distribution") or []
    ]
    return stable_hash({
        "snapshot": packet.get("source_snapshot_id"),
        "analysis_mode": packet.get("analysis_mode"),
        "distribution": distribution,
        "playlist_exclusion": packet.get("playlist_exclusion"),
        "style_definitions": (packet.get("style_analysis") or {}).get("style_definitions"),
    })


def _summary_prefetch_packet(
    snapshot: dict[str, Any], full_playlist_tracks: list[dict[str, Any]],
    taxonomy_path: Path, policy_path: Path | None,
) -> dict[str, Any]:
    """A snapshot-bound discovery input, never a publishable analysis packet.

    Artist IDs and order mirror taste_summary.map_taste_to_packet. No style,
    relationship or track fact is invented: discovery still verifies public
    platform identity and Last.fm evidence before producing a candidate.
    """
    from collections import Counter
    from contracts import DEFAULT_RECALL_MIX, artist_key
    from analysis_agent import load_style_taxonomy
    from musician_analyzer import load_recommendation_policy

    tracks = snapshot["tracks"]
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for track in tracks:
        key = artist_key(track["artist"])
        counts[key] += 1
        display.setdefault(key, track["artist"])
    distribution = [
        {"rank": rank, "artist": display[key], "entity_ref": f"artist:{key}",
         "count": count, "share": round(count / len(tracks), 6)}
        for rank, (key, count) in enumerate(
            sorted(counts.items(), key=lambda item: (-item[1], item[0])), 1)
    ]
    exclusion = {
        "source_track_count": len(full_playlist_tracks),
        "track_keys": sorted({track_key(item.get("title"), item.get("artist"))
                              for item in full_playlist_tracks if item.get("title") and item.get("artist")}),
        "platform_track_ids": sorted({str(item.get("platform_track_id") or "").strip()
                                    for item in full_playlist_tracks
                                    if str(item.get("platform_track_id") or "").strip()}),
    }
    taxonomy = load_style_taxonomy(taxonomy_path)
    policy = load_recommendation_policy(policy_path)
    retained = [(kind, ratio) for kind, ratio in DEFAULT_RECALL_MIX if kind != "musician_relation"]
    ratio_sum = sum(ratio for _, ratio in retained)
    policy["recall_mix"] = [
        {"candidate_type": kind, "target_ratio": round(ratio / ratio_sum, 6)}
        for kind, ratio in retained
    ]
    return {
        "analysis_id": f"prefetch-{snapshot['snapshot_id']}",
        "analysis_mode": resolve_analysis_mode(len(full_playlist_tracks)),
        "source_playlist_track_count": len(full_playlist_tracks),
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_track_count": len(tracks),
        "primary_distribution": distribution,
        "entities": [{"entity_ref": item["entity_ref"], "name": item["artist"],
                      "aliases": [], "related_projects": []} for item in distribution],
        "favorite_track_keys": [track_key(item["title"], item["artist"]) for item in tracks],
        "favorite_tracks": [
            {"track_key": track_key(item["title"], item["artist"]),
             "title": item["title"], "artist": item["artist"], "album": item.get("album", "")}
            for item in tracks
        ],
        "playlist_exclusion": exclusion,
        "recommendation_policy": policy,
        "style_analysis": {"style_definitions": list(taxonomy["styles"].values()),
                           "active_style_refs": []},
    }


def _apply_summary_islands(packet: dict[str, Any], bundle_path: Path) -> None:
    """摘要模式：把摘要里的 islands（按歌手）转成 agent_islands（带 record_ids）。

    大歌单不再单独调用一次分析 Skill；整体总结与三个兴趣岛由摘要一次给出。
    """
    try:
        bundle = read_json(bundle_path)
    except Exception:
        return
    islands = bundle.get("islands") if isinstance(bundle, dict) else None
    if not isinstance(islands, list) or not islands:
        return
    tracks = packet.get("favorite_tracks") or []
    # 与逐曲模式共用同一套命名规则：命中流派词时换成抽象意象名，
    # 避免整个分析包因为一个岛名被拒绝而重跑。
    from agent_lastfm import ABSTRACT_ISLAND_NAMES, _island_name_has_genre
    agent_islands = []
    for index, island in enumerate(islands, 1):
        artists = island.get("artists") if isinstance(island, dict) else None
        markers = {normalized_name(name) for name in (artists or []) if isinstance(name, str)}
        record_ids = [track_index for track_index, track in enumerate(tracks)
                      if normalized_name(track.get("artist") or "") in markers]
        island_name = str(island.get("name") or f"岛 {index}")
        if _island_name_has_genre(island_name):
            island_name = ABSTRACT_ISLAND_NAMES[(index - 1) % len(ABSTRACT_ISLAND_NAMES)]
        agent_islands.append({
            "id": f"agent-island-{index}",
            "name": island_name,
            "summary": str(island.get("summary") or ""),
            "record_ids": record_ids,
        })
    # 摘要只列代表歌手，未覆盖的曲目会导致候选拿不到兴趣岛归属（选曲搜索因此无解）。
    # 按轮转把剩余曲目补进三个岛，保证 100% 覆盖。
    assigned = {rid for island in agent_islands for rid in island["record_ids"]}
    remaining = [index for index in range(len(tracks)) if index not in assigned]
    for offset, track_index in enumerate(remaining):
        agent_islands[offset % len(agent_islands)]["record_ids"].append(track_index)
    # min_interest_groups 契约要求至少为 1：靠上面的 100% 覆盖来满足约束。
    packet["agent_islands"] = agent_islands
    packet["agent_copy_version"] = 1
    summary = bundle.get("overall_summary")
    if isinstance(summary, str) and summary.strip():
        packet["overall_summary"] = summary.strip()


def _build_atlas_groups(candidates: list[dict[str, Any]], packet: dict[str, Any]) -> list[dict[str, Any]]:
    target = int(packet["recommendation_policy"]["target_recommendations"])
    required = max(MIN_RECOMMENDATION_CANDIDATES, target * ATLAS_GROUP_COUNT)
    if len(candidates) < required:
        raise ContractError(f"可用候选不足：三组 Atlas 至少需要 {required} 首，当前只有 {len(candidates)} 首")
    excluded_ids: set[str] = set()
    excluded_keys: set[str] = set()
    groups: list[dict[str, Any]] = []
    for index in range(ATLAS_GROUP_COUNT):
        # 候选类型以程序路由为准：相似艺人召回也可能命中当前歌单艺人，
        # 声明类型与实际路径不一致时统一改正，而不是让整批挑选失败。
        from candidate_routes import resolve_candidate_route
        for item in candidates:
            resolved = resolve_candidate_route(item, packet).get("candidate_type")
            if not resolved or resolved == item.get("candidate_type"):
                continue
            # 关系类型必须自带公开关系来源：路由把普通候选判成关系时不能直接改，
            # 否则会制造出“没有关系证据的关系候选”，被契约拒绝。
            if resolved == "musician_relation" and not (item.get("provider_relation") or {}).get("url"):
                continue
            item["candidate_type"] = resolved
        if index == 0 and packet.get("strict_recall_mix") is True:
            from collections import Counter
            from contracts import target_counts
            labels = {"style_neighbor": "风格邻近", "artist_continuation": "艺人延伸",
                      "musician_relation": "音乐人关系", "exploration": "探索推荐"}
            per_group = target_counts(target, packet)
            available = Counter(str(item.get("candidate_type")) for item in candidates)
            missing = [f"{labels.get(kind, kind)} {available[kind]}/{need * ATLAS_GROUP_COUNT}"
                       for kind, need in per_group.items()
                       if available[kind] < need * ATLAS_GROUP_COUNT]
            if missing:
                raise ContractError("三组 Atlas 严格配比候选不足：" + "；".join(missing) + "；未发布")
        # v2 选曲引擎不读 selection_exclusion（旧引擎才会读），
        # 因此这里必须自己排除前面几组已经用过的曲目，否则三组会大量重复。
        # 归属字段只在走 v2 时剥离（v2 自己按兴趣岛计算）；旧引擎校验依赖它。
        strip_ownership = packet.get("selection_mode") != "lastfm_constraints_v1"
        pool_for_selection = [
            {key: value for key, value in item.items()
             if not (strip_ownership and key == "matched_interest_id")}
            for item in candidates
            if str(item.get("canonical_track_id") or "") not in excluded_ids
            and track_key(item.get("title"), item.get("artist")) not in excluded_keys
        ]
        # 旧选曲引擎要求每个候选都有兴趣岛归属与理由（程序补齐的候选可能缺）：
        # 这里按轮转补上归属，并给空理由一个中性说明，避免整批复核失败。
        if not strip_ownership:
            island_ids = [group.get("id") for group in (packet.get("agent_islands") or [])]
            for offset, item in enumerate(pool_for_selection):
                if island_ids and item.get("matched_interest_id") not in island_ids:
                    item["matched_interest_id"] = island_ids[offset % len(island_ids)]
                if not isinstance(item.get("agent_reason"), str) or not item["agent_reason"].strip():
                    item["agent_reason"] = "该候选由程序按类型配额补入，用于本批推荐的类型均衡。"
        raw_bundle = {
            "schema_version": "2.0", "bundle_type": "recommendation_bundle",
            "bundle_stage": "candidate_pool", "status": "ready",
            "analysis_id": packet["analysis_id"], "generated_at": utc_now(),
            "candidate_pool": pool_for_selection, "recommendations": [],
            "selection_exclusion": {
                "canonical_track_ids": sorted(excluded_ids),
                "track_keys": sorted(excluded_keys),
            },
        }
        ranked = rank_bundle(raw_bundle, packet)
        recommendations = ranked.get("recommendations", [])
        if len(recommendations) != target:
            raise ContractError(f"第 {index + 1} 组 Atlas 只能生成 {len(recommendations)}/{target} 首，无法保证三组质量")
        if packet.get("strict_recall_mix") is True:
            from collections import Counter
            from contracts import target_counts
            expected = target_counts(target, packet)
            actual = Counter(str(item.get("candidate_type")) for item in recommendations)
            if actual != Counter(expected):
                raise ContractError(f"第 {index + 1} 组 Atlas 配比不符：实际 {dict(actual)}，要求 {expected}；未发布")
        ranked["atlas_group_index"] = index
        ranked["atlas_group_count"] = ATLAS_GROUP_COUNT
        groups.append(ranked)
        excluded_ids.update(str(item["canonical_track_id"]) for item in recommendations)
        excluded_keys.update(track_key(item["title"], item["artist"]) for item in recommendations)
    return groups


def _pool_hash(candidates: list[dict[str, Any]]) -> str:
    """候选池指纹：只有池子完全一致时，上一轮的编排结果才可以复用。"""
    return stable_hash(sorted(str(item.get("canonical_track_id") or "") for item in candidates))


def _save_curation_checkpoint(reuse_dir: Path, packet: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    """保存已通过复核的候选编排结果，供同一任务失败重试时复用（断点续跑）。"""
    try:
        write_json(Path(reuse_dir) / "curation_checkpoint.json", {
            "schema_version": "1.0",
            "analysis_id": packet.get("analysis_id"),
            "pool_hash": _pool_hash(candidates),
            "saved_at": utc_now(),
            "candidates": [
                {"id": item.get("canonical_track_id"), "reason": item.get("agent_reason"),
                 "details": item.get("agent_details"), "island": item.get("matched_interest_id")}
                for item in candidates
            ],
        })
    except Exception as error:
        print(f'[resume] 编排结果落盘失败：{error}', file=sys.stderr, flush=True)


def _load_curation_checkpoint(reuse_dir: Path | None, packet: dict[str, Any],
                              candidates: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """读取并校验编排断点：分析包与候选池任一变化即失效。"""
    if reuse_dir is None:
        return None
    path = Path(reuse_dir) / "curation_checkpoint.json"
    if not path.is_file():
        return None
    try:
        saved = read_json(path)
    except Exception:
        return None
    if not isinstance(saved, dict) or saved.get("analysis_id") != packet.get("analysis_id"):
        return None
    if saved.get("pool_hash") != _pool_hash(candidates):
        return None
    rows = saved.get("candidates")
    if not isinstance(rows, list) or not rows:
        return None
    by_id = {str(item.get("canonical_track_id")): item for item in candidates}
    restored: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        base = by_id.get(str(row.get("id")))
        if base is None:
            return None          # 池子里找不到这条：视为失效
        restored.append({**base, "agent_reason": row.get("reason"),
                         "agent_details": row.get("details"),
                         "matched_interest_id": row.get("island")})
    return restored or None


def _program_curate_candidates(packet: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach source-bounded copy without allowing text generation to choose songs."""
    from agent_lastfm import _program_copy
    from candidate_routes import resolve_candidate_route

    islands = packet.get("agent_islands") or []
    if len(islands) != 3:
        raise ContractError("三组 Atlas 缺少经校验的兴趣岛")
    source_tracks = packet.get("favorite_tracks") or []
    owner_by_ref: dict[str, str] = {}
    for island in islands:
        island_id = island["id"]
        for index in island.get("record_ids") or []:
            if type(index) is int and 0 <= index < len(source_tracks):
                artist = normalized_name(source_tracks[index].get("artist"))
                if artist:
                    owner_by_ref.setdefault(artist, island_id)
    ids = [island["id"] for island in islands]
    prepared = []
    for item in candidates:
        candidate = dict(item)
        route = resolve_candidate_route(candidate, packet).get("candidate_type")
        if route and (route != "musician_relation" or (candidate.get("provider_relation") or {}).get("url")):
            candidate["candidate_type"] = route
        relation = candidate.get("provider_relation") or {}
        similarity = candidate.get("provider_similarity") or {}
        anchor = normalized_name(relation.get("seed") or similarity.get("seed") or candidate.get("artist"))
        island_id = owner_by_ref.get(anchor)
        if island_id is None:
            # Stable for retries and independent of candidate-pool ordering.
            marker = str(candidate.get("canonical_track_id") or "")
            island_id = ids[int(hashlib.sha256(marker.encode("utf-8")).hexdigest(), 16) % len(ids)]
        prepared.append({**_program_copy(candidate, island_id), "matched_interest_id": island_id})
    return prepared


def _validate_selection_hard_constraints(
    packet: dict[str, Any],
    groups: list[dict[str, Any]],
) -> None:
    """Validate the selection contract inline, without a second review stage.

    Candidate discovery already performs provider lookups.  This final gate only
    checks the facts needed to publish the locked selection: identity/source
    consistency, playlist/history exclusion, cross-group uniqueness and exact
    route quotas.  Editorial style metadata is intentionally not a hard gate;
    missing details remain visible in the web payload.
    """
    from collections import Counter
    from candidate_routes import resolve_candidate_route
    from contracts import target_counts

    if not isinstance(groups, list) or len(groups) != ATLAS_GROUP_COUNT:
        raise ContractError("Atlas 必须包含三组正式结果")
    target = int(packet["recommendation_policy"]["target_recommendations"])
    expected = target_counts(target, packet) if packet.get("strict_recall_mix") is True else None
    exclusion = packet.get("playlist_exclusion") if isinstance(packet.get("playlist_exclusion"), dict) else {}
    excluded_keys = {str(value) for value in packet.get("favorite_track_keys") or []}
    excluded_keys.update(str(value) for value in exclusion.get("track_keys") or [])
    excluded_ids = {str(value).casefold() for value in exclusion.get("platform_track_ids") or []}
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    route_counts: list[Counter[str]] = []
    for index, group in enumerate(groups, 1):
        recommendations = group.get("recommendations") if isinstance(group, dict) else None
        if not isinstance(recommendations, list) or len(recommendations) != target:
            raise ContractError(f"第 {index} 组 Atlas 必须包含 {target} 首")
        counts: Counter[str] = Counter()
        for item in recommendations:
            if not isinstance(item, dict):
                raise ContractError(f"第 {index} 组包含无效曲目记录")
            canonical = str(item.get("canonical_track_id") or "").strip()
            title = str(item.get("title") or "").strip()
            artist = str(item.get("artist") or "").strip()
            platform_id = str(item.get("platform_track_id") or "").strip()
            if not canonical or not title or not artist or not platform_id:
                raise ContractError(f"第 {index} 组存在缺少真实身份字段的曲目")
            key = track_key(title, artist)
            if canonical in seen_ids or key in seen_keys:
                raise ContractError("三组 Atlas 存在重复曲目或重复身份")
            if key in excluded_keys or platform_id.casefold() in excluded_ids:
                raise ContractError("推荐结果包含原歌单曲目，拒绝发布")
            fact = item.get("metadata_verified")
            if not isinstance(fact, dict) or str(fact.get("url") or "").strip() == "":
                raise ContractError(f"{title} - {artist} 缺少平台身份来源")
            for field in ("title", "artist", "platform_track_id"):
                if str(fact.get(field) or "").strip() != str(item.get(field) or "").strip():
                    raise ContractError(f"{title} - {artist} 平台身份与来源记录不一致")
            source = str(fact.get("source") or "").strip()
            if canonical != f"platform:{source}:{platform_id}":
                raise ContractError(f"{title} - {artist} 的平台身份 ID 与来源记录不一致")
            source_url = str(fact.get("url") or "").strip()
            from urllib.parse import urlsplit
            source_host = urlsplit(source_url).hostname
            allowed_hosts = {"netease": {"music.163.com"}, "itunes": {"music.apple.com"},
                             "qq": {"y.qq.com", "music.qq.com"}}
            if urlsplit(source_url).scheme != "https" or source_host not in allowed_hosts.get(source, set()):
                raise ContractError(f"{title} - {artist} 缺少可信平台歌曲链接")
            projected = {str(value) for value in item.get("sources") or []}
            projected.update(str(value) for value in (item.get("platform_links") or {}).values())
            if source_url not in projected:
                raise ContractError(f"{title} - {artist} 的平台来源未投影到正式结果")
            route = str(resolve_candidate_route(item, packet).get("candidate_type") or item.get("candidate_type") or "")
            if not route:
                raise ContractError(f"{title} - {artist} 缺少候选路线")
            if route == "musician_relation":
                relation = item.get("provider_relation") or {}
                if not str(relation.get("url") or "").strip() or not str(relation.get("person") or "").strip():
                    raise ContractError(f"{title} - {artist} 缺少音乐人关系来源")
            counts[route] += 1
            seen_ids.add(canonical)
            seen_keys.add(key)
        route_counts.append(counts)
    if expected is not None:
        for index, counts in enumerate(route_counts, 1):
            if counts != Counter(expected):
                raise ContractError(f"第 {index} 组 Atlas 配比不符：实际 {dict(counts)}，要求 {expected}")


def _select_review_groups_fast(
    packet: dict[str, Any], candidates: list[dict[str, Any]], *, required: int, stage: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Lock all three groups using verified inputs; editorial detail is not a selection gate."""
    if len(candidates) < required:
        raise ContractError(f"三组 Atlas 候选不足：{len(candidates)}/{required}")
    route_reallocation = _reallocate_route_mix_for_available_evidence(packet, candidates)
    if packet.get("strict_recall_mix") is True:
        from collections import Counter
        from contracts import target_counts
        from candidate_routes import resolve_candidate_route
        quota = target_counts(int(packet["recommendation_policy"]["target_recommendations"]), packet)
        available = Counter(str(resolve_candidate_route(item, packet).get("candidate_type") or item.get("candidate_type"))
                            for item in candidates)
        labels = {"style_neighbor": "风格邻近", "artist_continuation": "艺人延伸",
                  "musician_relation": "音乐人关系", "exploration": "探索推荐"}
        missing = [f"{labels.get(kind, kind)} {available[kind]}/{need * ATLAS_GROUP_COUNT}"
                   for kind, need in quota.items() if available[kind] < need * ATLAS_GROUP_COUNT]
        if missing:
            raise ContractError("三组 Atlas 严格配比候选不足：" + "；".join(missing))
    prepared = _program_curate_candidates(packet, candidates)
    groups = _build_atlas_groups(prepared, packet)
    _validate_selection_hard_constraints(packet, groups)
    emit("stage_detail", status="running", stage=stage,
         candidate_count=len(prepared), atlas_group_count=len(groups),
         message="身份、来源、原歌单排除、去重与三组配比已通过，直接生成正式结果")
    return prepared, groups, {
        "status": "not_performed", "selection_validation": "passed",
        "scope": ["identity", "source", "playlist_exclusion", "deduplication", "route_quota"],
        "candidate_count": len(prepared), "atlas_group_count": len(groups),
        "route_reallocation": route_reallocation,
    }


def _write_locked_selection(runtime_dir: Path, snapshot: dict[str, Any], packet: dict[str, Any],
                            groups: list[dict[str, Any]], source_track_count: int | None = None) -> Path:
    """Persist the immutable song/order selection before editorial hydration."""
    locked_groups = []
    for index, group in enumerate(groups, 1):
        locked_groups.append({
            "id": f"atlas-{index}", "label": f"第 {index} 组",
            "recommendations": [
                {key: item.get(key) for key in (
                    "canonical_track_id", "title", "artist", "url", "platform",
                ) if item.get(key) not in (None, "")}
                for item in group.get("recommendations", [])
            ],
        })
    path = Path(runtime_dir) / "web_selection.json"
    write_json(path, {
        "status": "tracks_locked", "snapshot_id": snapshot["snapshot_id"],
        "playlist_name": snapshot["playlist_name"],
        "source_track_count": int(source_track_count or snapshot.get("track_count") or 0),
        "style_analysis": str(packet.get("overall_summary") or "").strip(),
        "atlas_groups": locked_groups, "locked_at": utc_now(),
    })
    return path


def _curate_review_groups(
    packet: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    required: int,
    curate_fn,
    curate_args: tuple,
    hydrate_fn,
    regeneration_event,
    stage: str,
    curate_reuse_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Run Agent curation plus local review, retrying rejected output up to three times."""
    # Agent 生成与元数据补全不属于本地复核预算。每个本地复核函数自行
    # 执行 10 秒硬门槛；这里只累计它们实际报告的耗时。
    review_elapsed_ms = 0.0
    attempts: list[dict[str, Any]] = []
    resume_candidates = _load_curation_checkpoint(curate_reuse_dir, packet, candidates)
    if resume_candidates is not None:
        # 断点续跑：上一轮已通过复核的编排结果直接复用，跳过 Agent 调用。
        candidates = resume_candidates
        emit("task_completed", status="running", stage=stage, task_kind="agent_recommendation_curate",
             task_id="agent-recommendation-curate", task_status="validated",
             candidate_count=len(candidates), message="续跑：沿用上一轮已通过的候选编排")
        attempts.append({"attempt": 0, "resumed": True, "candidate_count": len(candidates)})
    for attempt in range(1, 4):
        if resume_candidates is not None:
            break
        emit("task_started", status="running", stage=stage, task_kind="agent_recommendation_curate",
             task_id="agent-recommendation-curate", task_status="running", attempt=attempt,
             message="智能助手正在编排候选与推荐文案")
        candidates = curate_fn(packet, candidates, *curate_args,
                               lambda n, feedback: regeneration_event(stage, n, feedback))
        emit("task_completed", status="running", stage=stage, task_kind="agent_recommendation_curate",
             task_id="agent-recommendation-curate", task_status="validated", attempt=attempt,
             candidate_count=len(candidates), message="候选编排与推荐文案已生成")
        if len(candidates) < required:
            raise ContractError(
                f"候选校验后仅保留 {len(candidates)} 首，三组 Atlas 至少需要 {required} 首"
            )
        emit("task_started", status="running", stage=stage, task_kind="recommendation_review",
             task_id="recommendation-review", task_status="running", attempt=attempt,
             message="正在本地复核候选事实、来源、数量与去重")
        candidate_review = review_candidates(candidates, packet, stage="candidate_pool")
        review_elapsed_ms += float(candidate_review.get("elapsed_ms", 0.0) or 0.0)
        if candidate_review["status"] == "rejected":
            emit("task_failed", status="running", stage=stage, task_kind="recommendation_review",
                 task_id="recommendation-review", task_status="failed", attempt=attempt,
                 error="候选来源复核未通过", message="候选复核未通过，准备重新生成")
            attempts.append({"attempt": attempt, "candidate_pool": candidate_review})
            if attempt == 3:
                raise ContractError(
                    "候选来源复核连续 3 次拒绝："
                    + "; ".join(sorted({issue for entry in candidate_review["entries"] for issue in entry.get("issues", [])}))
                )
            emit(
                "stage_detail", status="running", stage=stage,
                review_status="rejected", review_attempt=attempt, max_review_attempts=3,
                review_issues=sorted({issue for entry in candidate_review["entries"] for issue in entry.get("issues", [])}),
                message="候选复核未通过，正在重新生成",
            )
            continue

        _t0 = time.monotonic()
        groups = _build_atlas_groups(candidates, packet)
        print(f'[timing] build_atlas_groups={time.monotonic()-_t0:.2f}s', file=sys.stderr, flush=True)
        selected_ids = {
            item["canonical_track_id"]
            for group in groups
            for item in group["recommendations"]
        }
        _t1 = time.monotonic()
        hydrate_fn([item for item in candidates if item["canonical_track_id"] in selected_ids])
        print(f'[timing] hydrate={time.monotonic()-_t1:.2f}s', file=sys.stderr, flush=True)
        _t2 = time.monotonic()
        groups = _build_atlas_groups(candidates, packet)
        print(f'[timing] build_atlas_groups2={time.monotonic()-_t2:.2f}s', file=sys.stderr, flush=True)
        _t3 = time.monotonic()
        groups_review = review_groups(groups, packet, base=candidate_review)
        print(f'[timing] review_groups={time.monotonic()-_t3:.2f}s', file=sys.stderr, flush=True)
        review_elapsed_ms += float(groups_review.get("elapsed_ms", 0.0) or 0.0)
        report_attempt = {
            "attempt": attempt,
            "candidate_pool": candidate_review,
            "atlas_groups": groups_review,
        }
        attempts.append(report_attempt)
        if groups_review["status"] != "rejected":
            emit("task_completed", status="running", stage=stage, task_kind="recommendation_review",
                 task_id="recommendation-review", task_status="validated", attempt=attempt,
                 candidate_count=len(candidates), atlas_group_count=len(groups),
                 reviewed_count=groups_review.get("reviewed_candidate_count", sum(len(group.get("recommendations", [])) for group in groups)),
                 accepted_count=groups_review.get("accepted_count", 0), gap_count=groups_review.get("gap_count", 0),
                 review_status=groups_review["status"],
                 message="本地来源、身份、去重与三组配比复核完成")
            return candidates, groups, {
                "schema_version": "1.0",
                "artifact_type": "music_atlas_review",
                "status": groups_review["status"],
                "attempt_count": attempt,
                "candidate_pool": candidate_review,
                "atlas_groups": groups_review,
                "elapsed_ms": round(review_elapsed_ms, 3),
                "budget_seconds": 10.0,
                "network_requests": 0,
                "agent_calls": 0,
                "attempts": attempts,
            }
        # 把具体问题写进任务日志与事件：区分“跨组重复”与逐条候选问题。
        group_issues = sorted({
            issue
            for report in (groups_review.get("group_reports") or [])
            for entry in (report.get("entries") or [])
            for issue in (entry.get("issues") or [])
        })
        print(f'[review] 分组复核未通过：跨组重复 {groups_review.get("duplicate_across_groups", 0)}；'
              f'逐条问题 {group_issues[:10]}', file=sys.stderr, flush=True)
        emit("task_failed", status="running", stage=stage, task_kind="recommendation_review",
             task_id="recommendation-review", task_status="failed", attempt=attempt,
             review_issues=group_issues[:10],
             error="Atlas 分组复核未通过", message="Atlas 分组复核未通过，准备重新生成")
        if attempt == 3:
            raise ContractError(
                "Atlas 分组复核连续 3 次拒绝："
                + str(groups_review.get("duplicate_across_groups", 0))
                + " 个跨组重复或字段问题"
            )
        emit(
            "stage_detail", status="running", stage=stage,
            review_status="rejected", review_attempt=attempt, max_review_attempts=3,
            review_issues=["atlas_groups"],
            message="Atlas 分组复核未通过，正在重新生成",
        )
    raise ContractError("复核流程未返回结果")


def _save_recommendation_history(path: Path, history: dict[str, Any], groups: list[dict[str, Any]], playlist_identity: dict[str, Any]) -> None:
    selected = [item for group in groups for item in group.get("recommendations", [])]
    entry = {
        "generated_at": utc_now(),
        "canonical_track_ids": sorted({str(item["canonical_track_id"]) for item in selected}),
        "track_keys": sorted({track_key(item["title"], item["artist"]) for item in selected}),
    }
    write_json(path, {
        "schema_version": "1.0",
        "user_id": str(os.environ.get("MUSIC_ATLAS_USER_ID", "") or ""),
        "playlist": playlist_identity,
        "retention_days": RECOMMENDATION_HISTORY_DAYS,
        "entries": [*history.get("entries", []), entry],
    })


def _normalize_track_percentile(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("歌单分位必须是 25%、50% 或 100%")
    normalized = float(value)
    if normalized not in TRACK_PERCENTILE_OPTIONS:
        raise ContractError("歌单分位必须是 25%、50% 或 100%")
    return normalized


def _percentile_track_limit(total: int, percentile: float) -> int:
    import math
    return max(1, math.ceil(total * percentile))


def _apply_track_limit(snapshot: dict[str, Any], limit: int, percentile: float | None = None) -> dict[str, Any]:
    """Keep only the first ``limit`` tracks as a self-consistent snapshot.

    The web panel size control trims the playlist only *after* Step 1 has read
    it, because the operator must not exceed the real track count. The Step 1
    contract still requires ``declared_track_count == track_count ==
    len(tracks)``, so the trimmed result stands on its own: the pre-trim source
    total stays on ``reader.source_track_count`` for traceability and the
    snapshot id is suffixed so trimmed and untrimmed runs never share analysis
    caches.
    """

    percentile = _normalize_track_percentile(percentile)
    tracks = snapshot.get("tracks")
    if not isinstance(tracks, list):
        raise ContractError("快照缺少 tracks 数组，无法应用处理数量")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ContractError("处理数量必须是大于等于 1 的整数")
    reader = snapshot.get("reader")
    if not isinstance(reader, dict):
        reader = {}
        snapshot["reader"] = reader
    total = len(tracks)
    reader["source_track_count"] = total
    if percentile is not None and _percentile_track_limit(total, percentile) != limit:
        expected = _percentile_track_limit(total, percentile)
        raise ContractError(f"{percentile:g} 分位对应的处理数量应为 {expected} 首")
    if limit >= total:
        reader["requested_track_limit"] = limit if percentile is not None else None
        reader["requested_track_percentile"] = percentile
        return snapshot
    trimmed: list[dict[str, Any]] = []
    for position, track in enumerate(tracks[:limit], 1):
        if not isinstance(track, dict):
            raise ContractError(f"第 {position} 首歌曲不是对象")
        trimmed.append({**track, "position": position})
    reader["requested_track_limit"] = limit
    reader["requested_track_percentile"] = percentile
    snapshot["tracks"] = trimmed
    snapshot["track_count"] = limit
    snapshot["declared_track_count"] = limit
    base_id = re.sub(r"-limit\d+$", "", str(snapshot.get("snapshot_id") or ""))
    snapshot["snapshot_id"] = f"{base_id}-limit{limit}"
    return snapshot


def _read_limit_request_details(path: Path, maximum: int) -> dict[str, Any]:
    """Parse and validate one operator-submitted track limit request."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError("数量请求文件不存在") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"数量请求文件无法解析：{exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError("数量请求必须是 JSON 对象")
    value = payload.get("limit")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("处理数量必须是整数")
    if value < 1 or value > maximum:
        raise ContractError(f"处理数量必须在 1 到 {maximum} 之间")
    if "percentile" not in payload:
        raise ContractError("必须选择 25%、50% 或 100% 歌单分位")
    percentile = _normalize_track_percentile(payload.get("percentile"))
    expected = _percentile_track_limit(maximum, percentile)
    if expected != value:
        raise ContractError(f"{percentile:g} 分位对应的处理数量应为 {expected} 首")
    return {"limit": value, "percentile": percentile}


def _read_limit_request(path: Path, maximum: int) -> int:
    """Return the server-calculated count for a valid percentile request."""
    return int(_read_limit_request_details(path, maximum)["limit"])


def _await_track_limit_details(request_path: Path, maximum: int, timeout: int) -> dict[str, Any]:
    """Block until the operator submits a track limit, then return it.

    An invalid request is dropped and reported instead of failing the job, so
    the page can correct the value; the wait ends only on a valid request or on
    timeout. The playlist has already been read at this point, which is what
    lets the upper bound be the real track count.
    """

    deadline = time.monotonic() + timeout
    while True:
        if request_path.is_file():
            try:
                selection = _read_limit_request_details(request_path, maximum)
            except ContractError as exc:
                request_path.unlink(missing_ok=True)
                emit("limit_rejected", status="awaiting_limit", stage="snapshot",
                     track_count=maximum, error=str(exc))
            else:
                return selection
        if time.monotonic() >= deadline:
            raise ContractError(f"等待选择处理数量超时（{timeout} 秒），本次任务未继续")
        time.sleep(LIMIT_WAIT_POLL_SECONDS)


def _await_track_limit(request_path: Path, maximum: int, timeout: int) -> int:
    """Return the server-calculated count after a valid percentile request."""
    return int(_await_track_limit_details(request_path, maximum, timeout)["limit"])


def _apple_playlist_name(url: str) -> str:
    """Read the public page title; absence never blocks playlist import."""
    from html.parser import HTMLParser
    class TitleParser(HTMLParser):
        name = ''
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'meta' and values.get('property') == 'og:title':
                self.name = values.get('content', '')
    try:
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(request, timeout=5) as response:
            markup = response.read(2000000).decode('utf-8', errors='replace')
        parser = TitleParser()
        parser.feed(markup)
        name = re.sub(r'\s*[-–—]\s*Apple Music\s*$', '', parser.name.replace('\u00a0', ' ')).strip()
        if normalized_name(name) in {normalized_name('Apple Music 网页播放器'), normalized_name('Apple Music Web Player')}:
            return ''
        return name
    except (OSError, ValueError):
        return ''


def _apple_export_playlist_name(path: Path) -> str:
    """Return one unambiguous playlist name recorded in the exported CSV."""
    try:
        with Path(path).open('r', encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            field = next((name for name in (reader.fieldnames or [])
                          if normalized_name(name) == normalized_name('Playlist name')), None)
            if not field:
                return ''
            names = {str(row.get(field) or '').strip() for row in reader if str(row.get(field) or '').strip()}
        return next(iter(names)) if len(names) == 1 else ''
    except (OSError, csv.Error, UnicodeError):
        return ''


def _build_source(args: argparse.Namespace, runtime_dir: Path) -> tuple[dict, dict]:
    kind = args.source_kind
    playlist_name = (args.playlist_name or "").strip()
    # 网页续跑会保留同一运行目录，但重启后不保留原始链接参数。对于 Step 2
    # 半成品，从已校验的完整快照恢复，避免把半成品误送到 Step 3，也避免重复抓取。
    saved_snapshot = runtime_dir / "snapshot.json"
    if kind == "local_json" and not args.input and saved_snapshot.is_file():
        snapshot = validate_playlist_snapshot(read_json(saved_snapshot), require_complete=True)
        tracks = snapshot.get("tracks") or []
        source_count = int((snapshot.get("reader") or {}).get("source_track_count") or len(tracks))
        if source_count != len(tracks):
            raise ContractError("旧任务只有部分歌单快照且分析未完成，无法保证排除完整原歌单；请从歌单链接重新开始")
        source_kind = {"netease": "netease_public", "qq_music": "qq_public"}.get(
            snapshot.get("platform"), snapshot.get("platform") or "local_json")
        return snapshot, {"kind": source_kind, "playlist_id": snapshot.get("playlist_id"),
                          "reused_snapshot": True}
    if kind == "apple_music":
        url = _validate_url(kind, args.source_url or "")
        source_path = runtime_dir / "source" / "apple-playlist.csv"
        export_summary = export_apple_playlist_file(url, source_path, args.expected_count)
        exported_count = export_summary.get("tracks")
        if isinstance(exported_count, bool) or not isinstance(exported_count, int) or exported_count <= 0:
            raise ContractError("Apple Music 导出结果没有有效的歌曲数量")
        completeness_status = export_summary.get("completeness_status", "unconfirmed")
        if completeness_status not in {"confirmed", "unconfirmed"}:
            raise ContractError(f"Apple Music 导出完整性状态不可识别：{completeness_status}")
        snapshot = build_snapshot(
            source_path,
            reader_name="csv",
            platform="apple_music",
            playlist_id=args.playlist_id or _source_id(kind, url),
            playlist_name=playlist_name or _apple_export_playlist_name(source_path) or _apple_playlist_name(url) or "Apple Music 公开歌单",
            # 没有额外参数时仍按导出文件建立内部一致的快照；
            # completeness_status 保存在 reader 中，避免把第三方导出的行数
            # 误写成独立确认的歌单总数。
            declared_count=args.expected_count if args.expected_count is not None else exported_count,
        )
        snapshot["reader"]["completeness_status"] = completeness_status
        return snapshot, {"kind": kind, "url": url, "export": export_summary}
    if kind in PUBLIC_HOSTS:
        url = _validate_url(kind, args.source_url or "")
        reader = "netease_public" if kind == "netease_public" else "qq_public"
        platform = "netease" if kind == "netease_public" else "qq_music"
        resolved_url = url
        playlist_id = url
        if kind == "netease_public":
            playlist_id, resolved_url = _resolve_netease_source(url)
        snapshot = build_snapshot(
            runtime_dir / "source" / "remote-placeholder.json",
            reader_name=reader,
            platform=platform,
            playlist_id=playlist_id,
            playlist_name=playlist_name or ("网易云公开歌单" if kind == "netease_public" else "QQ 音乐公开歌单"),
        )
        source_report = {"kind": kind, "url": url}
        if kind == "netease_public":
            source_report.update({"resolved_url": resolved_url, "playlist_id": playlist_id})
        return snapshot, source_report
    if kind not in {"local_json", "csv"}:
        raise ContractError(f"不支持的网页来源：{kind}")
    input_path = Path(args.input or "").resolve()
    if not input_path.is_file():
        raise ContractError(f"找不到本地歌单输入：{input_path}")
    snapshot = build_snapshot(
        input_path,
        reader_name=kind,
        platform=args.platform or "local",
        playlist_id=args.playlist_id or _source_id(kind, str(input_path)),
        playlist_name=playlist_name or input_path.stem,
        declared_count=args.expected_count,
    )
    return snapshot, {"kind": kind, "input": input_path.name}


def _ensure_resume_playlist_exclusion(packet: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Upgrade old Step 2 packets only when the snapshot covers the entire source list."""
    exclusion = packet.get("playlist_exclusion")
    if isinstance(exclusion, dict) and all(
        isinstance(exclusion.get(field), list) for field in ("track_keys", "platform_track_ids")
    ):
        return
    tracks = snapshot.get("tracks") or []
    source_count = int((snapshot.get("reader") or {}).get("source_track_count") or len(tracks))
    if source_count != len(tracks):
        raise ContractError(
            "旧分析包缺少完整歌单排除数据，且快照仅含部分曲目；不能安全续跑，请重新分析完整歌单"
        )
    packet["playlist_exclusion"] = {
        "source_track_count": len(tracks),
        "track_keys": sorted({track_key(item.get("title"), item.get("artist")) for item in tracks
                              if isinstance(item, dict) and item.get("title") and item.get("artist")}),
        "platform_track_ids": sorted({str(item.get("platform_track_id") or "").strip() for item in tracks
                                      if isinstance(item, dict) and str(item.get("platform_track_id") or "").strip()}),
    }


def run_recommendation_only(args: argparse.Namespace) -> int:
    """Reuse a completed Step 2 packet and regenerate only Step 3."""
    runtime_dir = Path(args.runtime_dir).resolve()
    source_runtime = Path(args.source_runtime_dir).resolve()
    if not source_runtime.is_dir():
        raise ContractError(f"找不到可复用的 Step 2 运行目录：{source_runtime}")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # 同一任务失败重试时允许沿用已有阶段产物；只有未知文件才视为目录污染。
    unexpected = [item for item in runtime_dir.iterdir() if not _is_resumable_artifact(item.name)]
    if unexpected:
        raise ContractError(f"网页任务运行目录出现未知文件：{[item.name for item in unexpected][:5]}")
    snapshot_path = source_runtime / "snapshot.json"
    analysis_path = source_runtime / "musician_analysis.json"
    report_path = source_runtime / "web_job_report.json"
    if not snapshot_path.is_file() or not analysis_path.is_file():
        raise ContractError("上一任务缺少可复用的 Step 2 产物，无法生成新 Atlas")
    snapshot = validate_playlist_snapshot(read_json(snapshot_path), require_complete=True)
    packet = read_json(analysis_path)
    if (packet.get("style_analysis") or {}).get("evidence_model") != "sourced_tags_v1":
        raise ContractError("旧版分析使用已移除的评价机制，不能复用；请从原歌单完整重新运行")
    _ensure_resume_playlist_exclusion(packet, snapshot)
    # 召回配比属于推荐策略：复用分析包时也跟随当前策略，否则改了配比必须
    # 重新分析才生效。分析包的其余事实字段保持原样。
    try:
        from contracts import TASTE_MODES
        from musician_analyzer import load_recommendation_policy
        policy_file = getattr(args, "policy_file", None)
        current = (
            load_recommendation_policy(Path(policy_file).resolve())
            if policy_file else deepcopy(packet["recommendation_policy"])
        )
        mix = [(str(item.get("candidate_type")), float(item.get("target_ratio") or 0.0))
               for item in current.get("recall_mix") or []
               if isinstance(item, dict) and item.get("candidate_type")]
        if str(packet.get("analysis_mode") or "") in TASTE_MODES:
            # 摘要模式没有逐曲关系研究，配比中不含音乐人关系。
            mix = [(kind, ratio) for kind, ratio in mix if kind != "musician_relation"]
        total = sum(ratio for _, ratio in mix)
        packet["recommendation_policy"] = current
        if mix and total > 0:
            packet["recommendation_policy"]["recall_mix"] = [
                {"candidate_type": kind, "target_ratio": round(ratio / total, 6)}
                for kind, ratio in mix
            ]
    except Exception as error:
        raise ContractError(f"推荐策略快照无效：{error}") from error
    packet["strict_recall_mix"] = True
    from contracts import validate_analysis_packet
    validate_analysis_packet(packet)
    from contracts import require_analysis_coverage
    require_analysis_coverage(packet)
    # 报告只在完整跑完的任务里存在；断点续跑时由快照重建来源信息。
    report = read_json(report_path) if report_path.is_file() else {}
    source_report = report.get("source") if isinstance(report, dict) else None
    if not isinstance(source_report, dict):
        source_report = {"kind": snapshot.get("platform"), "playlist_id": snapshot.get("playlist_id")}
    write_json(runtime_dir / "snapshot.json", snapshot)
    write_json(runtime_dir / "musician_analysis.json", packet)
    emit("style_analysis_ready", status="running", stage="analysis",
         style_analysis=str(packet.get("overall_summary") or "").strip(),
         analysis_id=packet.get("analysis_id"), message="音乐风格分析已复用，正在确定最终曲目")
    emit("started", status="running", stage="recommendation", reused_analysis_id=packet.get("analysis_id"),
         analysis_parallelism=args.analysis_parallelism, recommendation_parallelism=args.recommendation_parallelism,
         message="新 Atlas：复用当前 Step 2 分析包，重新执行 Step 3")
    emit("task_started", status="running", stage="recommendation", task_kind="platform_discovery",
         task_id="platform-discovery", task_index=1, task_total=1, task_status="running",
         message="正在基于当前兴趣岛重新召回候选")
    history_path = _playlist_history_path(source_report, snapshot, runtime_dir)
    recommendation_history = _recent_recommendation_history(history_path)
    target = int(packet["recommendation_policy"]["target_recommendations"])
    required = max(MIN_RECOMMENDATION_CANDIDATES, target * ATLAS_GROUP_COUNT)
    initial_limit, hard_limit = _configured_candidate_limits(args, required)
    candidates = _reuse_candidate_pool(runtime_dir, packet)
    if candidates is not None:
        candidates = _exclude_recent_recommendations(candidates, recommendation_history)[:hard_limit]
        # 断点续跑：同一任务重试时沿用上一轮召回的候选（省下平台搜索与风格补全）。
        discovery_report = {"provider": "cached_candidate_pool", "candidate_count": len(candidates)}
        emit("task_completed", status="running", stage="recommendation", task_kind="platform_discovery",
             task_id="platform-discovery", task_index=1, task_total=1, task_status="validated",
             completed=1, total=1, candidate_count=len(candidates),
             message=f"续跑：沿用上一轮的 {len(candidates)} 首候选")
    else:
        initial_limit, hard_limit = _configured_candidate_limits(args, required)
        candidates, discovery_report = _discover_unique_candidates(
            packet,
            recommendation_history,
            initial_limit,
            required,
            lambda previous, current, eligible: emit(
                "stage_detail",
                status="running",
                stage="recommendation",
                message=(f"排除原歌单与近一周推荐后暂有 {eligible}/{required} 首，"
                         f"正在扩大候选召回范围 {previous}→{current}"),
            ),
            hard_limit=hard_limit,
            max_rounds=args.max_research_rounds,
            concurrency=args.recommendation_parallelism,
        )
        _save_candidate_pool(runtime_dir, packet, candidates)
    _save_candidate_pool(runtime_dir, packet, candidates)
    discovery_report = {**discovery_report, "regenerated_from": str(source_runtime)}
    write_json(runtime_dir / "platform_discovery.json", discovery_report)
    if len(candidates) < required:
        raise ContractError(
            f"排除原歌单与近一周推荐后仍只有 {len(candidates)} 首可核验候选，"
            f"三组 Atlas 至少需要 {required} 首；"
            f"原歌单排除 {discovery_report.get('playlist_excluded_count', 0)} 首，"
            f"近一周推荐排除 {discovery_report.get('history_excluded_count', 0)} 首，"
            f"事实核验未通过 {discovery_report.get('verification_rejected_count', 0)} 首，"
            f"还缺 {required - len(candidates)} 首")
    emit("task_completed", status="running", stage="recommendation", task_kind="platform_discovery",
         task_id="platform-discovery", task_index=1, task_total=1, task_status="validated", completed=1, total=1,
         candidate_count=len(candidates), message=f"已取得 {len(candidates)} 首排除原歌单与一周缓存后的真实候选")
    emit("stage_detail", status="running", stage="recommendation",
         message=f"正在基于当前兴趣岛生成 {required} 首以上新候选，并校验身份、来源、排除、去重与配比")
    candidates, groups, selection_validation = _select_review_groups_fast(
        packet, candidates, required=required, stage="recommendation",
    )
    if packet.get("route_reallocation"):
        discovery_report["route_reallocation"] = packet["route_reallocation"]
        write_json(runtime_dir / "platform_discovery.json", discovery_report)
        # Export/retry must validate the same effective route quota that was
        # used to lock the groups, rather than the stale configured mix.
        write_json(runtime_dir / "musician_analysis.json", packet)
    for index, group in enumerate(groups, 1):
        write_json(runtime_dir / f"recommendation_bundle_group_{index}.json", group)
    write_json(runtime_dir / "recommendation_bundle.json", groups[0])
    selection_path = _write_locked_selection(runtime_dir, snapshot, packet, groups)
    emit("tracks_locked", status="running", stage="recommendation",
         selection_path=str(selection_path), recommendation_group_count=ATLAS_GROUP_COUNT,
         total_unique_recommendation_count=sum(len(group["recommendations"]) for group in groups),
         message="三组 Atlas 曲目与顺序已确定，正在补充资料细节")
    emit("completed", status="running", stage="recommendation", recommendation_count=target,
         recommendation_group_count=ATLAS_GROUP_COUNT,
         total_unique_recommendation_count=sum(len(group["recommendations"]) for group in groups),
         parallelism=args.recommendation_parallelism)
    payloads = []
    editorial_file = getattr(args, "editorial", None)
    editorial_path = Path(editorial_file).resolve() if editorial_file else None
    for index in range(1, ATLAS_GROUP_COUNT + 1):
        out = runtime_dir / f"web_payload_group_{index}.json"
        export_web_payload(runtime_dir, out, snapshot_path=snapshot_path,
                           analysis_path=runtime_dir / "musician_analysis.json",
                           bundle_path=runtime_dir / f"recommendation_bundle_group_{index}.json",
                           editorial_path=editorial_path,
                           publish_web_result=bool(args.current_data))
        payloads.append(read_json(out))
    combined = dict(payloads[0])
    combined["atlas_groups"] = [{"id": f"atlas-{i+1}", "label": f"第 {i+1} 组",
                                  "recommendations": value["recommendations"], "artists": value["artists"],
                                  "albums": value["albums"], "sources": value["sources"]}
                                 for i, value in enumerate(payloads)]
    combined["atlas_group_count"] = ATLAS_GROUP_COUNT
    combined["atlas_swap_limit"] = ATLAS_GROUP_COUNT - 1
    combined["candidate_count"] = len(candidates)
    combined["recommendation_history_days"] = RECOMMENDATION_HISTORY_DAYS
    payload_path = runtime_dir / "web_payload.json"
    write_json(payload_path, combined)
    _save_recommendation_history(history_path, recommendation_history, groups,
                                 {"kind": source_report.get("kind"), "playlist_id": snapshot.get("playlist_id")})
    if args.current_data:
        _publish_payload(payload_path, Path(args.current_data))
    write_json(runtime_dir / "web_job_report.json", {
        "schema_version": "2.0", "artifact_type": "web_job_report", "status": "completed",
        "completed_at": utc_now(), "source": source_report, "snapshot_id": snapshot.get("snapshot_id"),
        "analysis_id": packet.get("analysis_id"), "reused_analysis_id": packet.get("analysis_id"),
        "source_runtime_dir": str(source_runtime), "recommendation_groups": {"count": ATLAS_GROUP_COUNT,
        "swap_limit": ATLAS_GROUP_COUNT - 1, "total_unique_recommendation_count": sum(len(g["recommendations"]) for g in groups)},
        "platform_discovery": discovery_report, "selection_validation": selection_validation,
        "payload_path": str(payload_path),
        "current_data_path": str(Path(args.current_data).resolve()) if args.current_data else None,
    })
    emit("completed", status="completed", stage="export", payload_path=str(payload_path),
         current_data_path=str(Path(args.current_data).resolve()) if args.current_data else None,
         recommendation_count=target, recommendation_group_count=ATLAS_GROUP_COUNT)
    emit("task_completed", status="completed", stage="export", task_kind="atlas_export", task_id="atlas-export",
         task_index=1, task_total=1, task_status="published" if args.current_data else "validated", completed=1,
         total=1, recommendation_count=target, recommendation_group_count=ATLAS_GROUP_COUNT,
         message="新 Atlas 已更新" if args.current_data else "新 Atlas 验收预览已生成")
    return 0

def run_web_workflow(args: argparse.Namespace) -> int:
    first_run_started = time.monotonic()
    workflow_started = first_run_started
    workflow_budget = int(getattr(args, "workflow_time_budget", DEFAULT_WORKFLOW_TIME_BUDGET_SECONDS))
    if workflow_budget < 1 or workflow_budget > 600:
        raise ContractError("workflow-time-budget 必须是 1 到 600 秒之间的整数")
    workflow_deadline = workflow_started + workflow_budget
    first_run_deadline = workflow_deadline
    runtime_dir = Path(args.runtime_dir).resolve()
    # server.js 会在启动子进程前把任务状态持久化到同一运行目录。
    # 该单一状态文件不属于工作流产物，不能因此把新任务误判为目录污染。
    if runtime_dir.exists():
        # 同一任务失败重试时允许沿用已有阶段产物；只有未知文件才视为目录污染。
        unexpected = [item for item in runtime_dir.iterdir() if not _is_resumable_artifact(item.name)]
        if unexpected:
            raise ContractError(f"网页任务运行目录出现未知文件：{[item.name for item in unexpected][:5]}")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # Step 2/3 已改为平台公开数据的程序化流程。保留旧参数只为兼容已有网页
    # 调用，不再让没有联网能力的模型生成曲目、关系或来源事实。

    emit("started", status="running", stage="snapshot", source_kind=args.source_kind,
         analysis_parallelism=args.analysis_parallelism,
         recommendation_parallelism=args.recommendation_parallelism)
    emit("task_started", status="running", stage="snapshot", task_kind="snapshot_import",
         task_id="snapshot-import", task_index=1, task_total=1, task_status="running",
         message="正在识别平台并拉取歌单")
    snapshot, source_report = _build_source(args, runtime_dir)
    # 处理上限只限制 Step 2 的分析样本，不改变“不要推荐已在原歌单里”的边界。
    # 在截断前保存完整歌单身份，后续随分析包传给候选召回和最终排序。
    full_playlist_tracks = list(snapshot.get("tracks") or [])
    snapshot_path = runtime_dir / "snapshot.json"
    save_snapshot(snapshot, snapshot_path)
    validate_playlist_snapshot(snapshot, require_complete=True)
    # 用户选择分析范围所花的时间不属于机器处理时间；首次抓取耗时仍
    # 单独记录，并计入首次完整运行的端到端验收指标。
    snapshot_elapsed = time.monotonic() - first_run_started
    emit("completed", status="running", stage="snapshot", track_count=snapshot["track_count"],
         snapshot_id=snapshot["snapshot_id"], playlist_name=snapshot["playlist_name"], source=source_report,
         completeness_status=(snapshot.get("reader") or {}).get("completeness_status"),
         snapshot_elapsed_seconds=round(snapshot_elapsed, 3))
    emit("task_completed", status="running", stage="snapshot", task_kind="snapshot_import",
         task_id="snapshot-import", task_index=1, task_total=1, task_status="validated",
         completed=1, total=1, track_completed=snapshot["track_count"],
         track_total=snapshot["track_count"], message=f"歌单已整理 · {snapshot['track_count']} 首")

    # 网页端档位选择：歌单读取完成后暂停，等操作者提交处理数量再截断。
    # 只有读到了曲目才谈得上上限，因此空歌单不走等待，交给既有校验报错。
    source_track_count = snapshot["track_count"]
    await_limit = bool(getattr(args, "await_track_limit", False)) and not source_report.get("reused_snapshot")
    if await_limit and source_track_count > 0:
        limit_timeout = int(getattr(args, "await_limit_timeout", 0) or 0)
        if not 0 < limit_timeout <= AWAIT_LIMIT_MAX_TIMEOUT_SECONDS:
            raise ContractError(
                f"await-limit-timeout 必须是 1 到 {AWAIT_LIMIT_MAX_TIMEOUT_SECONDS} 秒之间的整数")
        request_path = runtime_dir / LIMIT_REQUEST_FILENAME
        emit("awaiting_limit", status="awaiting_limit", stage="snapshot",
             track_count=source_track_count, snapshot_id=snapshot["snapshot_id"],
             timeout_seconds=limit_timeout,
             message=f"歌单已读取 · {source_track_count} 首，等待选择处理数量")
        emit("task_started", status="awaiting_limit", stage="snapshot", task_kind="track_limit",
             task_id="track-limit", task_index=1, task_total=1, task_status="running",
             track_count=source_track_count, message="等待选择处理数量")
        selection = _await_track_limit_details(request_path, source_track_count, limit_timeout)
        request_path.unlink(missing_ok=True)
        workflow_started = time.monotonic()
        workflow_deadline = workflow_started + workflow_budget
        # The human's selection wait is excluded, but the initial crawl still
        # counts toward the first-run end-to-end 120-second acceptance budget.
        first_run_deadline = workflow_deadline - snapshot_elapsed
        limit = int(selection["limit"])
        percentile = selection.get("percentile")
        snapshot = _apply_track_limit(snapshot, limit, percentile=percentile)
        validate_playlist_snapshot(snapshot, require_complete=True)
        save_snapshot(snapshot, snapshot_path)
        emit("limit_applied", status="running", stage="snapshot",
             requested_track_limit=limit, requested_track_percentile=percentile,
             source_track_count=source_track_count,
             track_count=snapshot["track_count"], snapshot_id=snapshot["snapshot_id"],
             message=f"已确定处理数量 · 前 {snapshot['track_count']} 首")
        emit("task_completed", status="running", stage="snapshot", task_kind="track_limit",
             task_id="track-limit", task_index=1, task_total=1, task_status="validated",
             completed=1, total=1, track_count=snapshot["track_count"],
             message=f"处理数量已确定 · 前 {snapshot['track_count']} 首")

    # 规模路由：≤200 逐曲；201–999 汇总曲目及专辑来源；≥1000 只汇总歌手来源。
    track_count = len(snapshot["tracks"])
    # A selected percentile does not change the original playlist's analysis
    # resolution. A 1917-song list trimmed to 959 remains artist-only.
    scale_mode = resolve_analysis_mode(source_track_count)
    summary_mode = scale_mode != "track_research"
    # A large playlist must overlap its 60–80 second discovery with the
    # summary Agent. The snapshot is complete at this point (including the
    # original songs omitted by the selected analysis limit).
    prefetch_history_path = _playlist_history_path(source_report, snapshot, runtime_dir)
    prefetch_history = _recent_recommendation_history(prefetch_history_path)
    prefetch_result: dict[str, Any] = {}
    prefetch_thread: threading.Thread | None = None
    prefetch_signature = ""
    prefetch_initial = prefetch_hard = prefetch_required = 0
    source_prefetch_result: dict[str, Any] = {}
    source_prefetch_thread: threading.Thread | None = None

    def start_candidate_prefetch(input_packet: dict[str, Any]) -> tuple[threading.Thread, str, int, int, int]:
        required = max(MIN_RECOMMENDATION_CANDIDATES,
                       int(input_packet["recommendation_policy"]["target_recommendations"]) * ATLAS_GROUP_COUNT)
        initial, hard = _configured_candidate_limits(args, required)
        captured = deepcopy(input_packet)
        signature = _candidate_prefetch_signature(captured)

        def worker() -> None:
            window_result: dict[str, Any] = {}
            window_thread: threading.Thread | None = None
            window_cutoff = min(workflow_deadline - 12,
                                time.monotonic() + DISCOVERY_TIME_BUDGET_SECONDS)
            if (len(captured.get("primary_distribution") or []) >= 32
                    and int(captured.get("source_track_count") or 0) >= 500):
                def research_second_window() -> None:
                    try:
                        rows, report = _prefetch_large_playlist_window(
                            captured, prefetch_history, hard_limit=hard,
                            concurrency=max(args.recommendation_parallelism, 8),
                            deadline=workflow_deadline,
                        )
                        window_result.update(candidates=rows, report=report)
                    except Exception as error:
                        window_result["error"] = error

                window_thread = threading.Thread(
                    target=research_second_window, name="music-atlas-large-window-prefetch",
                    daemon=True,
                )
                window_thread.start()
            try:
                rows, report = _discover_unique_candidates(
                    captured, prefetch_history, initial, required,
                    hard_limit=hard, max_rounds=args.max_research_rounds,
                    concurrency=max(args.recommendation_parallelism, 8), defer_relation=True,
                    deadline=workflow_deadline,
                )
                prefetch_result.update(candidates=rows, report=report)
                if window_thread is not None:
                    window_thread.join(timeout=max(0, window_cutoff - time.monotonic()))
                    if not window_thread.is_alive() and "candidates" in window_result:
                        prefetch_result.update(window_candidates=window_result["candidates"],
                                               window_report=window_result["report"])
            except Exception as error:
                prefetch_result["error"] = error

        thread = threading.Thread(target=worker, name="music-atlas-candidate-prefetch", daemon=True)
        thread.start()
        return thread, signature, initial, hard, required

    if summary_mode:
        early_packet = _summary_prefetch_packet(
            snapshot, full_playlist_tracks, (ROOT / "styles" / "style_taxonomy.json").resolve(),
            Path(args.policy_file).resolve() if getattr(args, "policy_file", None) else None,
        )
        (prefetch_thread, prefetch_signature, prefetch_initial, prefetch_hard,
         prefetch_required) = start_candidate_prefetch(early_packet)
        # Public music facts are fetched once from the complete snapshot while
        # the summary and candidate discovery run. At 1000+ tracks this worker
        # queries only distinct artists, never individual tracks or albums.
        def prefetch_public_sources() -> None:
            try:
                from lastfm_pipeline import LastFM, collect_artist_tags, collect_tags
                client = LastFM(ROOT / "runtime" / "lastfm-cache", seconds=max(
                    1, min(40, int(workflow_deadline - time.monotonic() - 25))))
                if scale_mode == "artist_summary":
                    sources = collect_artist_tags(
                        early_packet, client, concurrency=max(args.analysis_parallelism, 5),
                        max_artists=96, max_seconds=40,
                    )
                else:
                    sources = collect_tags(
                        early_packet, client, concurrency=max(args.analysis_parallelism, 5),
                    )
                source_prefetch_result["source_tags"] = sources
            except Exception as error:
                source_prefetch_result["error"] = error

        source_prefetch_thread = threading.Thread(
            target=prefetch_public_sources, name="music-atlas-public-source-prefetch", daemon=True)
        source_prefetch_thread.start()
    # 首次运行不再启动独立曲目审计或单列复核；公开平台候选的身份和
    # 来源在召回时核验，最终选择在同一条确定性选曲门槛内收口。

    taxonomy_path = (ROOT / "styles" / "style_taxonomy.json").resolve()
    policy_file = getattr(args, "policy_file", None)
    editorial_file = getattr(args, "editorial", None)
    policy_path = Path(policy_file).resolve() if policy_file else None
    editorial_path = Path(editorial_file).resolve() if editorial_file else None
    analysis_research_dir = runtime_dir / "analysis_research"
    analysis_mode = scale_mode if summary_mode else "lastfm_agent"
    scale_label = {"taste_summary": "品味摘要", "artist_summary": "歌手摘要"}.get(scale_mode, "逐曲分析")
    scale_message = (f"Step 2：歌单 {track_count} 首，采用{scale_label}模式（按公开资料生成整体音乐风格分析）"
                     if summary_mode else
                     f"Step 2：歌单 {track_count} 首，逐曲分析；来源在选曲门槛内即时校验")
    emit("started", status="running", stage="analysis", parallelism=args.analysis_parallelism,
         analysis_mode=analysis_mode, track_count=track_count, summary_mode=summary_mode,
         message=scale_message)
    analysis_path = runtime_dir / "musician_analysis.json"
    analysis_markdown_path = runtime_dir / "musician_analysis.md"
    analysis_manifest_path = runtime_dir / "analysis_manifest.json"
    # 不把历史静态关系或未重新取得的风格档案混入本次事实包。
    empty_relations, empty_profiles = runtime_dir / "public_relations.json", runtime_dir / "public_style_profiles.json"
    write_json(empty_relations, {})
    write_json(empty_profiles, {"artists": {}})
    if summary_mode:
        from musician_analyzer import load_style_taxonomy
        from taste_summary import build_sourced_summary_packet
        # Only identity and programme-owned counts exist until the parallel
        # public-source worker completes. No Agent-written style or URL is
        # accepted as an intermediate fact or a publishable summary.
        packet = build_sourced_summary_packet(
            snapshot, load_style_taxonomy(taxonomy_path), taxonomy_path=taxonomy_path,
            policy_path=policy_path, source_playlist_track_count=source_track_count,
        )
    else:
        emit("task_started", status="running", stage="analysis", task_kind="analysis_aggregate",
             task_id="analysis-aggregate", task_index=2, task_total=2, task_status="running",
             message="正在生成只含本次公开事实的分析包")
        packet = analyze_and_validate(
            snapshot_path, preferred_path=ROOT / "preferred_artists.txt", relation_path=empty_relations,
            output_path=analysis_path, markdown_path=analysis_markdown_path, manifest_path=analysis_manifest_path,
            style_taxonomy_path=taxonomy_path, style_profile_path=empty_profiles, policy_path=policy_path,
            analysis_mode="public_facts_only",
        )
        packet["source_playlist_track_count"] = source_track_count
    playlist_exclusion = {
        "source_track_count": len(full_playlist_tracks),
        "track_keys": sorted({track_key(item.get("title"), item.get("artist")) for item in full_playlist_tracks
                              if isinstance(item, dict) and item.get("title") and item.get("artist")}),
        "platform_track_ids": sorted({str(item.get("platform_track_id") or "").strip() for item in full_playlist_tracks
                                      if isinstance(item, dict) and str(item.get("platform_track_id") or "").strip()}),
    }
    packet["playlist_exclusion"] = playlist_exclusion
    from lastfm_pipeline import LastFM, collect_tags, validate_knowledge
    if summary_mode:
        packet["selection_mode"] = "taste_constraints_v1"
        if source_prefetch_thread is None:
            raise ContractError("公开资料任务未启动，不能发布摘要分析")
        source_prefetch_thread.join(timeout=max(0, _workflow_remaining(
            first_run_deadline, "公开资料归集", reserve=16)))
        if source_prefetch_thread.is_alive():
            raise ContractError("公开资料归集超过首次运行时间预算，未发布")
        if "error" in source_prefetch_result:
            raise ContractError(f"公开资料归集失败：{source_prefetch_result['error']}")
        if "source_tags" not in source_prefetch_result:
            raise ContractError("未取得可核对的公开资料，不能发布摘要分析")
        packet["source_tags"] = source_prefetch_result["source_tags"]
    else:
        packet["selection_mode"] = "lastfm_constraints_v1"
        # Start public candidate discovery before source-track tags, allowing
        # the independent network work to overlap without crossing the Step 2
        # publication/validation barrier.
        if prefetch_thread is None:
            (prefetch_thread, prefetch_signature, prefetch_initial, prefetch_hard,
             prefetch_required) = start_candidate_prefetch(packet)
        packet["source_tags"] = collect_tags(
            packet,
            LastFM(ROOT / "runtime" / "lastfm-cache", seconds=max(1, min(
                30, int(_workflow_remaining(workflow_deadline, "风格标签", reserve=8))))),
            concurrency=max(args.analysis_parallelism, 8),
        )
    validate_knowledge(packet)
    from musician_analyzer import apply_public_style_evidence
    packet = apply_public_style_evidence(packet, packet["source_tags"])
    packet["strict_recall_mix"] = True
    write_json(runtime_dir / "lastfm_analysis.json", packet["source_tags"])
    if summary_mode:
        from taste_summary import attach_sourced_editorial
        packet = attach_sourced_editorial(packet)
        analysis_research_dir.mkdir(parents=True, exist_ok=True)
        write_json(analysis_research_dir / "taste_summary.json", packet["taste_summary"])
    # Track-research mode needs its initial packet first; summary mode started
    # at the snapshot boundary and must not launch the same network work twice.
    if prefetch_thread is None:
        (prefetch_thread, prefetch_signature, prefetch_initial, prefetch_hard,
         prefetch_required) = start_candidate_prefetch(packet)
    from agent_lastfm import analyze, curate
    def regeneration_event(stage, attempt, feedback):
        message = ("Agent 请求超时，正在重新尝试" if "Agent 执行超时" in feedback
                   else "文案校验需调整，正在重新生成")
        emit("stage_detail", status="running", stage=stage, generation_attempt=attempt,
             max_generation_attempts=3, validation_feedback=feedback,
             message=message)
    if summary_mode:
        # Both summary and presentation islands are derived from the fetched
        # source layers. Unverified artists are not silently treated as a style.
        emit("task_completed", status="running", stage="analysis", task_kind="agent_style_analysis",
             task_id="agent-style-analysis", task_status="validated",
             message="已按公开资料归纳风格与资料缺口")
    else:
        emit("task_started", status="running", stage="analysis", task_kind="agent_style_analysis",
             task_id="agent-style-analysis", task_status="running",
             message="智能助手正在总结整体风格并归纳三个兴趣岛")
        analysis_timeout = analysis_style_timeout(
            args.analysis_timeout,
            _workflow_remaining(workflow_deadline, "Step 2 文案分析"),
        )
        analyze(packet, args.analysis_command, analysis_timeout, runtime_dir / "agent",
                lambda attempt,feedback:regeneration_event("analysis",attempt,feedback))
        emit("task_completed", status="running", stage="analysis", task_kind="agent_style_analysis",
             task_id="agent-style-analysis", task_status="validated",
             message="整体风格总结与三大兴趣岛已生成")
    # Agent 只完成风格归纳。关系事实随后由公开目录程序化取得，避免模型
    # 根据记忆补充成员或合作。每个兴趣岛最多选择一个代表艺人，控制请求量。
    from relationship_sources import RelationshipClient, collect_relationships, select_island_seeds
    # 大歌单摘要只做歌手分析与推荐：跳过音乐人关系核验（该类型允许为 0）。
    relation_seeds = [] if summary_mode else select_island_seeds(packet)
    emit("task_started", status="running", stage="analysis", task_kind="relationship_collect",
         task_id="relationship-collect", task_index=3, task_total=3, task_status="running",
         seed_artists=relation_seeds, message="正在核验成员、合作与共享音乐人路径")
    relation_client = RelationshipClient(ROOT / "runtime" / "relationship-cache")
    relation_catalog = collect_relationships(relation_seeds, relation_client)
    write_json(empty_relations, relation_catalog)
    relation_artist_count = len(relation_catalog.get("artists", {}))
    relation_project_count = sum(len(item.get("related_projects", []))
                                 for item in relation_catalog.get("artists", {}).values())
    if relation_artist_count:
        agent_fields = {key: packet[key] for key in (
            "overall_summary", "agent_copy_version", "agent_islands", "source_tags",
            "selection_mode", "strict_recall_mix", "playlist_exclusion",
            "source_playlist_track_count",
        ) if key in packet}
        packet = analyze_and_validate(
            snapshot_path, preferred_path=ROOT / "preferred_artists.txt", relation_path=empty_relations,
            output_path=analysis_path, markdown_path=analysis_markdown_path, manifest_path=analysis_manifest_path,
            style_taxonomy_path=taxonomy_path, style_profile_path=empty_profiles, policy_path=policy_path,
            analysis_mode="public_facts_only",
        )
        packet.update(agent_fields)
        packet = apply_public_style_evidence(packet, packet["source_tags"])
    packet["relationship_research"] = {
        "status": "source_recorded" if relation_artist_count else "unavailable",
        "seed_artists": relation_seeds,
        "resolved_artist_count": relation_artist_count,
        "related_project_count": relation_project_count,
        "unresolved_artists": relation_catalog.get("unresolved_artists", []),
        "request_count": len(relation_catalog.get("requests", [])),
        "catalog_path": empty_relations.name,
    }
    relation_result: dict[str, Any] = {}
    relation_thread: threading.Thread | None = None
    if relation_project_count:
        relation_packet = deepcopy(packet)

        def _prefetch_relations_worker() -> None:
            try:
                from lastfm_pipeline import LastFM, discover
                rows, report = discover(
                    relation_packet, LastFM(ROOT / "runtime" / "lastfm-cache", seconds=30),
                    max_candidates=prefetch_initial, relation_project_limit=8,
                    relation_top_track_limit=8, include_similarity=False,
                    excluded_track_keys=prefetch_history.get("track_keys", set()),
                    excluded_canonical_track_ids=prefetch_history.get("canonical_track_ids", set()),
                    concurrency=max(args.recommendation_parallelism, 8),
                )
                relation_result.update(candidates=rows, report=report)
            except Exception as error:
                relation_result["error"] = error

        relation_thread = threading.Thread(target=_prefetch_relations_worker,
                                           name="music-atlas-relation-prefetch", daemon=True)
        relation_thread.start()
    emit("task_completed", status="running", stage="analysis", task_kind="relationship_collect",
         task_id="relationship-collect", task_index=3, task_total=3, task_status="validated",
         completed=1, total=1, resolved_artist_count=relation_artist_count,
         related_project_count=relation_project_count,
         message=(f"已核验 {relation_artist_count} 位代表艺人 · {relation_project_count} 条共享项目路径"
                  if relation_artist_count else "公开目录未取得无歧义关系，关系字段保持为空"))
    emit("task_started", status="running", stage="analysis", task_kind="analysis_packet_validate",
         task_id="analysis-packet-validate", task_status="running",
         message="正在校验分析包的曲目覆盖、来源边界与兴趣岛结构")
    from contracts import validate_analysis_packet
    validate_analysis_packet(packet)
    from contracts import require_analysis_coverage
    require_analysis_coverage(packet)
    write_json(analysis_path, packet)
    emit("task_completed", status="running", stage="analysis", task_kind="analysis_packet_validate",
         task_id="analysis-packet-validate", task_status="validated",
         message="分析包结构与真实性边界校验通过")
    source_coverage = packet["style_analysis"]["source_coverage"]
    classified_track_count = packet["style_analysis"]["classified_track_count"]
    evidence_covered_count = (source_coverage["weighted_artist_track_count"]
                              if scale_mode == "artist_summary" else
                              source_coverage["track_evidence_count"] + source_coverage["album_background_count"])
    coverage_degraded = evidence_covered_count != packet["source_track_count"]
    coverage_threshold = packet["recommendation_policy"]["analysis_quality"][
        "min_artist_weight_share" if scale_mode == "artist_summary" else "min_track_or_album_share"]
    analysis_quality = {
        "model": "sourced_tags_v1", "mode": scale_mode,
        "evidence_covered_count": evidence_covered_count,
        "source_track_count": packet["source_track_count"],
        "minimum_share": coverage_threshold,
        "actual_share": round(evidence_covered_count / packet["source_track_count"], 6),
        "passed": True, "source_coverage": source_coverage,
    }
    coverage_report_path: Path | None = None
    if coverage_degraded:
        write_coverage_report(packet, runtime_dir / "coverage_report.json")
        coverage_report_path = runtime_dir / "coverage_report.json"
    emit("completed", status="running", stage="analysis", analysis_id=packet["analysis_id"],
         classified_track_count=classified_track_count,
         evidence_covered_count=evidence_covered_count,
         source_coverage=source_coverage,
         source_track_count=packet["source_track_count"],
         analysis_mode=analysis_mode,
         parallelism=args.analysis_parallelism,
         coverage_report_path=str(coverage_report_path) if coverage_report_path else None)
    emit("task_completed", status="running", stage="analysis", task_kind="analysis_aggregate",
         task_id="analysis-aggregate", task_index=1, task_total=1, task_status="validated",
         completed=1, total=1, track_completed=packet["source_track_count"],
         track_total=packet["source_track_count"],
         classified_track_count=classified_track_count,
         message="分析包已校验，允许进入推荐阶段")
    emit("style_analysis_ready", status="running", stage="analysis",
         style_analysis=str(packet.get("overall_summary") or "").strip(),
         analysis_id=packet["analysis_id"], message="音乐风格分析已完成，正在确定最终曲目")

    emit("started", status="running", stage="recommendation", parallelism=args.recommendation_parallelism,
         message="Step 3：分析包校验通过，开始公开平台候选召回")
    emit("task_started", status="running", stage="recommendation", task_kind="platform_discovery",
         task_id="platform-discovery", task_index=1, task_total=1, task_status="running",
         message="正在从公开平台记录构造真实候选")
    bundle_path = runtime_dir / "recommendation_bundle.json"
    history_path = _playlist_history_path(source_report, snapshot, runtime_dir)
    recommendation_history = _recent_recommendation_history(history_path)
    target_recommendations = int(packet["recommendation_policy"]["target_recommendations"])
    required_candidates = max(MIN_RECOMMENDATION_CANDIDATES, target_recommendations * ATLAS_GROUP_COUNT)
    initial_limit, hard_limit = _configured_candidate_limits(args, required_candidates)
    # 同一任务失败重试时沿用本目录上一轮召回的候选（断点续跑）。
    candidates = _reuse_candidate_pool(runtime_dir, packet)
    if candidates is not None:
        candidates = _exclude_recent_recommendations(candidates, recommendation_history)[:hard_limit]
        discovery_report = {"provider": "cached_candidate_pool", "candidate_count": len(candidates),
                            "reused_from": str(runtime_dir)}
        emit("task_completed", status="running", stage="recommendation", task_kind="platform_discovery",
             task_id="platform-discovery", task_index=1, task_total=1, task_status="validated",
             completed=1, total=1, candidate_count=len(candidates),
             message=f"续跑：沿用上一轮的 {len(candidates)} 首候选")
    else:
        initial_limit, hard_limit = _configured_candidate_limits(args, required_candidates)
        prefetch_thread.join(timeout=max(0.0, min(
            _workflow_remaining(workflow_deadline, "候选预取收口", reserve=8),
            DISCOVERY_TIME_BUDGET_SECONDS + 5,
        )))
        if prefetch_thread.is_alive():
            raise ContractError("候选预取未能在全流程 120 秒预算内完成")
        if relation_thread is not None:
            relation_thread.join(timeout=max(0.0, min(
                _workflow_remaining(workflow_deadline, "关系候选收口", reserve=6),
                DISCOVERY_TIME_BUDGET_SECONDS,
            )))
            if relation_thread.is_alive():
                relation_result["error"] = ContractError("关系候选预取超出总流程时间预算")
        from collections import Counter
        from candidate_routes import resolve_candidate_route
        from contracts import target_counts
        signature_matches = _candidate_prefetch_signature(packet) == prefetch_signature
        relations_ready = relation_thread is None or "candidates" in relation_result
        quota = target_counts(target_recommendations, packet)
        needed = {kind: need * ATLAS_GROUP_COUNT for kind, need in quota.items()}
        prefetch_candidates = (
            _merge_prefetched_candidates(prefetch_result["candidates"],
                                         relation_result.get("candidates", []),
                                         recommendation_history, hard_limit,
                                         supplemental=(prefetch_result.get("window_candidates", [])
                                                       if signature_matches else []),
                                         packet=packet, needed=needed)
            if "candidates" in prefetch_result else []
        )
        available = Counter(str(resolve_candidate_route(item, packet).get("candidate_type") or item.get("candidate_type"))
                            for item in prefetch_candidates)
        shortfall = {kind: need * ATLAS_GROUP_COUNT - available[kind]
                     for kind, need in quota.items()
                     if available[kind] < need * ATLAS_GROUP_COUNT}
        gap_fill_report = None
        if ("candidates" in prefetch_result and signature_matches and relations_ready
                and len(prefetch_candidates) >= required_candidates and shortfall):
            prefetch_candidates, gap_fill_report = _fill_prefetch_route_shortfall(
                packet, prefetch_candidates, recommendation_history, needed,
                hard_limit=hard_limit, concurrency=max(args.recommendation_parallelism, 8),
                deadline=workflow_deadline,
                skip_anchor_starts=(frozenset({16}) if prefetch_result.get("window_candidates")
                                    else frozenset()),
                on_attempt=lambda start, missing: emit(
                    "stage_detail", status="running", stage="recommendation",
                    message=(f"已有 {len(prefetch_candidates)} 首已核验候选；"
                             f"按新艺人窗口 {start + 1}–{start + 16} 补充缺少的候选路线 {missing}")),
            )
            shortfall = gap_fill_report["route_shortfall"]
        can_reuse_prefetch = ("candidates" in prefetch_result and signature_matches
                              and relations_ready and len(prefetch_candidates) >= required_candidates
                              )
        if can_reuse_prefetch:
            candidates = prefetch_candidates
            base = prefetch_result["report"]
            extra = relation_result.get("report", {})
            discovery_report = {
                **base, "provider": "parallel_verified_discovery",
                "candidate_count": len(candidates),
                "relation_candidate_count": len(relation_result.get("candidates", [])),
                "playlist_excluded_count": int(base.get("playlist_excluded_count", 0) or 0)
                    + int(extra.get("playlist_excluded_count", 0) or 0),
                "history_excluded_count": int(base.get("history_excluded_count", 0) or 0)
                    + int(extra.get("history_excluded_count", 0) or 0),
                "verification_rejected_count": int(base.get("verification_rejected_count", 0) or 0)
                    + int(extra.get("verification_rejected_count", 0) or 0)
                    + int((prefetch_result.get("window_report") or {}).get("verification_rejected_count", 0) or 0),
            }
            if prefetch_result.get("window_candidates"):
                discovery_report["prefetch_window"] = prefetch_result["window_report"]
            if gap_fill_report is not None:
                discovery_report["provider"] = "parallel_verified_discovery_with_gap_fill"
                discovery_report["gap_fill"] = gap_fill_report
        if not can_reuse_prefetch:
            error = prefetch_result.get("error") or relation_result.get("error")
            diagnostic = {"prefetch_count": len(prefetch_candidates),
                          "route_counts": dict(available), "route_shortfall": shortfall,
                          "signature_matches": signature_matches,
                          "relations_ready": relations_ready}
            print(f"[discovery] 并行预取未满足最终分析包与配比，重新召回：{error or '候选缺口'}；"
                  + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
                  file=sys.stderr, flush=True)
            candidates, discovery_report = _discover_unique_candidates(
                packet, recommendation_history, initial_limit, required_candidates,
                lambda previous, current, eligible: emit(
                    "stage_detail", status="running", stage="recommendation",
                    message=(f"排除原歌单与近一周推荐后暂有 {eligible}/{required_candidates} 首，"
                             f"正在扩大候选召回范围 {previous}→{current}")),
                hard_limit=hard_limit, max_rounds=args.max_research_rounds,
                concurrency=max(args.recommendation_parallelism, 8),
                deadline=workflow_deadline,
            )
            if prefetch_candidates and signature_matches:
                # Both sets have already passed identity checks in this run.
                # Keep additional non-duplicate evidence without replacing the
                # final packet's own discovery priority.
                candidates = _exclude_recent_recommendations(
                    _merge_candidates(candidates, prefetch_candidates),
                    recommendation_history,
                )[:hard_limit]
        _save_candidate_pool(runtime_dir, packet, candidates)
    write_json(runtime_dir / "platform_discovery.json", discovery_report)
    if len(candidates) < required_candidates:
        raise ContractError(
            f"排除原歌单与近一周推荐后仍只有 {len(candidates)} 首可核验候选，"
            f"三组 Atlas 至少需要 {required_candidates} 首；"
            f"原歌单排除 {discovery_report.get('playlist_excluded_count', 0)} 首，"
            f"近一周推荐排除 {discovery_report.get('history_excluded_count', 0)} 首，"
            f"事实核验未通过 {discovery_report.get('verification_rejected_count', 0)} 首，"
            f"还缺 {required_candidates - len(candidates)} 首")
    emit("task_completed", status="running", stage="recommendation", task_kind="platform_discovery",
         task_id="platform-discovery", task_index=1, task_total=1, task_status="validated", completed=1, total=1,
         candidate_count=len(candidates), message=f"已取得 {len(candidates)} 首排除原歌单与一周缓存后的真实候选")
    emit("stage_detail", status="running", stage="recommendation",
         message=f"正在从 {len(candidates)} 首已核验候选确定三组 Atlas，并校验身份、来源、排除、去重与配比")
    candidates, atlas_groups, selection_validation = _select_review_groups_fast(
        packet, candidates, required=required_candidates, stage="recommendation",
    )
    if packet.get("route_reallocation"):
        discovery_report["route_reallocation"] = packet["route_reallocation"]
        write_json(runtime_dir / "platform_discovery.json", discovery_report)
        # The effective mix belongs to this recommendation run. Keep the
        # source Step 2 directory immutable and export the runtime copy.
        write_json(runtime_dir / "musician_analysis.json", packet)
    write_json(bundle_path, atlas_groups[0])
    for index, group in enumerate(atlas_groups, 1):
        write_json(runtime_dir / f"recommendation_bundle_group_{index}.json", group)
    selection_path = _write_locked_selection(runtime_dir, snapshot, packet, atlas_groups, source_track_count)
    emit("tracks_locked", status="running", stage="recommendation",
         selection_path=str(selection_path),
         recommendation_group_count=ATLAS_GROUP_COUNT,
         total_unique_recommendation_count=sum(len(group["recommendations"]) for group in atlas_groups),
         message="三组 Atlas 曲目与顺序已确定，正在补充资料细节")
    recommendation_summary = {
        "recommendation_count": len(atlas_groups[0]["recommendations"]),
        "recommendation_group_count": len(atlas_groups),
        "total_unique_recommendation_count": sum(len(group["recommendations"]) for group in atlas_groups),
        "research_rounds": 1,
        "candidate_count": len(candidates),
        "platform_discovery": discovery_report,
    }
    emit("completed", status="running", stage="recommendation",
         recommendation_count=recommendation_summary["recommendation_count"],
         recommendation_group_count=recommendation_summary["recommendation_group_count"],
         total_unique_recommendation_count=recommendation_summary["total_unique_recommendation_count"],
         candidate_research_rounds=recommendation_summary["research_rounds"],
         parallelism=args.recommendation_parallelism)

    payload_path = runtime_dir / "web_payload.json"
    # 断点续跑：若上一次已完整导出并发布，直接结束（不重复生成三组网页数据）。
    resume_report = runtime_dir / "web_job_report.json"
    resume_payload = runtime_dir / "web_payload.json"
    if resume_report.is_file() and resume_payload.is_file():
        try:
            saved_report = read_json(resume_report)
        except Exception:
            saved_report = None
        if (isinstance(saved_report, dict) and saved_report.get("status") == "completed"
                and saved_report.get("analysis_id") == packet.get("analysis_id")):
            emit("task_completed", status="completed", stage="export", task_kind="atlas_export",
                 task_id="atlas-export", task_index=1, task_total=1, task_status="published",
                 completed=1, total=1, message="续跑：上一次已完成导出，本次直接结束")
            emit("completed", status="completed", stage="export", payload_path=str(resume_payload),
                 current_data_path=str(Path(args.current_data).resolve()) if args.current_data else None,
                 recommendation_count=target, recommendation_group_count=ATLAS_GROUP_COUNT,
                 message="续跑：沿用已发布的 Atlas")
            return 0

    emit("started", status="running", stage="export", message="生成网页数据并更新当前页面" if args.current_data else "生成网页验收数据")
    _workflow_remaining(workflow_deadline, "最终网页导出", reserve=1)
    emit("task_started", status="running", stage="export", task_kind="atlas_export",
         task_id="atlas-export", task_index=1, task_total=1, task_status="running",
         message="正在生成并发布三组 Atlas" if args.current_data else "正在生成三组 Atlas 验收预览")
    group_payloads: list[dict[str, Any]] = []
    group_summaries: list[dict[str, Any]] = []
    for index in range(1, ATLAS_GROUP_COUNT + 1):
        group_payload_path = runtime_dir / f"web_payload_group_{index}.json"
        group_summary = export_web_payload(
            runtime_dir, group_payload_path,
            bundle_path=runtime_dir / f"recommendation_bundle_group_{index}.json",
            editorial_path=editorial_path,
            publish_web_result=bool(args.current_data),
        )
        group_summaries.append(group_summary)
        group_payloads.append(read_json(group_payload_path))
    combined_payload = dict(group_payloads[0])
    combined_payload["atlas_groups"] = [
        {
            "id": f"atlas-{index + 1}", "label": f"第 {index + 1} 组",
            "recommendations": payload["recommendations"],
            "artists": payload["artists"], "albums": payload["albums"],
            "sources": payload["sources"],
        }
        for index, payload in enumerate(group_payloads)
    ]
    combined_payload["atlas_group_count"] = ATLAS_GROUP_COUNT
    combined_payload["atlas_swap_limit"] = ATLAS_GROUP_COUNT - 1
    combined_payload["candidate_count"] = len(candidates)
    combined_payload["recommendation_history_days"] = RECOMMENDATION_HISTORY_DAYS
    write_json(payload_path, combined_payload)
    web_summary = {
        **group_summaries[0],
        "recommendation_group_count": ATLAS_GROUP_COUNT,
        "total_unique_recommendation_count": recommendation_summary["total_unique_recommendation_count"],
        "candidate_count": len(candidates),
    }
    workflow_elapsed = time.monotonic() - workflow_started
    if workflow_elapsed > workflow_budget:
        raise ContractError(f"全流程超过 {workflow_budget} 秒预算（实际 {workflow_elapsed:.1f} 秒）")
    first_run_elapsed = workflow_elapsed + (snapshot_elapsed if await_limit else 0.0)
    completed_report = {
            "schema_version": "2.0",
            "artifact_type": "web_job_report",
            "status": "completed",
            "completed_at": utc_now(),
            "source": source_report,
            "snapshot_id": snapshot["snapshot_id"],
            "analysis_id": packet["analysis_id"],
            "analysis_parallelism": args.analysis_parallelism,
            "recommendation_parallelism": args.recommendation_parallelism,
            "workflow_time_budget_seconds": workflow_budget,
            "workflow_elapsed_seconds": round(workflow_elapsed, 3),
            "snapshot_elapsed_seconds": round(snapshot_elapsed, 3),
            "first_run_elapsed_seconds": round(first_run_elapsed, 3),
            "first_run_within_budget": first_run_elapsed <= workflow_budget,
            "source_track_count": source_track_count,
            "processed_track_count": packet["source_track_count"],
            "analysis_quality": analysis_quality,
            "requested_track_limit": (snapshot.get("reader") or {}).get("requested_track_limit"),
            "requested_track_percentile": (snapshot.get("reader") or {}).get("requested_track_percentile"),
            "platform_discovery": discovery_report,
            "recommendation_groups": {
                "count": ATLAS_GROUP_COUNT,
                "swap_limit": ATLAS_GROUP_COUNT - 1,
                "total_unique_recommendation_count": recommendation_summary["total_unique_recommendation_count"],
            },
            "recommendation_history": {
                "path": str(history_path),
                "retention_days": RECOMMENDATION_HISTORY_DAYS,
                "excluded_count": discovery_report["history_excluded_count"],
            },
            "relationship_research": packet.get("relationship_research", {}),
            "selection_validation": selection_validation,
            "web_export": web_summary,
            "payload_path": str(payload_path),
            "current_data_path": str(Path(args.current_data).resolve()) if args.current_data else None,
    }
    _commit_completed_workflow(
        payload_path=payload_path,
        current_data_path=Path(args.current_data) if args.current_data else None,
        history_path=history_path,
        history=recommendation_history,
        groups=atlas_groups,
        playlist_identity={"kind": source_report.get("kind"), "playlist_id": snapshot.get("playlist_id")},
        report_path=runtime_dir / "web_job_report.json",
        report=completed_report,
        deadline=workflow_deadline,
    )
    emit("completed", status="completed", stage="export", payload_path=str(payload_path),
         current_data_path=str(Path(args.current_data).resolve()) if args.current_data else None,
         recommendation_count=web_summary["recommendation_count"],
         recommendation_group_count=ATLAS_GROUP_COUNT,
         workflow_elapsed_seconds=round(time.monotonic() - workflow_started, 3),
         first_run_elapsed_seconds=round(time.monotonic() - workflow_started + (snapshot_elapsed if await_limit else 0.0), 3))
    emit("task_completed", status="completed", stage="export", task_kind="atlas_export",
         task_id="atlas-export", task_index=1, task_total=1, task_status="published" if args.current_data else "validated",
         completed=1, total=1, recommendation_count=web_summary["recommendation_count"],
         recommendation_group_count=ATLAS_GROUP_COUNT,
         message="三组 Atlas 已更新" if args.current_data else "三组 Atlas 验收预览已生成")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Music Atlas 网页端受控三阶段工作流")
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--current-data", default=None)
    parser.add_argument("--source-kind", required=True, choices=sorted(SOURCE_KINDS))
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--input", default=None)
    parser.add_argument("--playlist-id", default=None)
    parser.add_argument("--playlist-name", default=None)
    parser.add_argument("--platform", default=None)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--await-track-limit", action="store_true",
                        help="歌单读取完成后暂停，等待网页提交处理数量再继续")
    parser.add_argument("--preview", action="store_true", help="网页任务启用公开平台推荐预览")
    parser.add_argument("--await-limit-timeout", type=int, default=DEFAULT_AWAIT_LIMIT_TIMEOUT_SECONDS)
    parser.add_argument("--analysis-command", default=None)
    parser.add_argument("--recommendation-command", default=None)
    parser.add_argument("--analysis-parallelism", type=int, choices=tuple(range(1, 17)), default=5)
    parser.add_argument("--recommendation-parallelism", type=int, choices=tuple(range(1, 9)), default=4)
    parser.add_argument("--analysis-timeout", type=int, default=SUMMARY_AGENT_TIME_BUDGET_SECONDS)
    parser.add_argument("--recommendation-timeout", type=int, default=AGENT_TIME_BUDGET_SECONDS)
    parser.add_argument("--workflow-time-budget", type=int, default=DEFAULT_WORKFLOW_TIME_BUDGET_SECONDS,
                        help="网页任务从启动到 completed 的总墙钟预算，默认 120 秒")
    parser.add_argument("--recommendation-only", action="store_true")
    parser.add_argument("--source-runtime-dir", default=None)
    parser.add_argument("--max-research-rounds", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--initial-candidate-limit", "--max-candidates",
                        dest="initial_candidate_limit", type=int, default=48)
    parser.add_argument("--hard-candidate-limit", type=int, default=120)
    parser.add_argument("--policy-file", default=None, help="网页任务使用的推荐策略部分覆盖")
    parser.add_argument("--editorial", default=None, help="网页任务使用的 editorial 展示配置")
    return parser


def _can_resume_from_analysis(args: argparse.Namespace) -> bool:
    """同一任务失败重试时，若分析包存在且通过契约校验，则跳过 Step 1–2。

    仅用于断点续跑：不校验通过的半成品不会复用，避免把坏结果带到推荐阶段。
    """
    try:
        runtime_dir = Path(args.runtime_dir).resolve()
        analysis_file = runtime_dir / "musician_analysis.json"
        snapshot_file = runtime_dir / "snapshot.json"
        if not analysis_file.is_file() or not snapshot_file.is_file():
            return False
        snapshot = validate_playlist_snapshot(read_json(snapshot_file), require_complete=True)
        from contracts import validate_analysis_packet
        packet = read_json(analysis_file)
        validate_analysis_packet(packet)
        # 基础 schema 允许正在生成的半成品；只有完整 Step 2 才可进入推荐。
        if not isinstance(packet.get("overall_summary"), str) or not packet["overall_summary"].strip():
            return False
        if not isinstance(packet.get("agent_islands"), list) or len(packet["agent_islands"]) != 3:
            return False
        if not isinstance(packet.get("source_tags"), dict) or not isinstance(packet["source_tags"].get("records"), list):
            return False
        if not packet.get("selection_mode"):
            return False
        _ensure_resume_playlist_exclusion(packet, snapshot)
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        if args.recommendation_only:
            return run_recommendation_only(args)
        if _can_resume_from_analysis(args):
            # 断点续跑：同一任务重试时直接进入推荐阶段（Step 1–2 的产物已就绪）。
            print('[resume] 检测到有效的分析包，跳过 Step 1–2（断点续跑）', file=sys.stderr, flush=True)
            setattr(args, "source_runtime_dir", str(Path(args.runtime_dir).resolve()))
            setattr(args, "recommendation_only", True)
            return run_recommendation_only(args)
        return run_web_workflow(args)
    except (ContractError, OSError, ValueError) as exc:
        emit("failed", status="failed", stage="workflow", error=str(exc))
        print(f"音乐网页工作流未执行：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
