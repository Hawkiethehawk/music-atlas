"""Explicit offline listening comparisons; never train or mutate a policy."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from contracts import ContractError, SCHEMA_VERSION, normalized_name, stable_hash, utc_now
from evidence import audit_bundle_evidence
from recommender import rank_bundle


REASONS = {"style_mismatch", "vocal_mismatch", "too_heavy", "too_light", "already_known", "version_mismatch",
           "liked_melody", "liked_texture", "liked_rhythm", "other"}


def _input_identity(packet: dict[str, Any]) -> str:
    return stable_hash({key: packet.get(key) for key in ("source_snapshot_id", "source_snapshot_input_sha256", "source_platform",
                                                       "as_of_date", "favorite_tracks", "preferred_artists")})


def _pair(packet_a, bundle_a, packet_b, bundle_b):
    if _input_identity(packet_a) != _input_identity(packet_b):
        raise ContractError("试听对照必须使用相同收藏输入、来源快照和评分日期")
    ranked_a, ranked_b = rank_bundle(bundle_a, packet_a), rank_bundle(bundle_b, packet_b)
    if any(item["status"] != "ready" for item in (ranked_a, ranked_b)):
        raise ContractError("试听对照需要两份完整的 ranked 研究草稿")
    if len(ranked_a["recommendations"]) != len(ranked_b["recommendations"]):
        raise ContractError("两份方案的推荐数量必须相同")
    tracks = {}
    for item in [*ranked_a["recommendations"], *ranked_b["recommendations"]]:
        identity = item["canonical_track_id"].casefold()
        if identity in tracks and any(item.get(key) != tracks[identity].get(key) for key in ("title", "artist", "album", "platform_track_id")):
            raise ContractError("同一候选标识在两份方案中对应不同歌曲事实")
        tracks[identity] = item
    return ranked_a, ranked_b, tracks


def prepare_listening_benchmark(packet_a, bundle_a, packet_b, bundle_b) -> dict[str, Any]:
    _, _, tracks = _pair(packet_a, bundle_a, packet_b, bundle_b)
    return {"schema_version": SCHEMA_VERSION, "artifact_type": "listening_benchmark",
            "input_fingerprint": _input_identity(packet_a), "source_snapshot_id": packet_a["source_snapshot_id"],
            "as_of_date": packet_a["as_of_date"], "allowed_verdicts": ["like", "dislike", "unsure"],
            "allowed_reasons": sorted(REASONS), "policy_changed": False,
            "judgments": [{"canonical_track_id": tracks[key]["canonical_track_id"], "title": tracks[key]["title"],
                           "artist": tracks[key]["artist"], "platform_links": tracks[key].get("platform_links", {}),
                           "verdict": None, "reasons": [], "notes": ""}
                          for key in sorted(tracks, key=stable_hash)]}


def _judgments(value, packet, tracks):
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or value.get("artifact_type") != "listening_benchmark":
        raise ContractError("试听标注文件类型或 schema 无效")
    if value.get("input_fingerprint") != _input_identity(packet):
        raise ContractError("试听标注不属于本次对照输入")
    rows = value.get("judgments")
    if not isinstance(rows, list):
        raise ContractError("judgments 必须是数组")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("canonical_track_id"), str):
            raise ContractError("试听标注必须包含 canonical_track_id")
        identity = row["canonical_track_id"].casefold()
        if identity not in tracks or identity in result:
            raise ContractError("试听标注包含不在本次对照中的歌曲或重复标识")
        if row.get("verdict") not in (None, "like", "dislike", "unsure"):
            raise ContractError("verdict 只能为 like、dislike、unsure 或 null")
        reasons = row.get("reasons", [])
        if not isinstance(reasons, list) or any(not isinstance(reason, str) or reason not in REASONS for reason in reasons) or len(set(reasons)) != len(reasons):
            raise ContractError("试听理由必须是无重复的允许理由数组")
        if not isinstance(row.get("notes", ""), str) or len(row.get("notes", "")) > 2000:
            raise ContractError("试听 notes 必须为不超过 2000 字符的文本")
        if any(key in row and row[key] != tracks[identity][key] for key in ("title", "artist")):
            raise ContractError("试听标注中的歌曲身份与对照结果不一致")
        result[identity] = row
    return result


def _metrics(bundle, packet, judgments):
    selected = bundle["recommendations"]
    rated = [item for item in selected if judgments.get(item["canonical_track_id"].casefold(), {}).get("verdict") in {"like", "dislike"}]
    liked = [item for item in rated if judgments[item["canonical_track_id"].casefold()]["verdict"] == "like"]
    known = {normalized_name(item["artist"]) for item in packet["primary_distribution"]}
    novel = [item for item in rated if normalized_name(item["artist"]) not in known]
    novel_liked = sum(judgments[item["canonical_track_id"].casefold()]["verdict"] == "like" for item in novel)
    interests = Counter(item.get("matched_interest_id") for item in selected if item.get("matched_interest_id"))
    audit = audit_bundle_evidence(bundle)
    return {"recommendation_count": len(selected), "judged_count": len(rated), "liked_count": len(liked),
            "feedback_coverage": round(len(rated) / len(selected), 6),
            "acceptance_rate": round(len(liked) / len(rated), 6) if rated else None,
            "novel_artist_judged_count": len(novel), "novel_artist_acceptance_rate": round(novel_liked / len(novel), 6) if novel else None,
            "selected_interest_counts": dict(interests), "available_interest_count": bundle["ranking"].get("interest_group_count"),
            "reason_counts": dict(Counter(reason for item in rated for reason in judgments[item["canonical_track_id"].casefold()].get("reasons", []))),
            "evidence_accepted_count": audit["accepted_count"], "evidence_status": audit["status"]}


def _telemetry(report, packet, bundle):
    if report is None:
        return {"status": "unavailable"}
    if not isinstance(report, dict) or report.get("artifact_type") != "research_report" or report.get("analysis_id") != packet["analysis_id"] or report.get("ranked_bundle_sha256") != stable_hash(bundle):
        raise ContractError("研究耗时报告与对应分析/排序结果不匹配")
    fields = ("total_elapsed_ms", "input_characters_total")
    if any(isinstance(report.get(key), bool) or not isinstance(report.get(key), (int, float)) or not math.isfinite(report[key]) or report[key] < 0 for key in fields):
        raise ContractError("研究耗时报告包含无效计量")
    for field in ("candidate_target", "max_candidates", "max_rounds", "research_timeout_seconds"):
        if isinstance(report.get(field), bool) or not isinstance(report.get(field), int) or report[field] <= 0:
            raise ContractError("研究耗时报告缺少有效预算记录")
    if not isinstance(report.get("rounds"), list) or len(report["rounds"]) > report["max_rounds"]:
        raise ContractError("研究耗时报告的轮数无效")
    return {"status": "measured", **{key: report[key] for key in fields}, "round_count": len(report.get("rounds", [])),
            "limits": {key: report.get(key) for key in ("candidate_target", "max_candidates", "max_rounds", "context_budget", "research_timeout_seconds")}}


def compare_listening_benchmark(packet_a, bundle_a, packet_b, bundle_b, judgments, *, report_a=None, report_b=None):
    ranked_a, ranked_b, tracks = _pair(packet_a, bundle_a, packet_b, bundle_b)
    rows = _judgments(judgments, packet_a, tracks)
    a, b = _metrics(ranked_a, packet_a, rows), _metrics(ranked_b, packet_b, rows)
    complete = a["feedback_coverage"] == 1 and b["feedback_coverage"] == 1
    telemetry_a, telemetry_b = _telemetry(report_a, packet_a, ranked_a), _telemetry(report_b, packet_b, ranked_b)
    same_limits = (telemetry_a["limits"] == telemetry_b["limits"]) if telemetry_a["status"] == telemetry_b["status"] == "measured" else None
    return {"schema_version": SCHEMA_VERSION, "artifact_type": "listening_comparison", "generated_at": utc_now(),
            "status": "descriptive_comparison" if complete else "incomplete_labels", "input_fingerprint": _input_identity(packet_a),
            "analysis_a": packet_a["analysis_id"], "analysis_b": packet_b["analysis_id"],
            "bundle_a_sha256": stable_hash(ranked_a), "bundle_b_sha256": stable_hash(ranked_b), "judgments_sha256": stable_hash(judgments),
            "a": a, "b": b, "acceptance_rate_delta_b_minus_a": round(b["acceptance_rate"] - a["acceptance_rate"], 6) if complete else None,
            "telemetry_a": telemetry_a, "telemetry_b": telemetry_b, "same_research_limits": same_limits,
            "winner": None, "policy_changed": False, "approval_required": True, "auto_applied": False,
            "notes": ["未标注与 unsure 不算拒绝；标签未覆盖双方全部推荐时不报告优劣差值。",
                      "完整标注也仅提供描述性比较，不宣称统计显著性或自动选择策略。",
                      "兴趣覆盖与目录证据状态是诊断信息，不代表音频测量或在线事实核验。"]}
