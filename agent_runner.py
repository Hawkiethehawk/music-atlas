#!/usr/bin/env python3
"""Execute or test the isolated Step 3 Skill boundary.

The runner accepts task text on stdin for a provider-neutral external Skill
executor. It does not know how to access a platform account and never sends a
channel message. The old Agent names remain as compatibility aliases.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agent_prompt import (
    load_or_prepare_agent_context,
    prompt_size_telemetry,
)
from channels import render_for_channel
from contracts import ContractError, read_json, stable_hash, utc_now, validate_analysis_packet, validate_recommendation_bundle, write_json
from recommender import rank_bundle
from research import ResearchFailure, research_candidates
from visualization_interface import render_recommendation_card


def parse_agent_json(output: str) -> dict[str, Any]:
    text = output.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"Agent 输出不是合法 JSON：第 {exc.lineno} 行") from exc
    if not isinstance(value, dict):
        raise ContractError("Agent 输出必须是 JSON 对象")
    return value


def mock_bundle(packet: dict[str, Any]) -> dict[str, Any]:
    """Return a no-send contract fixture without inventing music facts."""

    return {
        "schema_version": "2.0",
        "bundle_type": "recommendation_bundle",
        "bundle_stage": "final",
        "status": "insufficient_evidence",
        "analysis_id": packet["analysis_id"],
        "generated_at": utc_now(),
        "recommendations": [],
        "message": "本地 mock Skill 未执行公开资料研究；本次只验证 Step 3 契约，不生成真实推荐。",
    }


def run_external_agent(command: str, prompt: str, *, timeout: int) -> dict[str, Any]:
    # Windows CreateProcess consumes its native command line; shlex is POSIX-only.
    try:
        argv = command.strip() if os.name == "nt" else shlex.split(command, posix=True)
    except ValueError as exc:
        raise ContractError(f"无法解析 Agent 命令：{exc}") from exc
    if not argv:
        raise ContractError("Agent 命令不能为空")
    child_environment = os.environ.copy()
    child_environment["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            argv,
            input=prompt,
            text=True,
            encoding="utf-8",
            env=child_environment,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ContractError(f"找不到 Agent 命令：{command}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContractError(f"Agent 执行超时：{timeout} 秒") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")[:500]
        raise ContractError(f"Agent 返回码为 {completed.returncode}：{detail}")
    return parse_agent_json(completed.stdout)


def run_agent(
    analysis_path: Path,
    *,
    prompt_path: Path,
    output_path: Path,
    channel_output_path: Path,
    channel_image_path: Path | None = None,
    prompt_dir: Path | None = None,
    channel: str,
    command: str | None,
    mock: bool,
    timeout: int,
    context_budget: int | None = None,
    context_manifest_path: Path | None = None,
    max_research_rounds: int = 2,
    candidate_target: int | None = None,
    max_candidates: int = 80,
) -> dict[str, Any]:
    started = time.perf_counter()
    packet_value = read_json(analysis_path)
    packet = validate_analysis_packet(packet_value)
    candidate_target = packet["recommendation_policy"]["candidate_pool_min"] if candidate_target is None else candidate_target
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (max_research_rounds, candidate_target, max_candidates)):
        raise ContractError("研究轮数和候选预算必须是整数")
    if not 1 <= max_research_rounds <= 3 or not packet["recommendation_policy"]["candidate_pool_min"] <= candidate_target <= max_candidates <= 200:
        raise ContractError("研究轮数须为 1 到 3；候选目标不得低于策略最小值，候选上限不得超过 200")
    prompt, context_manifest = load_or_prepare_agent_context(
        packet, prompt_path, manifest_path=context_manifest_path,
        prompt_dir=prompt_dir, context_budget=context_budget,
    )
    context_budget = context_manifest["run_config"]["context_budget"]
    budget_report = context_manifest["budget_report"]
    prepared_ms = round((time.perf_counter() - started) * 1000, 2)
    report_path = output_path.with_suffix(".research.json")
    if mock:
        bundle = rank_bundle(mock_bundle(packet), packet)
        research_report = {"schema_version": "2.0", "artifact_type": "research_report", "analysis_id": packet["analysis_id"],
                           "status": "mock", "rounds": [], "policy_changed": False}
    else:
        try:
            bundle, research_report = research_candidates(
                packet, prompt, command or "", execute=run_external_skill, timeout=timeout,
                context_budget=context_budget, max_rounds=max_research_rounds,
                candidate_target=candidate_target, max_candidates=max_candidates,
            )
        except ResearchFailure as exc:
            exc.report.update(preparation_ms=prepared_ms, total_elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                              context_budget=context_budget, research_timeout_seconds=timeout,
                              input_characters_total=sum(item["input_characters"] for item in exc.report["rounds"]),
                              final_explanations_generated=0)
            write_json(report_path, exc.report)
            raise
    validate_recommendation_bundle(bundle, packet)
    channel_text = render_for_channel(channel, bundle, packet)
    image_summary = None
    if channel_image_path is not None:
        image_summary = render_recommendation_card(bundle, packet, channel_image_path)
    write_json(output_path, bundle)
    channel_output_path.parent.mkdir(parents=True, exist_ok=True)
    channel_output_path.write_text(channel_text, encoding="utf-8")
    research_report.update(preparation_ms=prepared_ms, total_elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                           ranked_bundle_sha256=stable_hash(bundle),
                           context_budget=context_budget, research_timeout_seconds=timeout,
                           input_characters_total=sum(item["input_characters"] for item in research_report["rounds"]),
                           final_explanations_generated=len(bundle["recommendations"]))
    write_json(report_path, research_report)
    return {
        "status": "agent_bundle_validated",
        "agent_mode": "mock" if mock else "external_command",
        "analysis_id": packet["analysis_id"],
        "source_track_count": packet["source_track_count"],
        "recommendation_status": bundle["status"],
        "publication_status": bundle.get("publication_status", "not_applicable"),
        "recommendation_count": len(bundle["recommendations"]),
        "ranking_applied": bool(bundle.get("ranking")),
        "research_rounds": len(research_report["rounds"]),
        "research_report_path": str(report_path),
        "prompt_characters": len(prompt),
        "estimated_tokens": prompt_size_telemetry(prompt)["estimated_tokens"],
        "context_budget": context_budget,
        "budget_exceeded": budget_report.get("budget_exceeded"),
        "bundle_path": str(output_path),
        "channel_text_path": str(channel_output_path),
        "channel_image_path": image_summary["path"] if image_summary else None,
        "channel_image_size": {
            "width": image_summary["width"],
            "height": image_summary["height"],
        }
        if image_summary
        else None,
        "send_performed": False,
    }


def parse_skill_json(output: str) -> dict[str, Any]:
    """Parse one JSON object returned by any Skill executor.

    This is intentionally model- and provider-neutral. The legacy parser name
    remains available because existing integrations import it directly.
    """

    return parse_agent_json(output)


def run_external_skill(command: str, prompt: str, *, timeout: int) -> dict[str, Any]:
    """Run a generic Skill executor without selecting an AI model.

    The executable, model, tools, retrieval provider and credentials all stay
    outside Music Atlas. The only runtime contract is stdin prompt -> stdout
    JSON object.
    """

    return run_external_agent(command, prompt, timeout=timeout)


def run_skill(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run the Step 3 Skill boundary while preserving the old API."""

    summary = dict(run_agent(*args, **kwargs))
    summary["status"] = "skill_bundle_validated"
    summary["skill_mode"] = "mock" if kwargs.get("mock") else "external_command"
    summary["skill_name"] = "music-atlas-recommendation"
    summary["executor_kind"] = "mock" if kwargs.get("mock") else "generic_external_executor"
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行隔离 Step 3 Agent 并校验 RecommendationBundle")
    parser.add_argument("--analysis", default="runtime/current-run/musician_analysis.json")
    parser.add_argument("--prompt", default="runtime/current-run/agent_prompt.md")
    parser.add_argument("--output", default="runtime/current-run/recommendation_bundle.json")
    parser.add_argument("--channel-output", default="runtime/current-run/channel_text.txt")
    parser.add_argument(
        "--image-output",
        default=None,
        help="预留可视化输出接口；当前未配置渲染后端",
    )
    parser.add_argument("--prompt-dir", default=None, help="可编辑提示词插槽目录；默认继承准备配置")
    parser.add_argument("--manifest", default=None, help="准备阶段的 context manifest 路径")
    parser.add_argument("--channel", default="weixin", choices=("weixin", "feishu", "telegram"))
    parser.add_argument("--command", help="读取 stdin 中 prompt 并向 stdout 输出 JSON 的 Agent 命令")
    parser.add_argument("--mock", action="store_true", help="不调用外部模型，只验证无推荐证据不足分支")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--context-budget", type=int, default=None, help="Agent 提示词字符预算")
    parser.add_argument("--max-research-rounds", type=int, default=2, help="含首轮，最多 3 轮；--timeout 为研究总预算")
    parser.add_argument("--candidate-target", type=int, default=None, help="目标候选数，默认使用策略最小值")
    parser.add_argument("--max-candidates", type=int, default=80, help="本次研究的候选数量上限，最大 200")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        root = Path(__file__).resolve().parent

        def resolve(value: str) -> Path:
            candidate = Path(value).expanduser()
            if candidate.is_absolute():
                return candidate
            cwd_candidate = (Path.cwd() / candidate).resolve()
            if cwd_candidate.exists() or cwd_candidate.parent.exists():
                return cwd_candidate
            return root / candidate

        image_output = resolve(args.image_output) if args.image_output else None
        summary = run_agent(
            resolve(args.analysis),
            prompt_path=resolve(args.prompt),
            output_path=resolve(args.output),
            channel_output_path=resolve(args.channel_output),
            channel_image_path=image_output,
            prompt_dir=resolve(args.prompt_dir) if args.prompt_dir else None,
            channel=args.channel,
            command=args.command,
            mock=args.mock,
            timeout=max(1, args.timeout),
            context_budget=args.context_budget,
            context_manifest_path=resolve(args.manifest) if args.manifest else None,
            max_research_rounds=args.max_research_rounds,
            candidate_target=args.candidate_target,
            max_candidates=args.max_candidates,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(f"Agent 步骤未执行：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
