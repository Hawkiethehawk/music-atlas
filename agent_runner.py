#!/usr/bin/env python3
"""Execute or test the isolated Step 3 Agent boundary.

The runner accepts prompt text on stdin for an external agent command. It does
not know how to access a platform account and never sends a channel message.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_prompt import build_agent_prompt_from_file
from channels import render_for_channel
from contracts import ContractError, read_json, utc_now, validate_analysis_packet, validate_recommendation_bundle, write_json


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
        "schema_version": "1.0",
        "bundle_type": "recommendation_bundle",
        "status": "insufficient_evidence",
        "analysis_id": packet["analysis_id"],
        "generated_at": utc_now(),
        "recommendations": [],
        "message": "本地 mock Agent 未执行公开资料研究；本次只验证 Step 3 契约，不生成真实推荐。",
    }


def run_external_agent(command: str, prompt: str, *, timeout: int) -> dict[str, Any]:
    argv = shlex.split(command, posix=False)
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
        )
    except FileNotFoundError as exc:
        raise ContractError(f"找不到 Agent 命令：{argv[0]}") from exc
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
    channel: str,
    command: str | None,
    mock: bool,
    timeout: int,
) -> dict[str, Any]:
    packet_value = read_json(analysis_path)
    packet = validate_analysis_packet(packet_value)
    prompt = build_agent_prompt_from_file(analysis_path, prompt_path)
    bundle = mock_bundle(packet) if mock else run_external_agent(command or "", prompt, timeout=timeout)
    validate_recommendation_bundle(bundle, packet)
    write_json(output_path, bundle)
    channel_text = render_for_channel(channel, bundle, packet)
    channel_output_path.parent.mkdir(parents=True, exist_ok=True)
    channel_output_path.write_text(channel_text, encoding="utf-8")
    return {
        "status": "agent_bundle_validated",
        "agent_mode": "mock" if mock else "external_command",
        "analysis_id": packet["analysis_id"],
        "source_track_count": packet["source_track_count"],
        "recommendation_status": bundle["status"],
        "recommendation_count": len(bundle["recommendations"]),
        "bundle_path": str(output_path),
        "channel_text_path": str(channel_output_path),
        "send_performed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行隔离 Step 3 Agent 并校验 RecommendationBundle")
    parser.add_argument("--analysis", default="runtime/current-run/musician_analysis.json")
    parser.add_argument("--prompt", default="runtime/current-run/agent_prompt.md")
    parser.add_argument("--output", default="runtime/current-run/recommendation_bundle.json")
    parser.add_argument("--channel-output", default="runtime/current-run/channel_text.txt")
    parser.add_argument("--channel", default="weixin", choices=("weixin", "feishu", "telegram"))
    parser.add_argument("--command", help="读取 stdin 中 prompt 并向 stdout 输出 JSON 的 Agent 命令")
    parser.add_argument("--mock", action="store_true", help="不调用外部模型，只验证无推荐证据不足分支")
    parser.add_argument("--timeout", type=int, default=600)
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

        summary = run_agent(
            resolve(args.analysis),
            prompt_path=resolve(args.prompt),
            output_path=resolve(args.output),
            channel_output_path=resolve(args.channel_output),
            channel=args.channel,
            command=args.command,
            mock=args.mock,
            timeout=max(1, args.timeout),
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(f"Agent 步骤未执行：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
