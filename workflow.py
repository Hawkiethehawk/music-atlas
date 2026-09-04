#!/usr/bin/env python3
"""Local entry point for the three-stage weekly music workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agent_prompt import apply_context_budget, build_agent_prompt_from_file, prompt_slot_manifest, prompt_size_telemetry, write_agent_context_manifest
from agent_runner import run_agent
from channels import render_for_channel
from contracts import (
    ContractError,
    read_json,
    utc_now,
    validate_analysis_packet,
    validate_feedback_log,
    validate_feedback_log_refs,
    validate_playlist_snapshot,
    validate_recommendation_bundle,
    validate_recommendation_file,
    write_json,
)
from evidence import audit_bundle_evidence
from evaluation import evaluate_offline
from musician_analyzer import analyze_and_validate, write_coverage_report
from recommender import rank_bundle
from source_adapters import build_snapshot, save_snapshot
from tune import propose_tuning
from visualization_interface import render_recommendation_card


ROOT = Path(__file__).resolve().parent


def _path(value: str | None, default: Path | None) -> Path | None:
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
        style_taxonomy_path=_path(args.style_taxonomy, ROOT / "styles" / "style_taxonomy.json"),
        style_profile_path=_path(args.style_profiles, ROOT / "styles" / "artist_style_profiles.json"),
    )
    coverage_report = None
    if packet["style_analysis"]["profile_coverage"]["degraded"]:
        coverage_report = write_coverage_report(packet, output_path.with_name("coverage_report.json"))
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
            "classified_track_count": packet["style_analysis"]["classified_track_count"],
            "unclassified_track_count": packet["style_analysis"]["unclassified_track_count"],
            "artist_profile_count": packet["style_analysis"]["artist_profile_count"],
            "profile_catalog_mode": packet["style_analysis"]["profile_catalog_mode"],
            "profile_coverage_degraded": packet["style_analysis"]["profile_coverage"]["degraded"],
            "coverage_report_path": coverage_report
            and str(output_path.with_name("coverage_report.json")),
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
    prompt_dir = _path(args.prompt_dir, ROOT / "prompts")
    prompt = build_agent_prompt_from_file(analysis_path, prompt_path, prompt_dir=prompt_dir)
    prompt, budget_report = apply_context_budget(prompt, args.context_budget)
    prompt_path.write_text(prompt, encoding="utf-8")
    packet = validate_analysis_packet(read_json(analysis_path))
    write_agent_context_manifest(
        packet,
        context_manifest_path,
        prompt_dir=prompt_dir,
        prompt=prompt,
        context_budget=args.context_budget,
        budget_report=budget_report,
    )
    _print_summary(
        {
            "status": "agent_context_written",
            "analysis_id": packet["analysis_id"],
            "source_track_count": packet["source_track_count"],
            "prompt_path": str(prompt_path),
            "prompt_characters": len(prompt),
            "estimated_tokens": prompt_size_telemetry(prompt)["estimated_tokens"],
            "context_budget": args.context_budget,
            "budget_exceeded": budget_report.get("budget_exceeded"),
            "truncated_slots": budget_report.get("truncated_slots", []),
            "prompt_slot_count": len(prompt_slot_manifest(prompt_dir)),
            "context_manifest_path": str(context_manifest_path),
        }
    )
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    """Compare a ranked bundle against recorded feedback (read-only)."""

    analysis_path = _path(args.analysis, ROOT / "runtime" / "musician_analysis.json")
    bundle_path = _path(args.bundle, ROOT / "runtime" / "recommendation_bundle.ranked.json")
    feedback_path = _path(args.feedback, ROOT / "runtime" / "feedback_log.json")
    packet = validate_analysis_packet(read_json(analysis_path))
    bundle = validate_recommendation_file(bundle_path, analysis_path)
    feedback_log = validate_feedback_log(read_json(feedback_path))
    report = evaluate_offline(bundle, packet, feedback_log)
    output_path = _path(args.output, bundle_path.with_name("evaluation_report.json"))
    write_json(output_path, report)
    output = {
        "status": "offline_evaluation_written",
        "analysis_id": packet["analysis_id"],
        "report_path": str(output_path),
        "policy_changed": report["policy_changed"],
        "feedback": {
            "record_count": report["feedback"]["record_count"],
            "matched_count": report["feedback"]["matched_count"],
            "unmatched_count": report["feedback"]["unmatched_count"],
        },
        "precision": report["precision"]["precision"],
        "acceptance_rate": report["precision"]["acceptance_rate"],
        "novel_artist_share": report["novelty"]["novel_artist_share"],
        "artist_diversity": report["diversity"]["artist_diversity"],
        "expected_calibration_error": report["calibration"]["expected_calibration_error"],
        "max_artist_share": report["repetition"]["max_artist_share"],
        "arc_conformance": report["sequence_quality"]["arc_conformance"],
    }
    _print_summary(output)
    return 0


def command_tune(args: argparse.Namespace) -> int:
    """Propose a human-approved policy tuning artifact; never apply it."""

    analysis_path = _path(args.analysis, ROOT / "runtime" / "musician_analysis.json")
    bundle_path = _path(args.bundle, ROOT / "runtime" / "recommendation_bundle.ranked.json")
    feedback_path = _path(args.feedback, ROOT / "runtime" / "feedback_log.json")
    packet = validate_analysis_packet(read_json(analysis_path))
    bundle = validate_recommendation_file(bundle_path, analysis_path)
    feedback_log = validate_feedback_log(read_json(feedback_path))
    report = evaluate_offline(bundle, packet, feedback_log)
    feedback_by_track = {}
    for record in feedback_log:
        feedback_by_track[str(record.get("recommendation_id") or "").casefold()] = record["outcome"]
    proposal = propose_tuning(report, packet, bundle=bundle, feedback_by_track=feedback_by_track)
    output_path = _path(args.output, bundle_path.with_name("tuning_proposal.json"))
    write_json(output_path, proposal)
    _print_summary(
        {
            "status": "tuning_proposal_written",
            "analysis_id": packet["analysis_id"],
            "proposal_path": str(output_path),
            "approval_required": proposal["approval_required"],
            "auto_applied": proposal["auto_applied"],
            "suggested_weight_deltas": proposal["suggested_deltas"]["ranking_weights"],
            "suggested_caps": proposal["suggested_caps"],
        }
    )
    return 0


def command_validate(args: argparse.Namespace) -> int:
    analysis_path = _path(args.analysis, ROOT / "runtime" / "musician_analysis.json")
    bundle_path = _path(args.bundle, ROOT / "runtime" / "recommendation_bundle.json")
    packet = validate_analysis_packet(read_json(analysis_path))
    bundle = validate_recommendation_file(bundle_path, analysis_path)
    bundle = rank_bundle(bundle, packet)
    validate_recommendation_bundle(bundle, packet)
    ranked_output_path = _path(
        args.ranked_output,
        bundle_path.with_name("recommendation_bundle.ranked.json"),
    )
    write_json(ranked_output_path, bundle)
    text = render_for_channel(args.channel, bundle, packet)
    output_path = _path(args.output, ROOT / "runtime" / "channel_text.txt")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    evidence_audit = None
    if args.evidence_audit is not None:
        audit_path = _path(args.evidence_audit, bundle_path.with_name("evidence_audit.json"))
        evidence_audit = audit_bundle_evidence(bundle)
        write_json(audit_path, evidence_audit)
    image_summary = None
    if args.image_output is not None:
        image_path = _path(args.image_output, output_path.parent / "recommendation_card.png")
        if image_path is None:
            raise ContractError("可视化输出路径不能为空")
        image_summary = render_recommendation_card(bundle, packet, image_path)
    _print_summary(
        {
            "status": "recommendation_validated",
            "analysis_id": packet["analysis_id"],
            "recommendation_status": bundle["status"],
            "recommendation_count": len(bundle["recommendations"]),
            "ranked_bundle_path": str(ranked_output_path),
            "channel": args.channel,
            "channel_text_path": str(output_path),
            "evidence_audit_path": str(audit_path) if evidence_audit else None,
            "evidence_accepted_count": evidence_audit and evidence_audit["accepted_count"],
            "evidence_rejected_count": evidence_audit and evidence_audit["rejected_count"],
            "channel_image_path": image_summary["path"] if image_summary else None,
            "channel_image_size": {
                "width": image_summary["width"],
                "height": image_summary["height"],
            }
            if image_summary
            else None,
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
        channel_image_path=_path(args.image_output, None),
        prompt_dir=_path(args.prompt_dir, root / "prompts"),
        channel=args.channel,
        command=args.command,
        mock=args.mock,
        timeout=max(1, args.timeout),
        context_budget=args.context_budget,
    )
    _print_summary(summary)
    return 0


def command_archive_schema1(args: argparse.Namespace) -> int:
    """Archive Schema 1 runtime artifacts under ``runtime/archive``.

    Runtime artifacts are git-ignored local outputs. Schema 1 bundles and
    analysis packets cannot be validated or ranked by the current Schema 2
    pipeline, so they are moved (not deleted) into a timestamped archive
    directory and a manifest is written. Current artifacts must be
    regenerated before a live Schema 2 run.
    """

    import shutil
    from datetime import datetime, timezone

    runtime_dir = _path(args.runtime_dir, ROOT / "runtime")
    archive_root = runtime_dir / "archive"
    archive_root_existed = archive_root.exists()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = archive_root / f"schema1-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, str]] = []
    for directory in sorted(item for item in runtime_dir.iterdir() if item.is_dir()):
        if directory.name == "archive":
            continue
        payloads = list(directory.glob("*.json"))
        if not payloads:
            continue
        versions = set()
        for path in payloads:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                versions.add(str(value.get("schema_version") or ""))
        if versions == {"1.0"}:
            destination = target / directory.name
            destination.mkdir(parents=True, exist_ok=True)
            for path in sorted(directory.iterdir()):
                if path.is_file():
                    shutil.move(str(path), str(destination / path.name))
            moved.append({"source": directory.name, "schema_versions": sorted(versions)})
            if not any(directory.iterdir()):
                directory.rmdir()
    if not moved:
        if not any(target.iterdir()):
            target.rmdir()
        if not archive_root_existed and not any(archive_root.iterdir()):
            archive_root.rmdir()
        _print_summary(
            {
                "status": "schema1_archive_skipped",
                "archive_dir": str(archive_root),
                "archived_run_count": 0,
                "archived_runs": [],
                "manifest_path": None,
                "regenerate_required": False,
                "note": "没有发现 Schema 1.0 运行时产物，未创建归档目录",
            }
        )
        return 0
    manifest = {
        "schema_version": "2.0",
        "manifest_type": "schema1_archive_manifest",
        "archived_at": utc_now(),
        "archive_dir": str(target),
        "archived_runs": moved,
        "note": "Schema 1 运行时产物已归档；实时运行前需重新执行 run 生成 Schema 2 工件",
    }
    manifest_path = target / "archive_manifest.json"
    write_json(manifest_path, manifest)
    _print_summary(
        {
            "status": "schema1_archive_written",
            "archive_dir": str(target),
            "archived_run_count": len(moved),
            "archived_runs": [item["source"] for item in moved],
            "manifest_path": str(manifest_path),
            "regenerate_required": True,
        }
    )
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
        style_taxonomy_path=_path(args.style_taxonomy, ROOT / "styles" / "style_taxonomy.json"),
        style_profile_path=_path(args.style_profiles, ROOT / "styles" / "artist_style_profiles.json"),
    )
    coverage_report = None
    if packet["style_analysis"]["profile_coverage"]["degraded"]:
        coverage_report = write_coverage_report(packet, runtime_dir / "coverage_report.json")
    prompt_dir = _path(args.prompt_dir, ROOT / "prompts")
    prompt = build_agent_prompt_from_file(paths["analysis"], paths["agent_prompt"], prompt_dir=prompt_dir)
    prompt, budget_report = apply_context_budget(prompt, args.context_budget)
    prompt_path = paths["agent_prompt"]
    prompt_path.write_text(prompt, encoding="utf-8")
    write_agent_context_manifest(
        packet,
        paths["agent_context_manifest"],
        prompt_dir=prompt_dir,
        prompt=prompt,
        context_budget=args.context_budget,
        budget_report=budget_report,
    )
    write_json(
        paths["pipeline_manifest"],
        {
            "schema_version": "2.0",
            "manifest_type": "local_pipeline_manifest",
            "status": "agent_context_ready",
            "source_snapshot_id": snapshot["snapshot_id"],
            "source_track_count": snapshot["track_count"],
            "analysis_id": packet["analysis_id"],
            "paths": {key: str(value) for key, value in paths.items()},
            "prompt_size": prompt_size_telemetry(prompt),
            "context_budget": args.context_budget,
            "budget_report": budget_report,
            "step3": {
                "mode": "agent_required",
                "input": "MusicianAnalysisPacket only",
                "algorithm": packet["recommendation_policy"].get("algorithm_version", "hybrid_music_discovery_v2"),
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
            "artist_profile_count": packet["style_analysis"]["artist_profile_count"],
            "classified_track_count": packet["style_analysis"]["classified_track_count"],
            "unclassified_track_count": packet["style_analysis"]["unclassified_track_count"],
            "profile_catalog_mode": packet["style_analysis"]["profile_catalog_mode"],
            "profile_coverage_degraded": packet["style_analysis"]["profile_coverage"]["degraded"],
            "coverage_report_path": str(runtime_dir / "coverage_report.json") if coverage_report else None,
            "prompt_characters": len(prompt),
            "estimated_tokens": prompt_size_telemetry(prompt)["estimated_tokens"],
            "context_budget": args.context_budget,
            "budget_exceeded": budget_report.get("budget_exceeded"),
            "truncated_slots": budget_report.get("truncated_slots", []),
            "runtime_dir": str(runtime_dir),
            "send_performed": False,
        }
    )
    return 0


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", default=None, help="Step 1 原始歌单 JSON/CSV")
    parser.add_argument(
        "--reader",
        default="local_json",
        choices=("local_json", "apple_music_json", "netease_json", "netease_public", "csv"),
    )
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


def _add_style_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--style-taxonomy",
        default=None,
        help="可复用的风格本体 JSON",
    )
    parser.add_argument(
        "--style-profiles",
        default=None,
        help="逐艺人风格画像 JSON；默认使用本地私有目录",
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
    _add_style_options(analyze_parser)
    analyze_parser.set_defaults(func=command_analyze)

    prompt_parser = subparsers.add_parser("prepare-agent", help="Step 3: 生成隔离 Agent 上下文")
    prompt_parser.add_argument("--analysis", default=None)
    prompt_parser.add_argument("--output", default=None)
    prompt_parser.add_argument("--manifest", default=None)
    prompt_parser.add_argument("--prompt-dir", default=None, help="可编辑提示词插槽目录")
    prompt_parser.add_argument("--context-budget", type=int, default=None, help="Agent 提示词字符预算")
    prompt_parser.set_defaults(func=command_prepare_agent)

    validate_parser = subparsers.add_parser("validate", help="校验 Agent RecommendationBundle 并渲染渠道文本")
    validate_parser.add_argument("--analysis", default=None)
    validate_parser.add_argument("--bundle", required=True)
    validate_parser.add_argument("--channel", default="weixin", choices=("weixin", "feishu", "telegram"))
    validate_parser.add_argument("--output", default=None)
    validate_parser.add_argument("--ranked-output", default=None)
    validate_parser.add_argument(
        "--image-output",
        default=None,
        help="预留可视化输出接口；当前未配置渲染后端",
    )
    validate_parser.add_argument("--print-text", action="store_true")
    validate_parser.add_argument("--evidence-audit", default=None, help="输出证据离线审计报告路径")
    validate_parser.set_defaults(func=command_validate)

    evaluate_parser = subparsers.add_parser("evaluate", help="将已排序 bundle 与记录的反馈进行只读离线评估")
    evaluate_parser.add_argument("--analysis", default=None)
    evaluate_parser.add_argument("--bundle", required=True)
    evaluate_parser.add_argument("--feedback", required=True)
    evaluate_parser.add_argument("--output", default=None)
    evaluate_parser.set_defaults(func=command_evaluate)

    tune_parser = subparsers.add_parser("tune", help="生成需人工批准的策略调优建议，绝不自动应用")
    tune_parser.add_argument("--analysis", default=None)
    tune_parser.add_argument("--bundle", required=True)
    tune_parser.add_argument("--feedback", required=True)
    tune_parser.add_argument("--output", default=None)
    tune_parser.set_defaults(func=command_tune)

    agent_parser = subparsers.add_parser("agent", help="Step 3: 执行 Agent 并校验 RecommendationBundle")
    agent_parser.add_argument("--analysis", default=None)
    agent_parser.add_argument("--prompt", default=None)
    agent_parser.add_argument("--output", default=None)
    agent_parser.add_argument("--channel-output", default=None)
    agent_parser.add_argument(
        "--image-output",
        default=None,
        help="预留可视化输出接口；当前未配置渲染后端",
    )
    agent_parser.add_argument("--channel", default="weixin", choices=("weixin", "feishu", "telegram"))
    agent_parser.add_argument("--command", help="读取 stdin 中 prompt 并向 stdout 输出 JSON 的 Agent 命令")
    agent_parser.add_argument("--mock", action="store_true")
    agent_parser.add_argument("--timeout", type=int, default=600)
    agent_parser.add_argument("--prompt-dir", default=None, help="可编辑提示词插槽目录")
    agent_parser.add_argument("--context-budget", type=int, default=None, help="Agent 提示词字符预算")
    agent_parser.set_defaults(func=command_agent)

    run_parser = subparsers.add_parser("run", help="本地执行 Step 1 + Step 2 + Step 3 上下文准备")
    _add_source_options(run_parser)
    run_parser.add_argument("--preferred", default=None)
    run_parser.add_argument("--relations", default=None)
    run_parser.add_argument("--runtime-dir", default=None)
    run_parser.add_argument("--prompt-dir", default=None, help="可编辑提示词插槽目录")
    run_parser.add_argument("--context-budget", type=int, default=None, help="Agent 提示词字符预算")
    _add_style_options(run_parser)
    run_parser.set_defaults(func=command_run)

    archive_parser = subparsers.add_parser("archive-schema1", help="将历史 Schema 1 运行时产物迁移到 runtime/archive")
    archive_parser.add_argument("--runtime-dir", default=None)
    archive_parser.set_defaults(func=command_archive_schema1)
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
