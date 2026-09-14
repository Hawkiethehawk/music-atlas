#!/usr/bin/env python3
"""Local entry point for the three-stage music playlist workflow (on demand)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from proc_util import hidden_window_kwargs
from agent_prompt import prepare_agent_context, prompt_slot_manifest, prompt_size_telemetry
from agent_runner import run_agent
from analysis_agent import execute_analysis_research, prepare_analysis_research
from taste_summary import TASTE_BATCH_SIZE, resolve_analysis_mode, run_taste_analysis, summarize_review
from reports import render_report
from benchmark import compare_listening_benchmark, prepare_listening_benchmark
from contracts import (
    ContractError,
    parse_as_of_date,
    read_json,
    utc_now,
    validate_analysis_packet,
    validate_feedback_log,
    validate_playlist_snapshot,
    validate_recommendation_bundle,
    validate_recommendation_file,
    write_json,
)
from evidence import audit_bundle_evidence
from evaluation import evaluate_offline
from feedback import latest_feedback_outcomes
from musician_analyzer import analyze_and_validate, load_recommendation_policy, write_coverage_report
from recommender import rank_bundle
from source_adapters import build_snapshot, save_snapshot
from skill_runner import run_skill
from tune import propose_tuning
from web_view_model import export_web_payload


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
        "report": runtime_dir / "report.txt",
        "pipeline_manifest": runtime_dir / "pipeline_manifest.json",
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _uses_analysis_skill(mode: str) -> bool:
    """Return whether a mode uses the provider-neutral research Skill.

    ``agent`` remains an accepted legacy spelling during migration.
    """

    return mode in {"skill", "agent"}


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


def _analysis_research_input(args: argparse.Namespace, snapshot_path: Path, output_path: Path) -> tuple[Path | None, dict | None, dict | None]:
    """按歌单规模分档：≤30 首逐曲研究（返回 research_path），31+ 首品味摘要（返回 packet）。"""
    # Reject configuration errors before making any external Skill call.
    load_recommendation_policy(_path(args.policy_file, None))
    if args.as_of_date is not None:
        parse_as_of_date(args.as_of_date)
    taxonomy_path = _path(args.style_taxonomy, ROOT / "styles" / "style_taxonomy.json")
    research_path = _path(args.research_bundle, None)
    inputs = [snapshot_path, taxonomy_path, *([research_path] if research_path else [])]
    outputs = [output_path, _path(getattr(args, "markdown", None), output_path.with_name("musician_analysis.md")),
               _path(getattr(args, "manifest", None), output_path.with_name("analysis_manifest.json")),
               output_path.with_name("coverage_report.json")]
    inputs.extend(_path(value, None) for value in (args.policy_file, args.preferred, args.style_profiles, args.relations) if value)
    if len({target.resolve() for target in outputs}) != len(outputs):
        raise ContractError("分析输出路径不能相互覆盖")
    if any(target.resolve() == source.resolve() for target in outputs for source in inputs):
        raise ContractError("分析输出不能覆盖快照、词表、策略或研究包等输入")
    if args.analysis_mode == "catalog":
        if args.analysis_command or args.research_bundle or args.import_analysis_results:
            raise ContractError("catalog 模式不能同时使用 Skill 研究参数")
        return None, None, None
    if args.style_profiles or args.relations or args.preferred:
        raise ContractError("Skill 分析不读取预置画像、关系目录或偏好名单；离线兼容请显式指定 --analysis-mode catalog")
    if research_path is not None:
        return research_path, None, None
    directory = _path(args.analysis_research_dir, output_path.parent / "analysis_research")
    if any(path.resolve().is_relative_to(directory.resolve()) for path in (*inputs, *outputs)):
        raise ContractError("分析研究目录不能包含分析输入或输出文件")
    # 规模分档：31+ 首的 Skill 分析改用品味/歌手摘要单任务，不再准备逐曲批次。
    analysis_scale_mode = resolve_analysis_mode(read_json(snapshot_path)["track_count"])
    if args.import_analysis_results:
        if analysis_scale_mode != "track_research":
            raise ContractError("歌单超过 30 首，应使用品味摘要模式（--analysis-command），不支持分批结果导入")
        result = execute_analysis_research(snapshot_path, taxonomy_path, directory, command=args.analysis_command,
                                           batch_size=args.analysis_batch_size, context_budget=args.analysis_context_budget,
                                           timeout=args.analysis_timeout,
                                           parallelism=args.analysis_parallelism)
        return result, None, None
    if analysis_scale_mode != "track_research":
        if args.analysis_command is None:
            return None, {
                "status": "taste_analysis_required", "analysis_mode": analysis_scale_mode,
                "source_track_count": read_json(snapshot_path)["track_count"],
                "analysis_written": False, "recommendation_count": 0, "send_performed": False,
                "next_action": "歌单超过 30 首：提供 --analysis-command 以运行品味摘要分析（单任务）。",
            }, None
        packet, _bundle_path = run_taste_analysis(
            snapshot_path, taxonomy_path, directory, command=args.analysis_command,
            timeout=args.analysis_timeout, policy_path=_path(args.policy_file, None))
        return None, {"status": "taste_analysis_written"}, packet
    if args.analysis_command:
        result = execute_analysis_research(snapshot_path, taxonomy_path, directory, command=args.analysis_command,
                                           batch_size=args.analysis_batch_size or TASTE_BATCH_SIZE,
                                           context_budget=args.analysis_context_budget,
                                           timeout=args.analysis_timeout,
                                           parallelism=args.analysis_parallelism)
        return result, None, None
    manifest = prepare_analysis_research(snapshot_path, taxonomy_path, directory,
                                         batch_size=args.analysis_batch_size or TASTE_BATCH_SIZE, context_budget=args.analysis_context_budget)
    return None, {
        "status": "analysis_agent_required", "analysis_mode": args.analysis_mode, "skill_name": "music-atlas-analysis",
        "executor_kind": "generic_external_executor", "source_snapshot_id": manifest["source_snapshot_id"],
        "source_track_count": manifest["source_track_count"], "research_batch_count": len(manifest["batches"]),
        "analysis_parallelism": args.analysis_parallelism,
        "analysis_research_dir": str(directory), "research_manifest_path": str(directory / "manifest.json"),
        "analysis_written": False, "recommendation_count": 0, "send_performed": False,
        "next_action": "使用任意模型或工具执行 Skill：提供 --analysis-command，或完成各批 result 文件后用 --import-analysis-results 汇总；不要重新抓取快照。",
    }, None

def command_analyze(args: argparse.Namespace) -> int:
    snapshot_path = _path(args.snapshot, ROOT / "runtime" / "snapshot.json")
    output_path = _path(args.output, ROOT / "runtime" / "musician_analysis.json")
    markdown_path = _path(args.markdown, output_path.with_name("musician_analysis.md"))
    manifest_path = _path(args.manifest, output_path.with_name("analysis_manifest.json"))
    research_path, pending, prepared_packet = _analysis_research_input(args, snapshot_path, output_path)
    if pending is not None:
        if pending.get("status") == "taste_analysis_required":
            _print_summary(pending)
            return 2
        if pending.get("status") != "taste_analysis_written":
            _print_summary(pending)
            return 0
    if prepared_packet is not None:
        # 品味/歌手摘要模式：分析包已生成，写盘与锐评摘要，不走逐曲聚合。
        packet = prepared_packet
        write_json(output_path, packet)
        review = summarize_review(packet["taste_summary"])
        output_path.with_name("musician_analysis.md").write_text(
            "# 品味摘要\n\n## {headline}\n\n{review}\n\n## 内心世界\n\n{inner}\n\n"
            "- 局限：{limits}\n".format(
                headline=review["headline"], review=review["review"], inner=review["inner_world"],
                limits="；".join(review["limitations"]) or "无"),
            encoding="utf-8")
    else:
        packet = analyze_and_validate(
            snapshot_path,
            preferred_path=_path(args.preferred, ROOT / "preferred_artists.txt"),
            relation_path=_path(args.relations, ROOT / "relations" / "artist_relations.json"),
            output_path=output_path,
            markdown_path=markdown_path,
            manifest_path=manifest_path,
            style_taxonomy_path=_path(args.style_taxonomy, ROOT / "styles" / "style_taxonomy.json"),
            style_profile_path=_path(args.style_profiles, ROOT / "styles" / "artist_style_profiles.json"),
            policy_path=_path(args.policy_file, None),
            as_of_date=args.as_of_date,
            research_bundle_path=research_path,
        )
    coverage_report = None
    if packet["style_analysis"]["profile_coverage"]["degraded"]:
        coverage_report = write_coverage_report(packet, output_path.with_name("coverage_report.json"))
    _print_summary(
        {
            "status": "analysis_written",
            "analysis_path": str(output_path),
            "analysis_id": packet["analysis_id"],
            "as_of_date": packet["as_of_date"],
            "source_snapshot_id": packet["source_snapshot_id"],
            "source_track_count": packet["source_track_count"],
            "entity_count": len(packet["entities"]),
            "mapped_entity_count": sum(
                1 for entity in packet["entities"] if entity["relation_status"] in {"confirmed", "researched"}
            ),
            "classified_track_count": packet["style_analysis"]["classified_track_count"],
            "unclassified_track_count": packet["style_analysis"]["unclassified_track_count"],
            "artist_profile_count": packet["style_analysis"]["artist_profile_count"],
            "profile_catalog_mode": packet["style_analysis"]["profile_catalog_mode"],
            "analysis_mode": args.analysis_mode,
            "analysis_parallelism": args.analysis_parallelism,
            "research_bundle_path": str(research_path) if research_path else None,
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
        prompt_path.with_name("agent_context_manifest.json"),
    )
    prompt_dir = _path(args.prompt_dir, ROOT / "prompts")
    packet = validate_analysis_packet(read_json(analysis_path))
    prompt, budget_report = prepare_agent_context(
        packet, prompt_path, manifest_path=context_manifest_path,
        prompt_dir=prompt_dir, context_budget=args.context_budget,
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


def command_benchmark(args: argparse.Namespace) -> int:
    packet_a = validate_analysis_packet(read_json(_path(args.analysis_a, None)))
    packet_b = validate_analysis_packet(read_json(_path(args.analysis_b or args.analysis_a, None)))
    bundle_a, bundle_b = read_json(_path(args.bundle_a, None)), read_json(_path(args.bundle_b, None))
    if args.command == "prepare-benchmark":
        report = prepare_listening_benchmark(packet_a, bundle_a, packet_b, bundle_b)
    else:
        report = compare_listening_benchmark(packet_a, bundle_a, packet_b, bundle_b, read_json(_path(args.judgments, None)),
                                            report_a=read_json(_path(args.research_report_a, None)) if args.research_report_a else None,
                                            report_b=read_json(_path(args.research_report_b, None)) if args.research_report_b else None)
    output = _path(args.output, None)
    inputs = [args.analysis_a, args.analysis_b or args.analysis_a, args.bundle_a, args.bundle_b]
    inputs += [getattr(args, name, None) for name in ("judgments", "research_report_a", "research_report_b")]
    if any(output.resolve() == _path(value, None).resolve() for value in inputs if value):
        raise ContractError("对照输出路径不得覆盖分析包、推荐、研究报告或人工标注")
    if args.command == "prepare-benchmark" and output.exists():
        raise ContractError("试听标注文件已存在；请使用新路径，避免覆盖人工标注")
    write_json(output, report)
    _print_summary({"status": report.get("status", "listening_template_written"), "output": str(output), "policy_changed": False})
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
    bundle = rank_bundle(read_json(bundle_path), packet)
    feedback_log = validate_feedback_log(read_json(feedback_path))
    report = evaluate_offline(bundle, packet, feedback_log)
    feedback_by_track = latest_feedback_outcomes(bundle, feedback_log)
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
            "proposal_status": proposal["status"],
            "feedback_sample": proposal["feedback_sample"],
            "suggested_weight_deltas": proposal["suggested_deltas"]["ranking_weights"],
            "suggested_caps": proposal["suggested_caps"],
        }
    )
    return 0


def command_validate(args: argparse.Namespace) -> int:
    analysis_path = _path(args.analysis, ROOT / "runtime" / "musician_analysis.json")
    bundle_path = _path(args.bundle, ROOT / "runtime" / "recommendation_bundle.json")
    audit_path = _path(args.evidence_audit, None)
    try:
        packet = validate_analysis_packet(read_json(analysis_path))
        bundle = rank_bundle(read_json(bundle_path), packet)
        validate_recommendation_bundle(bundle, packet)
        evidence_audit = audit_bundle_evidence(bundle) if audit_path is not None else None
    except ContractError as exc:
        if audit_path is not None:
            write_json(audit_path, {
                "schema_version": "2.0", "artifact_type": "evidence_audit",
                "status": "invalid_contract", "error": str(exc), "outputs_written": False,
            })
        raise
    if evidence_audit is not None:
        write_json(audit_path, evidence_audit)
        if evidence_audit["status"] not in {"accepted", "not_applicable"}:
            _print_summary({
                "status": "evidence_audit_failed", "analysis_id": packet["analysis_id"],
                "evidence_audit_path": str(audit_path), "audit_status": evidence_audit["status"],
                "evidence_accepted_count": evidence_audit["accepted_count"],
                "evidence_rejected_count": evidence_audit["rejected_count"],
                "evidence_pending_count": evidence_audit["pending_count"], "outputs_written": False,
            })
            return 2
    ranked_output_path = _path(
        args.ranked_output,
        bundle_path.with_name("recommendation_bundle.ranked.json"),
    )
    text = render_report(bundle, packet)
    output_path = _path(args.output, ROOT / "runtime" / "report.txt")
    write_json(ranked_output_path, bundle)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    _print_summary(
        {
            "status": "recommendation_validated",
            "analysis_id": packet["analysis_id"],
            "recommendation_status": bundle["status"],
            "publication_status": bundle.get("publication_status", "not_applicable"),
            "recommendation_count": len(bundle["recommendations"]),
            "ranked_bundle_path": str(ranked_output_path),
            "report_path": str(output_path),
            "evidence_audit_path": str(audit_path) if evidence_audit else None,
            "evidence_accepted_count": evidence_audit and evidence_audit["accepted_count"],
            "evidence_rejected_count": evidence_audit and evidence_audit["rejected_count"],
        }
    )
    if args.print_text:
        print(text, end="")
    return 0


def _run_step3(args: argparse.Namespace, runner: Any) -> dict[str, Any]:
    root = ROOT
    return runner(
        _path(args.analysis, root / "runtime" / "current-run" / "musician_analysis.json"),
        prompt_path=_path(args.prompt, root / "runtime" / "current-run" / "agent_prompt.md"),
        output_path=_path(args.output, root / "runtime" / "current-run" / "recommendation_bundle.json"),
        report_output_path=_path(
            args.report_output,
            root / "runtime" / "current-run" / "report.txt",
        ),
        prompt_dir=_path(args.prompt_dir, None),
        command=args.command,
        mock=args.mock,
        timeout=max(1, args.timeout),
        context_budget=args.context_budget,
        context_manifest_path=_path(args.manifest, None),
        max_research_rounds=args.max_research_rounds,
        candidate_target=args.candidate_target,
        max_candidates=args.max_candidates,
        recommendation_parallelism=args.recommendation_parallelism,
    )


def command_agent(args: argparse.Namespace) -> int:
    summary = _run_step3(args, run_agent)
    _print_summary(summary)
    return 0


def command_skill(args: argparse.Namespace) -> int:
    summary = _run_step3(args, run_skill)
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
    research_dir = _path(args.analysis_research_dir, runtime_dir / "analysis_research")
    if _uses_analysis_skill(args.analysis_mode) and paths["snapshot"].exists() and (research_dir / "manifest.json").exists():
        raise ContractError("当前快照已绑定分析研究任务；请用 analyze --snapshot 继续，或用新的 runtime-dir 开始新任务")
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

    research_path, pending, prepared_packet = _analysis_research_input(args, paths["snapshot"], paths["analysis"])
    if pending is not None:
        if pending.get("status") == "taste_analysis_required":
            write_json(paths["pipeline_manifest"], {
                "schema_version": "2.0", "manifest_type": "local_pipeline_manifest", **pending,
                "snapshot_path": str(paths["snapshot"]), "policy": {"file": args.policy_file},
            })
            _print_summary({**pending, "runtime_dir": str(runtime_dir)})
            return 2
        if pending.get("status") != "taste_analysis_written":
            write_json(paths["pipeline_manifest"], {
                "schema_version": "2.0", "manifest_type": "local_pipeline_manifest", **pending,
                "snapshot_path": str(paths["snapshot"]), "policy": {"file": args.policy_file},
            })
            _print_summary({**pending, "runtime_dir": str(runtime_dir)})
            return 0
    if prepared_packet is not None:
        # 品味/歌手摘要模式：分析包已生成，直接写盘并进入推荐准备。
        packet = prepared_packet
        write_json(paths["analysis"], packet)
    else:
        packet = analyze_and_validate(
            paths["snapshot"],
            preferred_path=_path(args.preferred, ROOT / "preferred_artists.txt"),
            relation_path=_path(args.relations, ROOT / "relations" / "artist_relations.json"),
            output_path=paths["analysis"],
            markdown_path=paths["analysis_markdown"],
            manifest_path=paths["analysis_manifest"],
            style_taxonomy_path=_path(args.style_taxonomy, ROOT / "styles" / "style_taxonomy.json"),
            style_profile_path=_path(args.style_profiles, ROOT / "styles" / "artist_style_profiles.json"),
            policy_path=_path(args.policy_file, None),
            as_of_date=args.as_of_date,
            research_bundle_path=research_path,
        )
    coverage_report = None
    if packet["style_analysis"]["profile_coverage"]["degraded"]:
        coverage_report = write_coverage_report(packet, runtime_dir / "coverage_report.json")
    prompt_dir = _path(args.prompt_dir, ROOT / "prompts")
    prompt, budget_report = prepare_agent_context(
        packet, paths["agent_prompt"], manifest_path=paths["agent_context_manifest"],
        prompt_dir=prompt_dir, context_budget=args.context_budget,
    )
    write_json(
        paths["pipeline_manifest"],
        {
            "schema_version": "2.0",
            "manifest_type": "local_pipeline_manifest",
            "status": "agent_context_ready",
            "skill_name": "music-atlas-recommendation",
            "executor_kind": "generic_external_executor",
            "analysis_mode": args.analysis_mode,
            "analysis_parallelism": args.analysis_parallelism,
            "research_bundle_path": str(research_path) if research_path else None,
            "as_of_date": packet["as_of_date"],
            "source_snapshot_id": snapshot["snapshot_id"],
            "source_track_count": snapshot["track_count"],
            "analysis_id": packet["analysis_id"],
            "paths": {key: str(value) for key, value in paths.items()},
            "prompt_size": prompt_size_telemetry(prompt),
            "context_budget": args.context_budget,
            "budget_report": budget_report,
            "policy": packet["input_manifest"]["policy"],
            "step3": {
                "mode": "skill_required",
                "skill_name": "music-atlas-recommendation",
                "executor_kind": "generic_external_executor",
                "parallelism": args.recommendation_parallelism,
                "input": "MusicianAnalysisPacket only",
                "algorithm": packet["recommendation_policy"].get("algorithm_version", "hybrid_music_discovery_v2"),
                "send_performed": False,
            },
        },
    )
    _print_summary(
        {
            "status": "agent_context_ready",
            "skill_name": "music-atlas-recommendation",
            "executor_kind": "generic_external_executor",
            "analysis_mode": args.analysis_mode,
            "research_bundle_path": str(research_path) if research_path else None,
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


def command_web_export(args: argparse.Namespace) -> int:
    """Export a validated runtime as a read-only web view model."""

    runtime_dir = _path(args.runtime_dir, ROOT / "runtime")
    if runtime_dir is None:
        raise ContractError("网页导出运行目录不能为空")
    output_path = _path(args.output, runtime_dir / "web_payload.json")
    if output_path is None:
        raise ContractError("网页导出输出路径不能为空")
    summary = export_web_payload(
        runtime_dir,
        output_path,
        snapshot_path=_path(args.snapshot, None),
        analysis_path=_path(args.analysis, None),
        bundle_path=_path(args.bundle, None),
        evidence_audit_path=_path(args.evidence_audit, None),
        editorial_path=_path(args.editorial, None),
        require_publishable=args.require_publishable,
    )
    _print_summary(summary)
    return 0


def export_apple_playlist_file(url: str, output_path: Path, expected_count: int | None = None) -> dict[str, Any]:
    """Export a shared Apple Music playlist to CSV via TuneMyMusic (no login)."""

    import shutil
    import subprocess

    tool_path = ROOT / "tools" / "export_apple_playlist.mjs"
    if not tool_path.is_file():
        raise ContractError(f"找不到导出工具：{tool_path}")
    node_executable = shutil.which("node")
    if node_executable is None:
        raise ContractError(
            "未找到 node；请先安装 Node.js，并在 tools/ 目录执行 "
            "`npm install playwright`（见 tools/README.md）"
        )
    output_path = output_path.resolve()
    if expected_count is not None and not 0 < expected_count <= 9007199254740991:
        raise ContractError("expected-count 必须是正整数且不超过 JavaScript 安全整数范围")
    command = [node_executable, str(tool_path), url, str(output_path)]
    if expected_count is not None:
        command.append(str(expected_count))
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   timeout=600, **hidden_window_kwargs())
    except subprocess.TimeoutExpired as exc:
        raise ContractError("Apple 歌单导出超时：600 秒") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")[:500]
        raise ContractError(f"Apple 歌单导出失败：{detail}")
    import json as json_module

    try:
        summary = json_module.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ContractError(f"导出工具输出无法解析：{completed.stdout[:200]}") from exc
    summary["output"] = str(output_path)
    return summary


def command_export_apple_playlist(args: argparse.Namespace) -> int:
    summary = export_apple_playlist_file(
        args.url,
        _path(args.output, ROOT / "input" / "apple_favorite_songs.csv"),
        args.expected_count,
    )
    _print_summary(summary)
    return 0


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", default=None, help="Step 1 原始歌单 JSON/CSV")
    parser.add_argument(
        "--reader",
        default="local_json",
        choices=("local_json", "apple_music_json", "netease_json", "netease_public", "qq_public", "csv"),
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
        help="仅 catalog 模式使用的逐艺人风格画像 JSON；该模式默认使用本地私有目录",
    )


def _add_analysis_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--analysis-mode", choices=("skill", "agent", "catalog"), default="skill",
                        help="默认由通用 Skill 研究画像；agent 为兼容别名，catalog 为显式本地目录兼容模式")
    research = parser.add_mutually_exclusive_group()
    research.add_argument("--analysis-command", "--analysis-skill-command", dest="analysis_command",
                          help="分析 Skill 执行器命令，stdin 接收研究请求，stdout 返回研究 JSON；不绑定模型")
    research.add_argument("--research-bundle", help="导入绑定当前快照的完整 MusicianResearchBundle")
    research.add_argument("--import-analysis-results", action="store_true", help="校验并汇总当前研究目录中已完成的各批 result 文件")
    parser.add_argument("--analysis-research-dir", help="本次分析研究目录，默认在分析输出目录下的 analysis_research")
    parser.add_argument("--analysis-batch-size", type=int, default=None, help="每批最多曲目数，1 到 50；继承准备配置，新任务默认 20")
    parser.add_argument("--analysis-context-budget", type=int, default=None, help="分析 Skill 单批字符硬预算；继承准备配置，新任务默认 100000")
    parser.add_argument("--analysis-timeout", type=int, default=600, help="所有分析研究批次共用的秒数预算，默认 600")
    parser.add_argument("--analysis-parallelism", type=int, default=5, help="Step 2 同时执行的分析任务数，默认 5")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="歌单推荐本地三阶段工作流：随时手动触发，不绑定时间")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Step 1: 生成统一 PlaylistSnapshot")
    _add_source_options(snapshot_parser)
    snapshot_parser.add_argument("--output", default=None)
    snapshot_parser.set_defaults(func=command_snapshot)

    analyze_parser = subparsers.add_parser("analyze", help="Step 2: Skill 研究音乐事实，程序聚合分析包")
    analyze_parser.add_argument("--snapshot", default=None)
    analyze_parser.add_argument("--preferred", default=None)
    analyze_parser.add_argument("--relations", default=None)
    analyze_parser.add_argument("--output", default=None)
    analyze_parser.add_argument("--markdown", default=None)
    analyze_parser.add_argument("--manifest", default=None)
    analyze_parser.add_argument("--policy-file", default=None, help="显式加载人工审阅的策略 JSON 部分覆盖")
    analyze_parser.add_argument("--as-of-date", default=None, help="评分基准 YYYY-MM-DD；默认快照的 UTC 日期")
    _add_style_options(analyze_parser)
    _add_analysis_options(analyze_parser)
    analyze_parser.set_defaults(func=command_analyze)

    prompt_parser = subparsers.add_parser("prepare-agent", help="Step 3: 生成隔离 Skill 上下文（兼容命令名）")
    prompt_parser.add_argument("--analysis", default=None)
    prompt_parser.add_argument("--output", default=None)
    prompt_parser.add_argument("--manifest", default=None)
    prompt_parser.add_argument("--prompt-dir", default=None, help="可编辑提示词插槽目录")
    prompt_parser.add_argument("--context-budget", type=int, default=None, help="Skill 提示词字符预算")
    prompt_parser.set_defaults(func=command_prepare_agent)

    skill_prompt_parser = subparsers.add_parser("prepare-skill", help="Step 3: 生成隔离 Skill 上下文")
    skill_prompt_parser.add_argument("--analysis", default=None)
    skill_prompt_parser.add_argument("--output", default=None)
    skill_prompt_parser.add_argument("--manifest", default=None)
    skill_prompt_parser.add_argument("--prompt-dir", default=None, help="可编辑提示词插槽目录")
    skill_prompt_parser.add_argument("--context-budget", type=int, default=None, help="Skill 提示词字符预算")
    skill_prompt_parser.set_defaults(func=command_prepare_agent)

    validate_parser = subparsers.add_parser("validate", help="校验 Skill RecommendationBundle 并生成内部文本报告")
    validate_parser.add_argument("--analysis", default=None)
    validate_parser.add_argument("--bundle", required=True)
    validate_parser.add_argument("--output", default=None)
    validate_parser.add_argument("--ranked-output", default=None)
    validate_parser.add_argument("--print-text", action="store_true")
    validate_parser.add_argument("--evidence-audit", default=None, help="输出证据离线审计报告路径")
    validate_parser.set_defaults(func=command_validate)

    web_parser = subparsers.add_parser(
        "web-export",
        help="校验当前运行并导出 Editorial Atlas 的只读网页视图模型",
    )
    web_parser.add_argument("--runtime-dir", default=None, help="运行产物目录，默认 runtime")
    web_parser.add_argument("--snapshot", default=None, help="可选的 PlaylistSnapshot 路径")
    web_parser.add_argument("--analysis", default=None, help="可选的 MusicianAnalysisPacket 路径")
    web_parser.add_argument("--bundle", default=None, help="可选的 RecommendationBundle 路径")
    web_parser.add_argument("--evidence-audit", default=None, help="可选的 evidence_audit.json 路径")
    web_parser.add_argument("--editorial", default=None, help="可选的人工网页 editorial 配置 JSON")
    web_parser.add_argument("--output", default=None, help="网页视图模型输出路径")
    web_parser.add_argument(
        "--require-publishable",
        action="store_true",
        help="仅允许正式可发布状态；默认允许导出研究草稿供本机预览",
    )
    web_parser.set_defaults(func=command_web_export)

    evaluate_parser = subparsers.add_parser("evaluate", help="将已排序 bundle 与记录的反馈进行只读离线评估")
    evaluate_parser.add_argument("--analysis", default=None)
    evaluate_parser.add_argument("--bundle", required=True)
    evaluate_parser.add_argument("--feedback", required=True)
    evaluate_parser.add_argument("--output", default=None)
    evaluate_parser.set_defaults(func=command_evaluate)

    for name in ("prepare-benchmark", "benchmark"):
        comparison_parser = subparsers.add_parser(name, help="生成盲测标注清单" if name == "prepare-benchmark" else "同输入的只读试听方案对照")
        comparison_parser.add_argument("--analysis-a", required=True)
        comparison_parser.add_argument("--analysis-b", default=None)
        comparison_parser.add_argument("--bundle-a", required=True)
        comparison_parser.add_argument("--bundle-b", required=True)
        comparison_parser.add_argument("--output", required=True)
        if name == "benchmark":
            comparison_parser.add_argument("--judgments", required=True)
            comparison_parser.add_argument("--research-report-a", default=None)
            comparison_parser.add_argument("--research-report-b", default=None)
        comparison_parser.set_defaults(func=command_benchmark)

    tune_parser = subparsers.add_parser("tune", help="生成需人工批准的策略调优建议，绝不自动应用")
    tune_parser.add_argument("--analysis", default=None)
    tune_parser.add_argument("--bundle", required=True)
    tune_parser.add_argument("--feedback", required=True)
    tune_parser.add_argument("--output", default=None)
    tune_parser.set_defaults(func=command_tune)

    agent_parser = subparsers.add_parser("agent", help="Step 3: 执行旧 Agent 兼容入口并校验 RecommendationBundle")
    agent_parser.add_argument("--analysis", default=None)
    agent_parser.add_argument("--prompt", default=None)
    agent_parser.add_argument("--manifest", default=None, help="准备阶段的 context manifest 路径")
    agent_parser.add_argument("--output", default=None)
    agent_parser.add_argument("--report-output", default=None)
    agent_parser.add_argument("--command", "--skill-command", dest="command",
                              help="读取 stdin 中任务并向 stdout 输出 JSON 的通用执行器命令；不绑定模型")
    agent_parser.add_argument("--mock", action="store_true")
    agent_parser.add_argument("--timeout", type=int, default=600)
    agent_parser.add_argument("--prompt-dir", default=None, help="可编辑提示词插槽目录")
    agent_parser.add_argument("--context-budget", type=int, default=None, help="Agent 提示词字符预算")
    agent_parser.add_argument("--max-research-rounds", type=int, default=2, help="含首轮，最多 3 轮；timeout 为研究总预算")
    agent_parser.add_argument("--candidate-target", type=int, default=None, help="目标候选数，默认使用策略最小值")
    agent_parser.add_argument("--max-candidates", type=int, default=80, help="本次候选数上限，最大 200")
    agent_parser.add_argument("--recommendation-parallelism", type=int, choices=tuple(range(1, 9)), default=4,
                              help="Step 3 同时执行的候选研究任务数，默认 4；允许 1 到 8")
    agent_parser.set_defaults(func=command_agent)

    skill_parser = subparsers.add_parser("skill", help="Step 3: 执行通用 Recommendation Skill 并校验结果")
    skill_parser.add_argument("--analysis", default=None)
    skill_parser.add_argument("--prompt", default=None)
    skill_parser.add_argument("--manifest", default=None, help="准备阶段的 context manifest 路径")
    skill_parser.add_argument("--output", default=None)
    skill_parser.add_argument("--report-output", default=None)
    skill_parser.add_argument("--command", "--skill-command", dest="command",
                              help="读取 stdin 中任务并向 stdout 输出 JSON 的通用执行器命令；不绑定模型")
    skill_parser.add_argument("--mock", action="store_true")
    skill_parser.add_argument("--timeout", type=int, default=600)
    skill_parser.add_argument("--prompt-dir", default=None, help="可编辑提示词插槽目录")
    skill_parser.add_argument("--context-budget", type=int, default=None, help="Skill 提示词字符预算")
    skill_parser.add_argument("--max-research-rounds", type=int, default=2, help="含首轮，最多 3 轮；timeout 为研究总预算")
    skill_parser.add_argument("--candidate-target", type=int, default=None, help="目标候选数，默认使用策略最小值")
    skill_parser.add_argument("--max-candidates", type=int, default=80, help="本次候选数上限，最大 200")
    skill_parser.add_argument("--recommendation-parallelism", type=int, choices=tuple(range(1, 9)), default=4,
                              help="Step 3 同时执行的候选研究任务数，默认 4；允许 1 到 8")
    skill_parser.set_defaults(func=command_skill)

    run_parser = subparsers.add_parser("run", help="快照 + 分析 Skill 研究 + 程序聚合 + 推荐上下文准备；未配置 Skill 时停在研究准备")
    _add_source_options(run_parser)
    run_parser.add_argument("--preferred", default=None)
    run_parser.add_argument("--relations", default=None)
    run_parser.add_argument("--runtime-dir", default=None)
    run_parser.add_argument("--policy-file", default=None, help="显式加载人工审阅的策略 JSON 部分覆盖")
    run_parser.add_argument("--as-of-date", default=None, help="评分基准 YYYY-MM-DD；默认快照的 UTC 日期")
    run_parser.add_argument("--prompt-dir", default=None, help="可编辑提示词插槽目录")
    run_parser.add_argument("--context-budget", type=int, default=None, help="Agent 提示词字符预算")
    run_parser.add_argument("--recommendation-parallelism", type=int, choices=tuple(range(1, 9)), default=4,
                            help="Step 3 同时执行的候选研究任务数，默认 4；允许 1 到 8")
    _add_style_options(run_parser)
    _add_analysis_options(run_parser)
    run_parser.set_defaults(func=command_run)

    archive_parser = subparsers.add_parser("archive-schema1", help="将历史 Schema 1 运行时产物迁移到 runtime/archive")
    archive_parser.add_argument("--runtime-dir", default=None)
    archive_parser.set_defaults(func=command_archive_schema1)

    export_parser = subparsers.add_parser(
        "export-apple-playlist",
        help="经 TuneMyMusic 免登录导出 Apple Music 公开分享歌单为 CSV",
    )
    export_parser.add_argument("--url", required=True, help="Apple Music 歌单分享链接")
    export_parser.add_argument("--output", default=None, help="CSV 输出路径（默认 input/apple_favorite_songs.csv）")
    export_parser.add_argument("--expected-count", type=int, default=None, help="从源歌单独立确认的歌曲总数；缺失时完整性未确认")
    export_parser.set_defaults(func=command_export_apple_playlist)
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
