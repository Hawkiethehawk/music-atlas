"""Bounded candidate research; no accounts, historical preferences, or policy updates."""

from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import Counter
from copy import deepcopy
from typing import Any, Callable

from agent_prompt import apply_context_budget
from candidate_routes import resolve_candidate_route
from contracts import ContractError, SCHEMA_VERSION, _validate_candidate_pool, normalized_text, parse_timestamp, target_counts, track_key
from evidence import require_usable_evidence
from recommender import SelectionSearchBudgetExceeded, rank_bundle


class ResearchFailure(ContractError):
    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


class ResearchExhausted(ResearchFailure):
    pass


def _validate_parallelism(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
        raise ContractError("recommendation-parallelism 必须是 1 到 8 的整数")
    return value


def _split_integer(total: int, parts: int) -> list[int]:
    base, remainder = divmod(max(0, total), parts)
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def _split_type_counts(counts: dict[str, int], parts: int) -> list[dict[str, int]]:
    result = [{} for _ in range(parts)]
    for candidate_type, count in counts.items():
        for offset in range(max(0, int(count))):
            index = offset % parts
            result[index][candidate_type] = result[index].get(candidate_type, 0) + 1
    return result


def _parallel_worker_requests(
    *,
    round_number: int,
    worker_count: int,
    requested_type_counts: dict[str, int],
    remaining_candidate_budget: int,
    exclude_canonical_ids: set[str],
) -> list[dict[str, Any]]:
    type_shards = _split_type_counts(requested_type_counts, worker_count)
    budget_shards = _split_integer(remaining_candidate_budget, worker_count)
    return [
        {
            "round": round_number,
            "parallel_worker": index + 1,
            "parallel_workers": worker_count,
            "requested_type_counts": type_shards[index],
            "candidate_budget": budget_shards[index],
            "remaining_candidate_budget": remaining_candidate_budget,
            "exclude_canonical_ids": sorted(exclude_canonical_ids),
            "instruction": (
                "这是并行候选研究的一个分片；只研究 requested_type_counts 指定的候选类型，"
                "最多返回 candidate_budget 首。返回 candidate_pool 事实，不写最终推荐、评分或排序。"
            ),
        }
        for index in range(worker_count)
    ]


def supplemental_prompt(prepared: str, request: dict[str, Any], budget: int | None) -> str:
    marker = "\n```json\n"
    start = prepared.rfind(marker)
    payload = json.loads(prepared[start + len(marker):].rsplit("\n```", 1)[0])
    payload["research_request"] = request
    complete = prepared[:start] + marker + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
    # A supplemental request extends an already prepared context. Do not
    # rewrite its fixed analysis payload merely to make room for the request;
    # if the hard budget is full, fail closed and stop the research loop.
    return apply_context_budget(complete, budget, allow_payload_compaction=False)[0]


def research_candidates(packet: dict[str, Any], prepared: str, command: str, *,
                        execute: Callable[..., dict[str, Any]], timeout: int, context_budget: int | None,
                        max_rounds: int, candidate_target: int, max_candidates: int,
                        parallelism: int = 1,
                        progress: Callable[[dict[str, Any]], None] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_parallelism(parallelism)
    started = time.perf_counter()
    pool: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set(packet["favorite_track_keys"])
    seen_platform_ids = {normalized_text(item.get("platform_track_id")).casefold() for item in packet["favorite_tracks"] if item.get("platform_track_id")}
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "artifact_type": "research_report", "analysis_id": packet["analysis_id"],
                              "candidate_target": candidate_target, "max_candidates": max_candidates, "max_rounds": max_rounds,
                              "parallelism": parallelism, "rounds": [], "route_corrections": [],
                              "skipped_duplicates_or_favorites": 0, "truncated_candidate_count": 0,
                              "policy_changed": False}
    prompt = prepared
    failure = "候选不足"
    requested_counts: dict[str, int] | None = None

    def notify(event: str, **payload: Any) -> None:
        if progress is not None:
            progress({"event": event, "stage": "recommendation", **payload})

    def run_one(worker_prompt: str, timeout_budget: int) -> tuple[dict[str, Any] | None, Exception | None]:
        try:
            return execute(command, worker_prompt, timeout=timeout_budget), None
        except Exception as exc:  # Keep every worker outcome for the diagnostic report.
            return None, exc

    for round_number in range(1, max_rounds + 1):
        remaining = timeout - (time.perf_counter() - started)
        if remaining <= 0:
            failure = "候选研究总超时预算已耗尽"
            break
        round_start = time.perf_counter()
        if parallelism == 1:
            worker_prompts = [prompt]
        else:
            if requested_counts is None:
                requested_counts = target_counts(candidate_target, packet)
            worker_requests = _parallel_worker_requests(
                round_number=round_number,
                worker_count=parallelism,
                requested_type_counts=requested_counts,
                remaining_candidate_budget=max_candidates - len(pool),
                exclude_canonical_ids=seen_ids,
            )
            try:
                worker_prompts = [
                    supplemental_prompt(prepared, request, context_budget)
                    for request in worker_requests
                ]
            except ContractError as exc:
                failure = str(exc)
                break
        round_report = {
            "round": round_number,
            "input_characters": sum(len(item) for item in worker_prompts),
            "prompt_sha256": hashlib.sha256("\n".join(worker_prompts).encode("utf-8")).hexdigest(),
            "output_characters": None,
            "worker_count": len(worker_prompts),
            "workers": [
                {
                    "worker_index": index + 1,
                    "input_characters": len(worker_prompt),
                    "prompt_sha256": hashlib.sha256(worker_prompt.encode("utf-8")).hexdigest(),
                    "status": "started",
                }
                for index, worker_prompt in enumerate(worker_prompts)
            ],
        }
        report["rounds"].append(round_report)
        notify(
            "round_started",
            task_kind="recommendation_round",
            round=round_number,
            round_total=max_rounds,
            worker_total=len(worker_prompts),
            parallelism=parallelism,
            parallel_slots=len(worker_prompts),
            accepted_candidate_count=len(pool),
            candidate_target=candidate_target,
            candidate_budget=max_candidates - len(pool),
            message=f"第 {round_number}/{max_rounds} 轮候选研究，启动 {len(worker_prompts)} 个并行任务",
        )
        worker_timeout = math.ceil(remaining)
        outcomes: list[tuple[dict[str, Any] | None, Exception | None]] = []
        if len(worker_prompts) == 1:
            worker_started = time.perf_counter()
            notify(
                "task_started",
                task_kind="recommendation_worker",
                task_id=f"recommendation-r{round_number}-w1",
                round=round_number,
                round_total=max_rounds,
                worker_index=1,
                worker_total=1,
                task_index=1,
                task_total=1,
                task_status="running",
                parallelism=parallelism,
                parallel_slots=1,
                accepted_candidate_count=len(pool),
                candidate_target=candidate_target,
            )
            outcome = run_one(worker_prompts[0], worker_timeout)
            outcomes.append(outcome)
            worker_elapsed = round((time.perf_counter() - worker_started) * 1000, 2)
            raw, error = outcome
            if error is not None:
                round_report["workers"][0].update(status="failed", error=str(error), elapsed_ms=worker_elapsed)
                notify(
                    "task_failed",
                    task_kind="recommendation_worker",
                    task_id=f"recommendation-r{round_number}-w1",
                    round=round_number,
                    round_total=max_rounds,
                    worker_index=1,
                    worker_total=1,
                    task_index=1,
                    task_total=1,
                    task_status="failed",
                    completed=1,
                    total=1,
                    parallelism=parallelism,
                    parallel_slots=1,
                    accepted_candidate_count=len(pool),
                    candidate_target=candidate_target,
                    error=str(error),
                    elapsed_ms=worker_elapsed,
                )
            else:
                output_characters = len(json.dumps(raw, ensure_ascii=False)) if isinstance(raw, dict) else None
                round_report["workers"][0].update(status="returned", output_characters=output_characters, elapsed_ms=worker_elapsed)
                notify(
                    "task_completed",
                    task_kind="recommendation_worker",
                    task_id=f"recommendation-r{round_number}-w1",
                    round=round_number,
                    round_total=max_rounds,
                    worker_index=1,
                    worker_total=1,
                    task_index=1,
                    task_total=1,
                    task_status="returned",
                    completed=1,
                    total=1,
                    parallelism=parallelism,
                    parallel_slots=1,
                    accepted_candidate_count=len(pool),
                    candidate_target=candidate_target,
                    output_characters=output_characters,
                    elapsed_ms=worker_elapsed,
                )
        else:
            executor = ThreadPoolExecutor(max_workers=len(worker_prompts), thread_name_prefix="music-atlas-recommendation")
            worker_started_at = {}
            for index in range(len(worker_prompts)):
                worker_started_at[index] = time.perf_counter()
                notify(
                    "task_started",
                    task_kind="recommendation_worker",
                    task_id=f"recommendation-r{round_number}-w{index + 1}",
                    round=round_number,
                    round_total=max_rounds,
                    worker_index=index + 1,
                    worker_total=len(worker_prompts),
                    task_index=index + 1,
                    task_total=len(worker_prompts),
                    task_status="running",
                    parallelism=parallelism,
                    parallel_slots=len(worker_prompts),
                    accepted_candidate_count=len(pool),
                    candidate_target=candidate_target,
                )
            futures = {
                executor.submit(run_one, worker_prompt, worker_timeout): index
                for index, worker_prompt in enumerate(worker_prompts)
            }
            try:
                outcomes_by_index: dict[int, tuple[dict[str, Any] | None, Exception | None]] = {}
                pending = set(futures)
                finished_workers = 0
                while pending:
                    remaining_now = timeout - (time.perf_counter() - started)
                    if remaining_now <= 0:
                        done_now = set()
                    else:
                        done_now, pending = wait(
                            pending,
                            timeout=max(0.0, remaining_now),
                            return_when=FIRST_COMPLETED,
                        )
                    if not done_now:
                        break
                    for future in done_now:
                        index = futures[future]
                        worker_elapsed = round((time.perf_counter() - worker_started_at[index]) * 1000, 2)
                        try:
                            outcome = future.result()
                        except Exception as exc:  # pragma: no cover - defensive future boundary
                            outcome = (None, exc)
                        outcomes_by_index[index] = outcome
                        finished_workers += 1
                        raw, error = outcome
                        if error is not None:
                            round_report["workers"][index].update(status="failed", error=str(error), elapsed_ms=worker_elapsed)
                            notify(
                                "task_failed",
                                task_kind="recommendation_worker",
                                task_id=f"recommendation-r{round_number}-w{index + 1}",
                                round=round_number,
                                round_total=max_rounds,
                                worker_index=index + 1,
                                worker_total=len(worker_prompts),
                                task_index=index + 1,
                                task_total=len(worker_prompts),
                                task_status="failed",
                                completed=finished_workers,
                                total=len(worker_prompts),
                                parallelism=parallelism,
                                parallel_slots=len(worker_prompts),
                                accepted_candidate_count=len(pool),
                                candidate_target=candidate_target,
                                error=str(error),
                                elapsed_ms=worker_elapsed,
                            )
                        else:
                            output_characters = len(json.dumps(raw, ensure_ascii=False)) if isinstance(raw, dict) else None
                            round_report["workers"][index].update(status="returned", output_characters=output_characters, elapsed_ms=worker_elapsed)
                            notify(
                                "task_completed",
                                task_kind="recommendation_worker",
                                task_id=f"recommendation-r{round_number}-w{index + 1}",
                                round=round_number,
                                round_total=max_rounds,
                                worker_index=index + 1,
                                worker_total=len(worker_prompts),
                                task_index=index + 1,
                                task_total=len(worker_prompts),
                                task_status="returned",
                                completed=finished_workers,
                                total=len(worker_prompts),
                                parallelism=parallelism,
                                parallel_slots=len(worker_prompts),
                                accepted_candidate_count=len(pool),
                                candidate_target=candidate_target,
                                output_characters=output_characters,
                                elapsed_ms=worker_elapsed,
                            )
                not_done = pending
                for future in not_done:
                    future.cancel()
                    index = futures[future]
                    error = ContractError("候选研究总超时预算已耗尽")
                    worker_elapsed = round((time.perf_counter() - worker_started_at[index]) * 1000, 2)
                    outcomes_by_index[index] = (None, error)
                    finished_workers += 1
                    round_report["workers"][index].update(status="failed", error=str(error), elapsed_ms=worker_elapsed)
                    notify(
                        "task_failed",
                        task_kind="recommendation_worker",
                        task_id=f"recommendation-r{round_number}-w{index + 1}",
                        round=round_number,
                        round_total=max_rounds,
                        worker_index=index + 1,
                        worker_total=len(worker_prompts),
                        task_index=index + 1,
                        task_total=len(worker_prompts),
                        task_status="timeout",
                        completed=finished_workers,
                        total=len(worker_prompts),
                        parallelism=parallelism,
                        parallel_slots=len(worker_prompts),
                        accepted_candidate_count=len(pool),
                        candidate_target=candidate_target,
                        error=str(error),
                        elapsed_ms=worker_elapsed,
                    )
                outcomes = [outcomes_by_index.get(index, (None, ContractError("候选研究总超时预算已耗尽")))
                            for index in range(len(worker_prompts))]
            finally:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except TypeError:  # pragma: no cover - Python < 3.9 compatibility
                    executor.shutdown(wait=False)

        errors: list[Exception] = []
        returned: list[dict[str, Any]] = []
        output_characters = 0
        insufficient_count = 0
        for index, (raw, error) in enumerate(outcomes):
            worker_report = round_report["workers"][index]
            if error is not None:
                worker_report.update(status="failed", error=str(error))
                errors.append(error)
                continue
            if not isinstance(raw, dict):
                error = ContractError("Agent 输出必须是 JSON 对象")
                worker_report.update(status="failed", error=str(error))
                errors.append(error)
                continue
            serialized = json.dumps(raw, ensure_ascii=False)
            output_characters += len(serialized)
            worker_report.update(status="returned", output_characters=len(serialized))
            if raw.get("analysis_id") != packet["analysis_id"] or raw.get("schema_version") != SCHEMA_VERSION or raw.get("bundle_type") != "recommendation_bundle":
                error = ContractError("Agent 返回的研究包不属于当前分析或 schema 无效")
            elif not isinstance(raw.get("generated_at"), str) or not raw["generated_at"].strip():
                error = ContractError("研究包缺少 generated_at")
            elif raw.get("status") == "insufficient_evidence":
                insufficient_count += 1
            elif raw.get("status") != "ready" or raw.get("bundle_stage") != "candidate_pool" or raw.get("recommendations") not in (None, []):
                error = ContractError("Agent 的 ready 输出必须是未排序 candidate_pool")
            elif "publication_status" in raw or raw.get("ranking") is not None:
                error = ContractError("Agent 不得提交程序拥有的发布或排序字段")
            else:
                returned.append(raw)
            if error is not None:
                worker_report.update(status="failed", error=str(error))
                errors.append(error)

        round_report.update(
            status="failed" if errors else "returned",
            output_characters=output_characters if output_characters else None,
            elapsed_ms=round((time.perf_counter() - round_start) * 1000, 2),
        )
        if errors:
            error = errors[0]
            report.update(status="agent_failed", error=str(error), accepted_candidate_count=len(pool),
                          elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
            notify(
                "round_failed",
                task_kind="recommendation_round",
                round=round_number,
                round_total=max_rounds,
                worker_total=len(worker_prompts),
                parallelism=parallelism,
                parallel_slots=len(worker_prompts),
                accepted_candidate_count=len(pool),
                candidate_target=candidate_target,
                error=str(error),
            )
            raise ResearchFailure(f"候选研究中断：{error}；保留研究报告，不输出推荐", report) from error
        if not returned and insufficient_count == len(outcomes):
            raw = next((item for item, error in outcomes if item is not None), None)
            if raw is not None:
                result = rank_bundle(raw, packet)
                report.update(status="insufficient_evidence", accepted_candidate_count=len(pool))
                notify(
                    "round_completed",
                    task_kind="recommendation_round",
                    round=round_number,
                    round_total=max_rounds,
                    worker_total=len(worker_prompts),
                    parallelism=parallelism,
                    parallel_slots=len(worker_prompts),
                    accepted_candidate_count=len(pool),
                    candidate_target=candidate_target,
                    candidate_status="insufficient_evidence",
                    message="候选研究返回证据不足，未生成推荐",
                )
                return result, report

        generated_values = [raw["generated_at"] for raw in returned]
        generated_at = max(generated_values, key=lambda value: parse_timestamp(value, "generated_at")) if generated_values else None
        for raw in returned:
            batch = raw.get("candidate_pool")
            if not isinstance(batch, list):
                raise ContractError("候选返回数量超过预算或不是数组")
            for original in batch:
                # 缺 track_identity / style 证据属于模型输出不完整：只丢弃该候选，
                # 原因进研究报告供审计。证据本身不可用（矛盾/不可访问/过期）仍然致命，
                # 由下面的 require_usable_evidence 直接报错，不静默丢弃。
                try:
                    _validate_candidate_pool([original], known_refs=set(packet["analysis_ref_ids"]),
                                             known_style_refs=set(packet["style_analysis"]["known_style_refs"]), require_all_types=False)
                except ContractError as exc:
                    report.setdefault("rejected_candidates", []).append({
                        "canonical_track_id": str(original.get("canonical_track_id") or ""),
                        "reason": str(exc),
                    })
                    continue
                require_usable_evidence(original)
                candidate = deepcopy(original)
                candidate["candidate_type"] = resolve_candidate_route(candidate, packet)["candidate_type"]
                if candidate["candidate_type"] != original["candidate_type"]:
                    report["route_corrections"].append({"canonical_track_id": candidate["canonical_track_id"],
                                                       "declared": original["candidate_type"], "resolved": candidate["candidate_type"]})
                try:
                    _validate_candidate_pool([candidate], known_refs=set(packet["analysis_ref_ids"]),
                                             known_style_refs=set(packet["style_analysis"]["known_style_refs"]), require_all_types=False)
                except ContractError as exc:
                    report.setdefault("rejected_candidates", []).append({
                        "canonical_track_id": str(candidate.get("canonical_track_id") or ""),
                        "reason": str(exc),
                    })
                    continue
                identity = candidate["canonical_track_id"].casefold()
                key = track_key(candidate["title"], candidate["artist"])
                platform_id = normalized_text(candidate.get("platform_track_id")).casefold()
                if identity in seen_ids or key in seen_keys or (platform_id and platform_id in seen_platform_ids):
                    report["skipped_duplicates_or_favorites"] += 1
                    continue
                if len(pool) >= max_candidates:
                    report["truncated_candidate_count"] += 1
                    continue
                pool.append(candidate)
                seen_ids.add(identity)
                seen_keys.add(key)
                if platform_id:
                    seen_platform_ids.add(platform_id)
        result = {"schema_version": SCHEMA_VERSION, "bundle_type": "recommendation_bundle", "bundle_stage": "candidate_pool",
                  "status": "ready", "analysis_id": packet["analysis_id"], "generated_at": generated_at or "",
                  "candidate_pool": pool, "recommendations": []}
        if len(pool) >= candidate_target:
            notify(
                "round_completed",
                task_kind="recommendation_round",
                round=round_number,
                round_total=max_rounds,
                worker_total=len(worker_prompts),
                parallelism=parallelism,
                parallel_slots=len(worker_prompts),
                accepted_candidate_count=len(pool),
                candidate_target=candidate_target,
                deficits={},
                candidate_status="ready",
                message=f"第 {round_number} 轮完成，已接收 {len(pool)}/{candidate_target} 个候选",
            )
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
        notify(
            "round_completed",
            task_kind="recommendation_round",
            round=round_number,
            round_total=max_rounds,
            worker_total=len(worker_prompts),
            parallelism=parallelism,
            parallel_slots=len(worker_prompts),
            accepted_candidate_count=len(pool),
            candidate_target=candidate_target,
            deficits=deficits,
            candidate_status="ready" if len(pool) >= candidate_target else "collecting",
            message=f"第 {round_number} 轮完成，已接收 {len(pool)}/{candidate_target} 个候选",
        )
        if len(pool) >= max_candidates or round_number == max_rounds:
            break
        if parallelism == 1:
            request = {"round": round_number + 1, "requested_type_counts": deficits, "constraint_failure": failure,
                       "remaining_candidate_budget": max_candidates - len(pool), "exclude_canonical_ids": sorted(seen_ids),
                       "instruction": "只补充当前缺额或有助于满足约束的新候选；不重复已有候选，不写最终推荐说明。"}
            try:
                prompt = supplemental_prompt(prepared, request, context_budget)
            except ContractError as exc:
                failure = str(exc)
                break
        else:
            requested_counts = deficits
    report.update(status="budget_exhausted", elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
    raise ResearchExhausted(f"候选研究未完成：{failure}；保留研究报告，不输出不足 10 首或降低门槛的推荐", report)
