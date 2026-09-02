#!/usr/bin/env python3
"""Local entry point for the three-stage weekly music workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agent_prompt import build_agent_prompt_from_file, write_agent_context_manifest
from agent_runner import run_agent
from channels import render_for_channel
from contracts import (
    ContractError,
    read_json,
    validate_analysis_packet,
    validate_playlist_snapshot,
    validate_recommendation_file,
    write_json,
)
from musician_analyzer import analyze_and_validate
from source_adapters import build_snapshot, save_snapshot


ROOT = Path(__file__).resolve().parent


def _path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    cwd_candidate = (Path.cwd() / candidate).resolve()
    if cwd_candidate.exists() or cwd_candidate.parent.exists():
        return cwd_candidate
    return ROOT / candidate


def _runtime_paths(runtime_dir: Path) -> dict[str, Path]:
    return {
        "snapshot": runtime_dir / "snapshot.json",
        "analysis": runtime_dir / "musician_analysis.json",
        "analysis_markdown": runtime_dir / "musician_analysis.md",
        "analysis_manifest": runtime_dir / "analysis_manifest.json",
        "agent_prompt": runtime_dir / "agent_prompt.md",
        "agent_context_manifest": runtime_dir / "agent_context_manifest.json",
        "channel_text": runtime_dir / "channel_text.txt",
        "pipeline_manifest": runtime_dir / "pipeline_manifest.json",
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def command_snapshot(args: argparse.Namespace) -> int:
    output_path = _path(args.output, ROOT / "runtime" / "snapshot.json")
    snapshot = build_snapshot(
        _path(args.input, ROOT / "input" / "web_favorites.json"),
        reader_name=args.reader,
        platform=args.platform,
        playlist_id=args.playlist_id,
        playlist_name=args.playlist_name,
        declared_count=args.declared_count,
        declared_count_file=_path(args.declared_count_file, ROOT / "input" / "artist_distribution.json")
        if args.declared_count_file or args.use_default_count_file
        else None,
    )
    save_snapshot(snapshot, output_path)
    _print_summary(
        {
            "status": "snapshot_written",
            "snapshot_path": str(output_path),
            "snapshot_id": snapshot["snapshot_id"],
            "reader_status": snapshot["reader_status"],
            "source_track_count": snapshot["track_count"],
            "declared_track_count": snapshot["declared_track_count"],
        }
    )
    return 0 if snapshot["reader_status"] == "complete" else 2


def command_analyze(args: argparse.Namespace) -> int:
    snapshot_path = _path(args.snapshot, ROOT / "runtime" / "snapshot.json")
    output_path = _path(args.output, ROOT / "runtime" / "musician_analysis.json")
    markdown_path = _path(args.markdown, ROOT / "runtime" / "musician_analysis.md")
    manifest_path = _path(args.manifest, ROOT / "runtime" / "analysis_manifest.json")
    packet = analyze_and_validate(
        snapshot_path,
        preferred_path=_path(args.preferred, ROOT / "preferred_artists.txt"),
        relation_path=_path(args.relations, ROOT / "relations" / "artist_relations.json"),
        output_path=output_path,
        markdown_path=markdown_path,
        manifest_path=manifest_path,
    )
    _print_summary(
        {
            "status": "analysis_written",
            "analysis_path": str(output_path),
            "analysis_id": packet["analysis_id"],
            "source_snapshot_id": packet["source_snapshot_id"],
            "source_track_count": packet["source_track_count"],
            "entity_count": len(packet["entities"]),
            "mapped_entity_count": sum(
                1 for entity in packet["entities"] if entity["relation_status"] == "confirmed"
            ),
        }
    )
    return 0


def command_prepare_agent(args: argparse.Namespace) -> int:
    analysis_path = _path(args.analysis, ROOT / "runtime" / "musician_analysis.json")
    prompt_path = _path(args.output, ROOT / "runtime" / "agent_prompt.md")
    context_manifest_path = _path(
        args.manifest,
        ROOT / "runtime" / "agent_context_manifest.json",
    )
    prompt = build_agent_prompt_from_file(analysis_path, prompt_path)
    packet = validate_analysis_packet(read_json(analysis_path))
    write_agent_context_manifest(packet, context_manifest_path)
    _print_summary(
        {
            "status": "agent_context_written",
            "analysis_id": packet["analysis_id"],
            "source_track_count": packet["source_track_count"],
            "prompt_path": str(prompt_path),
            "prompt_characters": len(prompt),
            "context_manifest_path": str(context_manifest_path),
        }
    )
    return 0


def command_validate(args: argparse.Namespace) -> int:
    analysis_path = _path(args.analysis, ROOT / "runtime" / "musician_analysis.json")
    bundle_path = _path(args.bundle, ROOT / "runtime" / "recommendation_bundle.json")
    packet = validate_analysis_packet(read_json(analysis_path))
    bundle = validate_recommendation_file(bundle_path, analysis_path)
    text = render_for_channel(args.channel, bundle, packet)
    output_path = _path(args.output, ROOT / "runtime" / "channel_text.txt")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    _print_summary(
        {
            "status": "recommendation_validated",
            "analysis_id": packet["analysis_id"],
            "recommendation_status": bundle["status"],
            "recommendation_count": len(bundle["recommendations"]),
            "channel": args.channel,
            "channel_text_path": str(output_path),
        }
    )
    if args.print_text:
        print(text, end="")
    return 0


def command_agent(args: argparse.Namespace) -> int:
    root = ROOT
    summary = run_agent(
        _path(args.analysis, root / "runtime" / "current-run" / "musician_analysis.json"),
        prompt_path=_path(args.prompt, root / "runtime" / "current-run" / "agent_prompt.md"),
        output_path=_path(args.output, root / "runtime" / "current-run" / "recommendation_bundle.json"),
        channel_output_path=_path(
            args.channel_output,
            root / "runtime" / "current-run" / "channel_text.txt",
        ),
        channel=args.channel,
        command=args.command,
        mock=args.mock,
        timeout=max(1, args.timeout),
    )
    _print_summary(summary)
    return 0


def command_run(args: argparse.Namespace) -> int:
    runtime_dir = _path(args.runtime_dir, ROOT / "runtime")
    paths = _runtime_paths(runtime_dir)
    snapshot = build_snapshot(
        _path(args.input, ROOT / "input" / "web_favorites.json"),
        reader_name=args.reader,
        platform=args.platform,
        playlist_id=args.playlist_id,
        playlist_name=args.playlist_name,
        declared_count=args.declared_count,
        declared_count_file=_path(args.declared_count_file, ROOT / "input" / "artist_distribution.json")
        if args.declared_count_file or args.use_default_count_file
        else None,
    )
    save_snapshot(snapshot, paths["snapshot"])
    validate_playlist_snapshot(snapshot, require_complete=True)

    packet = analyze_and_validate(
        paths["snapshot"],
        preferred_path=_path(args.preferred, ROOT / "preferred_artists.txt"),
        relation_path=_path(args.relations, ROOT / "relations" / "artist_relations.json"),
        output_path=paths["analysis"],
        markdown_path=paths["analysis_markdown"],
        manifest_path=paths["analysis_manifest"],
    )
    prompt = build_agent_prompt_from_file(paths["analysis"], paths["agent_prompt"])
    write_agent_context_manifest(packet, paths["agent_context_manifest"])
    write_json(
        paths["pipeline_manifest"],
        {
            "schema_version": "1.0",
            "manifest_type": "local_pipeline_manifest",
            "status": "agent_context_ready",
            "source_snapshot_id": snapshot["snapshot_id"],
            "source_track_count": snapshot["track_count"],
            "analysis_id": packet["analysis_id"],
            "paths": {key: str(value) for key, value in paths.items()},
            "step3": {
                "mode": "agent_required",
                "input": "MusicianAnalysisPacket only",
                "send_performed": False,
            },
        },
    )
    _print_summary(
        {
            "status": "agent_context_ready",
            "source_snapshot_id": snapshot["snapshot_id"],
            "source_track_count": snapshot["track_count"],
            "declared_track_count": snapshot["declared_track_count"],
            "analysis_id": packet["analysis_id"],
            "entity_count": len(packet["entities"]),
            "prompt_characters": len(prompt),
            "runtime_dir": str(runtime_dir),
            "send_performed": False,
        }
    )
    return 0


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", default=None, help="Step 1 原始歌单 JSON/CSV")
    parser.add_argument("--reader", default="local_json", choices=("local_json", "apple_music_json", "netease_json", "csv"))
    parser.add_argument("--platform", default="apple_music")
    parser.add_argument("--playlist-id", default="favorite-songs-web")
    parser.add_argument("--playlist-name", default="喜爱歌曲")
    parser.add_argument("--declared-count", type=int, default=None)
    parser.add_argument("--declared-count-file", default=None)
    parser.add_argument(
        "--use-default-count-file",
        action="store_true",
        help="使用 input/artist_distribution.json 中的本次 Step 1 数量记录",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apple Music 每周推荐本地三阶段工作流")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Step 1: 生成统一 PlaylistSnapshot")
    _add_source_options(snapshot_parser)
    snapshot_parser.add_argument("--output", default=None)
    snapshot_parser.set_defaults(func=command_snapshot)

    analyze_parser = subparsers.add_parser("analyze", help="Step 2: 生成确定性音乐人分析包")
    analyze_parser.add_argument("--snapshot", default=None)
    analyze_parser.add_argument("--preferred", default=None)
    analyze_parser.add_argument("--relations", default=None)
    analyze_parser.add_argument("--output", default=None)
    analyze_parser.add_argument("--markdown", default=None)
    analyze_parser.add_argument("--manifest", default=None)
    analyze_parser.set_defaults(func=command_analyze)

    prompt_parser = subparsers.add_parser("prepare-agent", help="Step 3: 生成隔离 Agent 上下文")
    prompt_parser.add_argument("--analysis", default=None)
    prompt_parser.add_argument("--output", default=None)
    prompt_parser.add_argument("--manifest", default=None)
    prompt_parser.set_defaults(func=command_prepare_agent)

    validate_parser = subparsers.add_parser("validate", help="校验 Agent RecommendationBundle 并渲染渠道文本")
    validate_parser.add_argument("--analysis", default=None)
    validate_parser.add_argument("--bundle", required=True)
    validate_parser.add_argument("--channel", default="weixin", choices=("weixin", "feishu", "telegram"))
    validate_parser.add_argument("--output", default=None)
    validate_parser.add_argument("--print-text", action="store_true")
    validate_parser.set_defaults(func=command_validate)

    agent_parser = subparsers.add_parser("agent", help="Step 3: 执行 Agent 并校验 RecommendationBundle")
    agent_parser.add_argument("--analysis", default=None)
    agent_parser.add_argument("--prompt", default=None)
    agent_parser.add_argument("--output", default=None)
    agent_parser.add_argument("--channel-output", default=None)
    agent_parser.add_argument("--channel", default="weixin", choices=("weixin", "feishu", "telegram"))
    agent_parser.add_argument("--command", help="读取 stdin 中 prompt 并向 stdout 输出 JSON 的 Agent 命令")
    agent_parser.add_argument("--mock", action="store_true")
    agent_parser.add_argument("--timeout", type=int, default=600)
    agent_parser.set_defaults(func=command_agent)

    run_parser = subparsers.add_parser("run", help="本地执行 Step 1 + Step 2 + Step 3 上下文准备")
    _add_source_options(run_parser)
    run_parser.add_argument("--preferred", default=None)
    run_parser.add_argument("--relations", default=None)
    run_parser.add_argument("--runtime-dir", default=None)
    run_parser.set_defaults(func=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ContractError, OSError, ValueError) as exc:
        print(f"音乐工作流未执行：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
