"""Bounded candidate research; no accounts, historical preferences, or policy updates."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from copy import deepcopy
from typing import Any, Callable

from agent_prompt import apply_context_budget
from candidate_routes import resolve_candidate_route
from contracts import ContractError, SCHEMA_VERSION, _validate_candidate_pool, normalized_text, target_counts, track_key
from evidence import require_usable_evidence
from recommender import SelectionSearchBudgetExceeded, rank_bundle


class ResearchFailure(ContractError):
    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


class ResearchExhausted(ResearchFailure):
    pass


def supplemental_prompt(prepared: str, request: dict[str, Any], budget: int | None) -> str:
    marker = "\n```json\n"
    start = prepared.rfind(marker)
    payload = json.loads(prepared[start + len(marker):].rsplit("\n```", 1)[0])
    payload["research_request"] = request
    complete = prepared[:start] + marker + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
    return apply_context_budget(complete, budget)[0]


def research_candidates(packet: dict[str, Any], prepared: str, command: str, *,
                        execute: Callable[..., dict[str, Any]], timeout: int, context_budget: int | None,
                        max_rounds: int, candidate_target: int, max_candidates: int) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    pool: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set(packet["favorite_track_keys"])
    seen_platform_ids = {normalized_text(item.get("platform_track_id")).casefold() for item in packet["favorite_tracks"] if item.get("platform_track_id")}
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "artifact_type": "research_report", "analysis_id": packet["analysis_id"],
                              "candidate_target": candidate_target, "max_candidates": max_candidates, "max_rounds": max_rounds,
                              "rounds": [], "route_corrections": [], "skipped_duplicates_or_favorites": 0, "policy_changed": False}
    prompt = prepared
    failure = "候选不足"
    for round_number in range(1, max_rounds + 1):
        remaining = timeout - (time.perf_counter() - started)
        if remaining <= 0:
            failure = "候选研究总超时预算已耗尽"
            break
        round_start = time.perf_counter()
        round_report = {"round": round_number, "input_characters": len(prompt),
                        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                        "output_characters": None}
        report["rounds"].append(round_report)
        try:
            raw = execute(command, prompt, timeout=math.ceil(remaining))
        except (ContractError, OSError) as exc:
            round_report.update(status="failed", elapsed_ms=round((time.perf_counter() - round_start) * 1000, 2))
            report.update(status="agent_failed", error=str(exc), accepted_candidate_count=len(pool),
                          elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
            raise ResearchFailure(f"候选研究中断：{exc}；保留研究报告，不输出推荐", report) from exc
        round_report.update(status="returned", output_characters=len(json.dumps(raw, ensure_ascii=False)),
                            elapsed_ms=round((time.perf_counter() - round_start) * 1000, 2))
        if raw.get("analysis_id") != packet["analysis_id"] or raw.get("schema_version") != SCHEMA_VERSION or raw.get("bundle_type") != "recommendation_bundle":
            raise ContractError("Agent 返回的研究包不属于当前分析或 schema 无效")
        if not isinstance(raw.get("generated_at"), str) or not raw["generated_at"].strip():
            raise ContractError("研究包缺少 generated_at")
        if raw.get("status") == "insufficient_evidence":
            result = rank_bundle(raw, packet)
            report.update(status="insufficient_evidence", accepted_candidate_count=len(pool))
            return result, report
        if raw.get("status") != "ready" or raw.get("bundle_stage") != "candidate_pool" or raw.get("recommendations") not in (None, []):
            raise ContractError("Agent 的 ready 输出必须是未排序 candidate_pool")
        if "publication_status" in raw or raw.get("ranking") is not None:
            raise ContractError("Agent 不得提交程序拥有的发布或排序字段")
        batch = raw.get("candidate_pool")
        if not isinstance(batch, list) or len(batch) > max_candidates - len(pool):
            raise ContractError("候选返回数量超过预算或不是数组")
        for original in batch:
            _validate_candidate_pool([original], known_refs=set(packet["analysis_ref_ids"]),
                                     known_style_refs=set(packet["style_analysis"]["known_style_refs"]), require_all_types=False)
            require_usable_evidence(original)
            candidate = deepcopy(original)
            candidate["candidate_type"] = resolve_candidate_route(candidate, packet)["candidate_type"]
            if candidate["candidate_type"] != original["candidate_type"]:
                report["route_corrections"].append({"canonical_track_id": candidate["canonical_track_id"],
                                                   "declared": original["candidate_type"], "resolved": candidate["candidate_type"]})
            _validate_candidate_pool([candidate], known_refs=set(packet["analysis_ref_ids"]),
                                     known_style_refs=set(packet["style_analysis"]["known_style_refs"]), require_all_types=False)
            identity = candidate["canonical_track_id"].casefold()
            key = track_key(candidate["title"], candidate["artist"])
            platform_id = normalized_text(candidate.get("platform_track_id")).casefold()
            if identity in seen_ids or key in seen_keys or (platform_id and platform_id in seen_platform_ids):
                report["skipped_duplicates_or_favorites"] += 1
                continue
            pool.append(candidate)
            seen_ids.add(identity)
            seen_keys.add(key)
            if platform_id:
                seen_platform_ids.add(platform_id)
        result = {"schema_version": SCHEMA_VERSION, "bundle_type": "recommendation_bundle", "bundle_stage": "candidate_pool",
                  "status": "ready", "analysis_id": packet["analysis_id"], "generated_at": raw.get("generated_at"),
                  "candidate_pool": pool, "recommendations": []}
        if len(pool) >= candidate_target:
            try:
                ranked = rank_bundle(result, packet)
            except SelectionSearchBudgetExceeded:
                raise
            except ContractError as exc:
                failure = str(exc)
            else:
                report.update(status="ready", accepted_candidate_count=len(pool),
                              selected_count=len(ranked["recommendations"]), elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
                return ranked, report
        counts = Counter(item["candidate_type"] for item in pool)
        deficits = {kind: max(0, count - counts[kind]) for kind, count in target_counts(candidate_target, packet).items()}
        report.update(accepted_candidate_count=len(pool), deficits=deficits)
        if len(pool) >= max_candidates or round_number == max_rounds:
            break
        request = {"round": round_number + 1, "requested_type_counts": deficits, "constraint_failure": failure,
                   "remaining_candidate_budget": max_candidates - len(pool), "exclude_canonical_ids": sorted(seen_ids),
                   "instruction": "只补充当前缺额或有助于满足约束的新候选；不重复已有候选，不写最终推荐说明。"}
        try:
            prompt = supplemental_prompt(prepared, request, context_budget)
        except ContractError as exc:
            failure = str(exc)
            break
    report.update(status="budget_exhausted", elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
    raise ResearchExhausted(f"候选研究未完成：{failure}；保留研究报告，不输出不足 10 首或降低门槛的推荐", report)
