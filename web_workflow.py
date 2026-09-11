"""Run the guarded Music Atlas workflow for the local web API.

The browser only supplies a public playlist URL.  External Skill executors
remain server configuration, and every stage still passes through the same
JSON contracts as the CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from analysis_agent import execute_analysis_research
from agent_prompt import prepare_agent_context, prompt_size_telemetry
from contracts import ContractError, read_json, utc_now, validate_playlist_snapshot, write_json
from musician_analyzer import analyze_and_validate, write_coverage_report
from source_adapters import build_snapshot, save_snapshot
from workflow import ROOT, export_apple_playlist_file
from skill_runner import run_skill
from taste_summary import TASTE_BATCH_SIZE, resolve_analysis_mode, run_taste_analysis
from web_view_model import export_web_payload


SOURCE_KINDS = {"apple_music", "netease_public", "qq_public", "local_json", "csv"}
PUBLIC_HOSTS = {
    "apple_music": {"music.apple.com"},
    "netease_public": {"music.163.com", "163cn.tv", "www.163cn.tv"},
    "qq_public": {"y.qq.com", "i.y.qq.com"},
}
NETEASE_SHORT_HOSTS = {"163cn.tv", "www.163cn.tv"}
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


def _validate_url(kind: str, value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or hostname not in PUBLIC_HOSTS[kind]:
        raise ContractError(f"{kind} 只接受受支持平台的 HTTPS 公开链接")
    return url


def _netease_playlist_id_from_url(value: str) -> str:
    parsed = urlparse(value)
    for query in (parsed.query, parsed.fragment.lstrip("#/")):
        playlist_id = parse_qs(query).get("id", [""])[0].strip()
        if playlist_id.isdigit():
            return playlist_id
    for pattern in (r"/playlist/(\d+)", r"[?&#]id=(\d+)"):
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    raise ContractError(f"无法从网易云公开链接解析歌单 ID：{value}")


def _resolve_netease_source(url: str) -> tuple[str, str]:
    """Resolve a music.163.com URL or 163cn.tv short link to a playlist id."""

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if hostname == "music.163.com":
        return _netease_playlist_id_from_url(url), url
    if hostname not in NETEASE_SHORT_HOSTS:
        raise ContractError("网易云公开链接域名不受支持")
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://music.163.com/",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            resolved_url = response.geturl()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ContractError(f"网易云短链解析失败：{exc}") from exc
    resolved_parsed = urlparse(resolved_url)
    resolved_host = (resolved_parsed.hostname or "").casefold().rstrip(".")
    if resolved_parsed.scheme != "https" or resolved_host != "music.163.com":
        raise ContractError("网易云短链未跳转到受支持的网易云歌单页面")
    playlist_id = _netease_playlist_id_from_url(resolved_url)
    # Do not persist redirect tracking parameters such as userid/app_version
    # in job events or runtime reports.
    return playlist_id, f"https://music.163.com/playlist?id={playlist_id}"


def _source_id(kind: str, value: str) -> str:
    return f"{kind}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


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


def _apply_track_limit(snapshot: dict[str, Any], limit: int) -> dict[str, Any]:
    """Keep only the first ``limit`` tracks as a self-consistent snapshot.

    The web panel size control trims the playlist only *after* Step 1 has read
    it, because the operator must not exceed the real track count. The Step 1
    contract still requires ``declared_track_count == track_count ==
    len(tracks)``, so the trimmed result stands on its own: the pre-trim source
    total stays on ``reader.source_track_count`` for traceability and the
    snapshot id is suffixed so trimmed and untrimmed runs never share analysis
    caches.
    """

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
    if limit >= total:
        reader["requested_track_limit"] = None
        return snapshot
    trimmed: list[dict[str, Any]] = []
    for position, track in enumerate(tracks[:limit], 1):
        if not isinstance(track, dict):
            raise ContractError(f"第 {position} 首歌曲不是对象")
        trimmed.append({**track, "position": position})
    reader["requested_track_limit"] = limit
    snapshot["tracks"] = trimmed
    snapshot["track_count"] = limit
    snapshot["declared_track_count"] = limit
    base_id = re.sub(r"-limit\d+$", "", str(snapshot.get("snapshot_id") or ""))
    snapshot["snapshot_id"] = f"{base_id}-limit{limit}"
    return snapshot


def _read_limit_request(path: Path, maximum: int) -> int:
    """Parse one operator-submitted track limit request."""

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
    return value


def _await_track_limit(request_path: Path, maximum: int, timeout: int) -> int:
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
                limit = _read_limit_request(request_path, maximum)
            except ContractError as exc:
                request_path.unlink(missing_ok=True)
                emit("limit_rejected", status="awaiting_limit", stage="snapshot",
                     track_count=maximum, error=str(exc))
            else:
                return limit
        if time.monotonic() >= deadline:
            raise ContractError(f"等待选择处理数量超时（{timeout} 秒），本次任务未继续")
        time.sleep(LIMIT_WAIT_POLL_SECONDS)


def _build_source(args: argparse.Namespace, runtime_dir: Path) -> tuple[dict, dict]:
    kind = args.source_kind
    playlist_name = (args.playlist_name or "").strip()
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
            playlist_name=playlist_name or "Apple Music 公开歌单",
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


def run_web_workflow(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).resolve()
    if runtime_dir.exists() and any(runtime_dir.iterdir()):
        raise ContractError(f"网页任务运行目录必须为空：{runtime_dir}")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    analysis_command = (args.analysis_command or "").strip()
    recommendation_command = (args.recommendation_command or "").strip()
    if not analysis_command or not recommendation_command:
        raise ContractError("网页工作流未配置分析或推荐 Skill 执行器")

    emit("started", status="running", stage="snapshot", source_kind=args.source_kind,
         analysis_parallelism=args.analysis_parallelism,
         recommendation_parallelism=args.recommendation_parallelism)
    emit("task_started", status="running", stage="snapshot", task_kind="snapshot_import",
         task_id="snapshot-import", task_index=1, task_total=1, task_status="running",
         message="正在识别平台并拉取歌单")
    snapshot, source_report = _build_source(args, runtime_dir)
    snapshot_path = runtime_dir / "snapshot.json"
    save_snapshot(snapshot, snapshot_path)
    validate_playlist_snapshot(snapshot, require_complete=True)
    emit("completed", status="running", stage="snapshot", track_count=snapshot["track_count"],
         snapshot_id=snapshot["snapshot_id"], source=source_report,
         completeness_status=(snapshot.get("reader") or {}).get("completeness_status"))
    emit("task_completed", status="running", stage="snapshot", task_kind="snapshot_import",
         task_id="snapshot-import", task_index=1, task_total=1, task_status="validated",
         completed=1, total=1, track_completed=snapshot["track_count"],
         track_total=snapshot["track_count"], message=f"歌单已整理 · {snapshot['track_count']} 首")

    # 网页端档位选择：歌单读取完成后暂停，等操作者提交处理数量再截断。
    # 只有读到了曲目才谈得上上限，因此空歌单不走等待，交给既有校验报错。
    source_track_count = snapshot["track_count"]
    await_limit = bool(getattr(args, "await_track_limit", False))
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
        limit = _await_track_limit(request_path, source_track_count, limit_timeout)
        request_path.unlink(missing_ok=True)
        snapshot = _apply_track_limit(snapshot, limit)
        validate_playlist_snapshot(snapshot, require_complete=True)
        save_snapshot(snapshot, snapshot_path)
        emit("limit_applied", status="running", stage="snapshot",
             requested_track_limit=limit, source_track_count=source_track_count,
             track_count=snapshot["track_count"], snapshot_id=snapshot["snapshot_id"],
             message=f"已确定处理数量 · 前 {snapshot['track_count']} 首")
        emit("task_completed", status="running", stage="snapshot", task_kind="track_limit",
             task_id="track-limit", task_index=1, task_total=1, task_status="validated",
             completed=1, total=1, track_count=snapshot["track_count"],
             message=f"处理数量已确定 · 前 {snapshot['track_count']} 首")

    taxonomy_path = (ROOT / "styles" / "style_taxonomy.json").resolve()
    analysis_research_dir = runtime_dir / "analysis_research"
    # 按歌单规模选择分析分辨率：≤30 首逐曲研究（每批默认 10 首），
    # 31-500 首品味摘要，≥501 首歌手摘要；后两者为单任务分析。
    analysis_mode = resolve_analysis_mode(snapshot["track_count"])
    emit("started", status="running", stage="analysis", parallelism=args.analysis_parallelism,
         analysis_mode=analysis_mode,
         message="Step 2：逐曲并行研究，全部完成后才进入 Step 3" if analysis_mode == "track_research"
         else "Step 2：按歌单规模采用整体品味分析（单任务）")
    if analysis_mode == "track_research":
        research_bundle_path = execute_analysis_research(
            snapshot_path,
            taxonomy_path,
            analysis_research_dir,
            command=analysis_command,
            batch_size=args.analysis_batch_size or TASTE_BATCH_SIZE,
            context_budget=args.analysis_context_budget,
            timeout=args.analysis_timeout,
            parallelism=args.analysis_parallelism,
            progress=emit_progress,
        )
        analysis_path = runtime_dir / "musician_analysis.json"
        analysis_markdown_path = runtime_dir / "musician_analysis.md"
        analysis_manifest_path = runtime_dir / "analysis_manifest.json"
        emit("task_started", status="running", stage="analysis", task_kind="analysis_aggregate",
             task_id="analysis-aggregate", task_index=1, task_total=1, task_status="running",
             message="所有批次已返回，正在合并兴趣、关系与覆盖率")
        packet = analyze_and_validate(
            snapshot_path,
            preferred_path=ROOT / "preferred_artists.txt",
            relation_path=ROOT / "relations" / "artist_relations.json",
            output_path=analysis_path,
            markdown_path=analysis_markdown_path,
            manifest_path=analysis_manifest_path,
            style_taxonomy_path=taxonomy_path,
            style_profile_path=ROOT / "styles" / "artist_style_profiles.json",
            research_bundle_path=research_bundle_path,
        )
    else:
        analysis_path = runtime_dir / "musician_analysis.json"
        packet, _ = run_taste_analysis(
            snapshot_path,
            taxonomy_path,
            analysis_research_dir,
            command=analysis_command,
            timeout=args.analysis_timeout,
            progress=emit_progress,
        )
        write_json(analysis_path, packet)
    coverage_report_path: Path | None = None
    if packet["style_analysis"]["profile_coverage"]["degraded"]:
        write_coverage_report(packet, runtime_dir / "coverage_report.json")
        coverage_report_path = runtime_dir / "coverage_report.json"
    emit("completed", status="running", stage="analysis", analysis_id=packet["analysis_id"],
         classified_track_count=packet["style_analysis"]["classified_track_count"],
         source_track_count=packet["source_track_count"],
         analysis_mode=analysis_mode,
         parallelism=args.analysis_parallelism,
         coverage_report_path=str(coverage_report_path) if coverage_report_path else None)
    emit("task_completed", status="running", stage="analysis", task_kind="analysis_aggregate",
         task_id="analysis-aggregate", task_index=1, task_total=1, task_status="validated",
         completed=1, total=1, track_completed=packet["source_track_count"],
         track_total=packet["source_track_count"],
         classified_track_count=packet["style_analysis"]["classified_track_count"],
         message="分析包已校验，允许进入推荐阶段")

    prompt_path = runtime_dir / "agent_prompt.md"
    context_manifest_path = runtime_dir / "agent_context_manifest.json"
    emit("started", status="running", stage="recommendation", parallelism=args.recommendation_parallelism,
         message="Step 3：分析包校验通过，启动候选研究并行任务")
    emit("task_started", status="running", stage="recommendation", task_kind="context_prepare",
         task_id="recommendation-context", task_index=1, task_total=1, task_status="running",
         message="正在准备候选研究上下文")
    prompt, budget_report = prepare_agent_context(
        packet,
        prompt_path,
        manifest_path=context_manifest_path,
        prompt_dir=ROOT / "prompts",
        context_budget=args.context_budget,
    )
    emit("task_completed", status="running", stage="recommendation", task_kind="context_prepare",
         task_id="recommendation-context", task_index=1, task_total=1, task_status="validated",
         completed=1, total=1, message="推荐研究上下文已准备")
    bundle_path = runtime_dir / "recommendation_bundle.json"
    recommendation_summary = run_skill(
        analysis_path,
        prompt_path=prompt_path,
        output_path=bundle_path,
        channel_output_path=runtime_dir / "channel_text.txt",
        prompt_dir=ROOT / "prompts",
        channel="weixin",
        command=recommendation_command,
        mock=False,
        timeout=args.recommendation_timeout,
        context_budget=args.context_budget,
        context_manifest_path=context_manifest_path,
        max_research_rounds=args.max_research_rounds,
        candidate_target=args.candidate_target,
        max_candidates=args.max_candidates,
        recommendation_parallelism=args.recommendation_parallelism,
        progress=emit_progress,
    )
    emit("completed", status="running", stage="recommendation",
         recommendation_count=recommendation_summary.get("recommendation_count", 0),
         candidate_research_rounds=recommendation_summary.get("research_rounds", 0),
         parallelism=args.recommendation_parallelism)

    payload_path = runtime_dir / "web_payload.json"
    emit("started", status="running", stage="export", message="生成网页脱敏数据并原子切换当前版本")
    emit("task_started", status="running", stage="export", task_kind="atlas_export",
         task_id="atlas-export", task_index=1, task_total=1, task_status="running",
         message="正在生成并发布当前 Atlas")
    web_summary = export_web_payload(runtime_dir, payload_path)
    if args.current_data:
        _publish_payload(payload_path, Path(args.current_data))
    write_json(
        runtime_dir / "web_job_report.json",
        {
            "schema_version": "2.0",
            "artifact_type": "web_job_report",
            "status": "completed",
            "completed_at": utc_now(),
            "source": source_report,
            "snapshot_id": snapshot["snapshot_id"],
            "analysis_id": packet["analysis_id"],
            "analysis_parallelism": args.analysis_parallelism,
            "recommendation_parallelism": args.recommendation_parallelism,
            "source_track_count": source_track_count,
            "requested_track_limit": (snapshot.get("reader") or {}).get("requested_track_limit"),
            "prompt_characters": len(prompt),
            "prompt_size": prompt_size_telemetry(prompt),
            "budget_report": budget_report,
            "web_export": web_summary,
            "payload_path": str(payload_path),
            "current_data_path": str(Path(args.current_data).resolve()) if args.current_data else None,
        },
    )
    emit("completed", status="completed", stage="export", payload_path=str(payload_path),
         current_data_path=str(Path(args.current_data).resolve()) if args.current_data else None,
         recommendation_count=web_summary["recommendation_count"])
    emit("task_completed", status="completed", stage="export", task_kind="atlas_export",
         task_id="atlas-export", task_index=1, task_total=1, task_status="published",
         completed=1, total=1, recommendation_count=web_summary["recommendation_count"],
         message="Atlas 已更新")
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
    parser.add_argument("--await-limit-timeout", type=int, default=DEFAULT_AWAIT_LIMIT_TIMEOUT_SECONDS)
    parser.add_argument("--analysis-command", default=None)
    parser.add_argument("--recommendation-command", default=None)
    parser.add_argument("--analysis-parallelism", type=int, default=5)
    parser.add_argument("--recommendation-parallelism", type=int, choices=(3, 4), default=4)
    parser.add_argument("--analysis-batch-size", type=int, default=None)
    parser.add_argument("--analysis-context-budget", type=int, default=None)
    parser.add_argument("--analysis-timeout", type=int, default=600)
    parser.add_argument("--context-budget", type=int, default=None)
    parser.add_argument("--recommendation-timeout", type=int, default=600)
    parser.add_argument("--max-research-rounds", type=int, default=2)
    parser.add_argument("--candidate-target", type=int, default=None)
    parser.add_argument("--max-candidates", type=int, default=80)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return run_web_workflow(args)
    except (ContractError, OSError, ValueError) as exc:
        emit("failed", status="failed", stage="workflow", error=str(exc))
        print(f"音乐网页工作流未执行：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
