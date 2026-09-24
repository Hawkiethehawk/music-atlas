"""Snapshot-bound research contracts; Agents provide facts, never aggregates."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from contracts import (
    ContractError, SCHEMA_VERSION, STYLE_AXIS_IDS, TASTE_MODES, _STYLE_REF_TYPO,
    _source_is_forbidden_personalization, _validate_http_url, _validate_style_axes, _validate_style_mix,
    canonical_style_ref, normalized_name, parse_timestamp, stable_hash, track_key,
    validate_playlist_snapshot,
)
from evidence import require_usable_evidence


_PROGRAM_OWNED_FIELDS = {
    "score", "scores", "score_breakdown", "score_features", "rank", "ranking",
    "rank_variant", "selection", "selection_metadata", "strategy", "program_owned",
    "accepted", "rejected", "candidate_status", "diversity", "sequence", "order",
    # 聚合统计与策略字段同样属于程序：Agent 只能提交曲目事实，不能提交总量/分布/画像/评分/策略。
    "source_track_count", "style_distribution", "interest_profiles", "ranking_score",
    "recommendation_policy", "classified_track_count", "artist_profile_count",
    "profile_coverage", "style_analysis",
}


def _object(
    value: Any,
    fields: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
    strip_extra: bool = False,
) -> dict:
    """校验对象字段。`strip_extra=True` 时剥离无害多余字段（模型偶发补充说明），
    但程序保留字段（评分/排序/策略等）始终硬拒绝。"""

    if not isinstance(value, dict):
        raise ContractError(f"{label} 必须是 JSON 对象")
    allowed = fields | (optional or set())
    missing = fields - set(value)
    extra = set(value) - allowed
    if missing:
        raise ContractError(f"{label} 缺少必填字段：{sorted(missing)}；Agent 不得提交统计、评分或策略")
    if extra:
        forbidden = extra & _PROGRAM_OWNED_FIELDS
        if forbidden:
            raise ContractError(f"{label} 包含程序保留字段：{sorted(forbidden)}；Agent 不得提交统计、评分或策略")
        if not strip_extra:
            raise ContractError(f"{label} 包含未允许字段：{sorted(extra)}；Agent 不得提交统计、评分或策略")
        return {key: item for key, item in value.items() if key not in extra}
    return value


def _text(value: Any, label: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContractError(f"{label} 必须是非空文本且不超过 {maximum} 字符")
    return value


def _confidence(value: Any) -> None:
    if value not in ("high", "medium", "low"):
        raise ContractError("研究画像 confidence 必须为 high、medium 或 low")


def validate_research_evidence(value: Any, required_claim: str, label: str | None = None) -> list[dict]:
    """校验 1 到 8 条公开证据；``label`` 用于错误信息定位到具体曲目。"""

    evidence_label = label or "研究证据"
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ContractError(f"{evidence_label}需要 1 到 8 条公开证据")
    for item in value:
        _object(item, {"claim_type", "claim", "url", "retrieved_at"}, "研究证据",
                optional={"verification_result", "source_identifier"})
        if item["claim_type"] not in ("style", "track_identity", "relation", "release"):
            raise ContractError("研究证据 claim_type 无效")
        _text(item["claim"], "研究证据 claim")
        url = _validate_http_url(item["url"], "研究证据 url")
        if _source_is_forbidden_personalization(url) or (urlparse(url).hostname or "").lower() == "music.apple.com":
            raise ContractError("分析 Agent 不得使用个性化音乐页面或 Apple Music 作为画像证据")
        retrieved_at = parse_timestamp(item["retrieved_at"], "研究证据 retrieved_at")
        if retrieved_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ContractError("研究证据 retrieved_at 不能是未来检索时间")
        if "verification_result" in item and item["verification_result"] not in (
            "verified", "unverified", "contradictory", "inaccessible", "stale", "unverifiable",
        ):
            raise ContractError("研究证据 verification_result 无效")
        if "source_identifier" in item:
            _text(item["source_identifier"], "研究证据 source_identifier")
    if not any(item["claim_type"] == required_claim for item in value):
        raise ContractError(f"研究事实缺少 {required_claim} 证据")
    require_usable_evidence({"evidence_items": value}, lenient_identifier=True)
    return value


def build_research_requests(snapshot: dict, taxonomy: dict, taxonomy_sha256: str, batch_size: int) -> list[dict]:
    validate_playlist_snapshot(snapshot, require_complete=True)
    parse_timestamp(snapshot["captured_at"], "snapshot.captured_at")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 50:
        raise ContractError("analysis-batch-size 必须为 1 到 50 首")
    if not snapshot["tracks"]:
        raise ContractError("空歌单不能生成分析研究任务")
    groups: dict[str, list[dict]] = defaultdict(list)
    for position, original in enumerate(snapshot["tracks"], 1):
        track = {**original, "position": position, "track_key": track_key(original["title"], original["artist"])}
        groups[normalized_name(track["artist"])].append(track)
    ordered = [track for key in sorted(groups, key=lambda key: (-len(groups[key]), key)) for track in groups[key]]
    requests = []
    assigned_artists: set[str] = set()
    snapshot_sha256 = stable_hash(snapshot)
    for offset in range(0, len(ordered), batch_size):
        tracks = ordered[offset:offset + batch_size]
        artists = []
        for track in tracks:
            for artist in dict.fromkeys([track["artist"], *track.get("artists", [])]):
                if normalized_name(artist) not in assigned_artists:
                    artists.append(artist)
                    assigned_artists.add(normalized_name(artist))
        request = {
            "schema_version": SCHEMA_VERSION, "request_type": "musician_research_request",
            "style_fact_policy": "precollected_only",
            "source_snapshot_id": snapshot["snapshot_id"], "snapshot_sha256": snapshot_sha256,
            "taxonomy_sha256": taxonomy_sha256, "source_track_count": snapshot["track_count"],
            "batch_number": len(requests) + 1,
            "tracks": [{key: track[key] for key in ("position", "track_key", "title", "artist", "album", "platform_track_id") if key in track}
                       for track in tracks],
            "relation_artists": artists,
        }
        request["request_id"] = "research-" + stable_hash(request)[:24]
        requests.append(request)
    return requests


def _repair_style_refs(value: Any, known_style_refs: set[str]) -> Any:
    """递归修复模型输出里的 style_ref 笔误（style.pop_punk → style:pop_punk）。

    关闭 reasoning 后模型偶尔写错分隔符或大小写；这是笔误而不是未知风格，
    统一在批次校验入口修好，避免整批研究因单个字符失败。
    """

    if isinstance(value, dict):
        return {key: _repair_style_refs(item, known_style_refs) for key, item in value.items()}
    if isinstance(value, list):
        return [_repair_style_refs(item, known_style_refs) for item in value]
    if isinstance(value, str) and _STYLE_REF_TYPO.match(value.strip()):
        return canonical_style_ref(value, known_style_refs)
    return value


def validate_research_result(value: Any, request: dict, taxonomy: dict) -> dict:
    value = _repair_style_refs(value, set(taxonomy.get("known_style_refs") or []))
    _object(value, {"schema_version", "bundle_type", "request_id", "source_snapshot_id", "generated_at",
                    "track_profiles", "artist_relations"}, "MusicianResearchResult")
    if value["schema_version"] != SCHEMA_VERSION or value["bundle_type"] != "musician_research_result":
        raise ContractError("分析研究结果的类型或 schema 无效")
    if value["request_id"] != request["request_id"] or value["source_snapshot_id"] != request["source_snapshot_id"]:
        raise ContractError("分析研究结果不属于当前快照/批次")
    parse_timestamp(value["generated_at"], "研究结果 generated_at")
    targets = {item["position"]: item for item in request["tracks"]}
    profiles = value["track_profiles"]
    if not isinstance(profiles, list) or len(profiles) != len(targets):
        raise ContractError("研究结果必须逐一覆盖本批每首曲目，未知也须明确返回")
    seen: set[int] = set()
    for index, profile in enumerate(profiles):
        if request.get("style_fact_policy") == "precollected_only":
            if isinstance(profile, dict) and "style_axes" in profile:
                raise ContractError("来源限定研究画像不得提交八轴")
            profile = _object(profile, {"position", "track_key", "classification_status", "scope", "confidence",
                                        "style_mix", "summary", "evidence_items"}, "来源限定研究曲目画像",
                              strip_extra=True)
            profiles[index] = profile
            position = profile["position"]
            if isinstance(position, bool) or not isinstance(position, int) or position not in targets or position in seen:
                raise ContractError("研究画像包含多余、重复或错误的曲目位置")
            seen.add(position)
            if profile["track_key"] != targets[position]["track_key"]:
                raise ContractError("研究画像不能更改曲目身份")
            _text(profile["summary"], "画像 summary")
            if (profile["classification_status"] != "unclassified" or profile["scope"] != "unknown"
                    or profile["confidence"] != "low" or profile["style_mix"] != [] or profile["evidence_items"] != []):
                raise ContractError("未提供已采集来源的研究请求不得提交风格分类或自填来源 URL")
            continue
        profile = _object(profile, {"position", "track_key", "classification_status", "scope", "confidence", "style_mix",
                                   "style_axes", "summary", "evidence_items"}, "研究曲目画像", strip_extra=True)
        profiles[index] = profile
        position = profile["position"]
        if isinstance(position, bool) or not isinstance(position, int) or position not in targets or position in seen:
            raise ContractError("研究画像包含多余、重复或错误的曲目位置")
        seen.add(position)
        if profile["track_key"] != targets[position]["track_key"]:
            raise ContractError("研究画像不能更改曲目身份")
        _text(profile["summary"], "画像 summary")
        _confidence(profile["confidence"])
        status = profile["classification_status"]
        # 已分类但缺少风格证据（模型 v4-flash 等偶发）：自动降级为 unknown，
        # 避免因模型输出不稳导致整个任务失败，同时不虚构事实。
        if status == "classified":
            ev_items = profile.get("evidence_items") or []
            if not isinstance(ev_items, list) or not any(
                e.get("claim_type") == "style" for e in ev_items if isinstance(e, dict)
            ):
                profile.update({
                    "classification_status": "unclassified",
                    "scope": "unknown",
                    "confidence": "low",
                    "style_mix": [],
                    "style_axes": dict.fromkeys(STYLE_AXIS_IDS),
                    "evidence_items": [],
                })
                profiles[index] = profile
                status = "unclassified"
        if status == "unclassified":
            if (profile["scope"] != "unknown" or profile["confidence"] != "low" or profile["style_mix"] != []
                    or profile["style_axes"] != dict.fromkeys(STYLE_AXIS_IDS) or profile["evidence_items"] != []):
                raise ContractError("未知画像必须保留 null 听感、空风格/证据、low 置信度和 unknown 范围")
        elif status == "classified":
            if profile["scope"] not in ("artist", "release", "track"):
                raise ContractError("画像 scope 必须明确为 artist、release 或 track")
            if profile["scope"] == "release" and not targets[position].get("album"):
                raise ContractError("缺少专辑信息的曲目不能声明 release 级画像")
            mix = _validate_style_mix(profile["style_mix"], "研究 style_mix", set(taxonomy["known_style_refs"]))
            if not mix or sum(item["role"] == "primary" for item in mix) != 1:
                raise ContractError("已分类画像必须且只能有一个主风格")
            for item in mix:
                _object(item, {"style_ref", "role", "weight"}, "研究风格权重")
            _object(profile["style_axes"], set(STYLE_AXIS_IDS), "研究 style_axes")
            _validate_style_axes(profile["style_axes"], "研究 style_axes")
            validate_research_evidence(profile["evidence_items"], "style", label=f"曲目 {profile['position']} 的")
        else:
            raise ContractError("画像 classification_status 无效")
    artists = value["artist_relations"]
    if not isinstance(artists, list) or len(artists) != len(request["relation_artists"]):
        raise ContractError("研究关系必须逐一覆盖本批要求的艺人；无证据时返回空关系列表")
    names: set[str] = set()
    for artist in artists:
        _object(artist, {"artist", "entity_type", "lead_vocalists", "related_projects"}, "艺人关系研究")
        name = _text(artist["artist"], "artist", 500)
        if name not in request["relation_artists"] or name in names:
            raise ContractError("艺人关系包含重复或非本批目标艺人")
        names.add(name)
        if artist["entity_type"] not in ("band", "person", "project", "unknown"):
            raise ContractError("研究 entity_type 无效")
        for field in ("lead_vocalists", "related_projects"):
            facts = artist[field]
            if not isinstance(facts, list) or len(facts) > 12:
                raise ContractError("每类关系事实必须是最多 12 项的数组")
            fact_names = set()
            remaining: list[dict[str, Any]] = []
            for fact in facts:
                fields = {"name", "confidence", "evidence_items"} | ({"role", "status"} if field == "lead_vocalists" else {"person", "relation"})
                _object(fact, fields, "音乐人关系事实")
                for key in fields - {"confidence", "evidence_items"}:
                    _text(fact[key], f"关系 {key}", 500)
                # 关系事实缺少 relation 证据（模型不稳）：丢弃该条，不虚构关系
                ev_items = fact.get("evidence_items") or []
                if not isinstance(ev_items, list) or not any(
                    e.get("claim_type") == "relation" for e in ev_items if isinstance(e, dict)
                ):
                    continue
                marker = normalized_name(fact["name"])
                if marker in fact_names:
                    raise ContractError("同一艺人不能重复返回相同关系端点")
                fact_names.add(marker)
                if field == "lead_vocalists" and fact["status"] not in ("current", "former", "unknown"):
                    raise ContractError("主唱 status 必须为 current、former 或 unknown")
                _confidence(fact["confidence"])
                validate_research_evidence(fact["evidence_items"], "relation", label=f"{artist['artist']} 的")
                remaining.append(fact)
            artist[field] = remaining
    return deepcopy(value)


def validate_research_bundle(value: Any, snapshot: dict, taxonomy: dict, taxonomy_sha256: str) -> dict:
    _object(value, {"schema_version", "bundle_type", "source_snapshot_id", "snapshot_sha256", "taxonomy_sha256",
                    "batch_size", "generated_at", "batches", "publication_status"}, "MusicianResearchBundle")
    if (value["schema_version"] != SCHEMA_VERSION or value["bundle_type"] != "musician_research_bundle"
            or value["publication_status"] != "draft"):
        raise ContractError("MusicianResearchBundle 类型/schema/草稿状态无效")
    if (value["source_snapshot_id"] != snapshot["snapshot_id"] or value["snapshot_sha256"] != stable_hash(snapshot)
            or value["taxonomy_sha256"] != taxonomy_sha256):
        raise ContractError("分析研究包不属于当前快照或风格词表")
    parse_timestamp(value["generated_at"], "分析研究包 generated_at")
    requests = build_research_requests(snapshot, taxonomy, taxonomy_sha256, value["batch_size"])
    if not isinstance(value["batches"], list) or len(value["batches"]) != len(requests):
        raise ContractError("分析研究包缺少批次，不能生成完成分析")
    for result, request in zip(value["batches"], requests):
        validate_research_result(result, request, taxonomy)
    return value


def _validate_score_or_absent(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ContractError(f"{label} 必须是 0 到 100 的数值或 null")


def validate_taste_summary_result(value: Any, *, snapshot: dict, taxonomy: dict, mode: str) -> dict:
    """品味摘要契约（中/大歌单）。

    Agent 只提供场景归属、风格标签、语义主题与锐评文案；
    艺人与歌名必须逐字来自当前快照，风格引用必须来自风格本体；
    任何推荐、评分、策略字段都会被拒绝。"""
    if mode not in TASTE_MODES:
        raise ContractError(f"未知的品味摘要模式：{mode}")
    fields = {"schema_version", "bundle_type", "request_id", "source_snapshot_id", "generated_at",
              "analysis_mode", "knowledge_basis", "artist_clusters", "style_tags", "taste_profile",
              "editorial_review", "limitations", "uncertainties"}
    optional: set[str] = {"semantic_themes", "overall_summary", "islands"}
    _object(value, fields, "TasteSummaryResult", optional=optional)
    if value["schema_version"] != SCHEMA_VERSION or value["bundle_type"] != "taste_summary_result":
        raise ContractError("TasteSummaryResult 类型/schema 无效")
    if value["analysis_mode"] != mode:
        raise ContractError(f"analysis_mode 必须是 {mode}")
    if value["source_snapshot_id"] != snapshot["snapshot_id"]:
        raise ContractError("品味摘要不属于当前快照")
    parse_timestamp(value["generated_at"], "品味摘要 generated_at")

    artist_keys = {normalized_name(track["artist"]) for track in snapshot["tracks"]}
    for track in snapshot["tracks"]:
        for name in track.get("artists", []) or []:
            artist_keys.add(normalized_name(name))
    title_keys = {normalized_name(track["title"]) for track in snapshot["tracks"]}
    known_refs = set(taxonomy["known_style_refs"])

    basis = _object(value["knowledge_basis"], {"model_internal", "web_verified", "inference"}, "knowledge_basis")
    for key, text in basis.items():
        _text(text, f"knowledge_basis.{key}", 600)

    clusters = value["artist_clusters"]
    if not isinstance(clusters, list) or not clusters:
        raise ContractError("artist_clusters 必须是非空数组")
    seen_artists: set[str] = set()
    cluster_refs_by_artist: dict[str, list[str]] = {}
    cluster_fields = {"artist", "scene", "confidence", "style_refs", "reference_url"}
    if mode == "artist_summary":
        cluster_fields.add("layer")
    for index, item in enumerate(clusters):
        cluster = _object(item, cluster_fields, f"artist_clusters[{index}]")
        marker = normalized_name(cluster["artist"])
        if marker not in artist_keys:
            raise ContractError(f"artist_clusters[{index}] 引用了清单之外的艺人：{cluster['artist']}")
        if marker in seen_artists:
            # 不同写法可能归一化到同一歌手：跳过重复簇，而不是拒绝整份摘要。
            continue
        seen_artists.add(marker)
        _text(cluster["scene"], f"artist_clusters[{index}].scene", 200)
        _confidence(cluster["confidence"])
        if mode == "artist_summary" and cluster["layer"] not in ("core", "active", "longtail"):
            raise ContractError(f"artist_clusters[{index}].layer 必须是 core、active 或 longtail")
        refs = cluster["style_refs"]
        if not isinstance(refs, list) or not refs or any(ref not in known_refs for ref in refs):
            raise ContractError(f"artist_clusters[{index}].style_refs 必须是风格本体引用")
        url = cluster["reference_url"]
        if url not in ("", None):
            _validate_http_url(url, f"artist_clusters[{index}].reference_url")
        cluster_refs_by_artist[marker] = list(refs)

    tags = value["style_tags"]
    if not isinstance(tags, list) or not 1 <= len(tags) <= 12:
        raise ContractError("style_tags 必须是 1 到 12 个元素")
    seen_tag_refs: set[str] = set()
    for index, item in enumerate(tags):
        tag = _object(item, {"tag", "weight", "matched_artists"}, f"style_tags[{index}]")
        if tag["tag"] not in known_refs:
            raise ContractError(f"style_tags[{index}].tag 必须来自风格本体：{tag['tag']}")
        if tag["tag"] in seen_tag_refs:
            raise ContractError(f"style_tags[{index}] 重复风格引用")
        seen_tag_refs.add(tag["tag"])
        weight = tag["weight"]
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 <= weight <= 100:
            raise ContractError(f"style_tags[{index}].weight 必须是 0 到 100")
        matched = tag["matched_artists"]
        if not isinstance(matched, list) or any(normalized_name(name) not in artist_keys for name in matched):
            raise ContractError(f"style_tags[{index}].matched_artists 引用了清单之外的艺人")

    if mode == "taste_summary":
        if "semantic_themes" not in value:
            raise ContractError("taste_summary 模式必须提供 semantic_themes")
        themes = value["semantic_themes"]
        if not isinstance(themes, list) or not 2 <= len(themes) <= 6:
            raise ContractError("semantic_themes 必须是 2 到 6 个主题")

    if mode == "artist_summary" and "semantic_themes" in value:
        raise ContractError("artist_summary 模式没有歌名清单，不得提交 semantic_themes")

    # 摘要可以选择一并给出整体总结与三个兴趣岛（一次调用完成分析）。
    if "overall_summary" in value:
        summary_text = value["overall_summary"]
        if not isinstance(summary_text, str) or not 80 <= len(summary_text.strip()) <= 300:
            raise ContractError("overall_summary 必须是 80–300 字的文本")
    if "islands" in value:
        islands = value["islands"]
        if not isinstance(islands, list) or len(islands) != 3:
            raise ContractError("islands 必须是 3 个兴趣岛")
        seen_islands: set[str] = set()
        for index, item in enumerate(islands):
            island = _object(item, {"name", "summary", "artists"}, f"islands[{index}]")
            name = _text(island["name"], f"islands[{index}].name", 20)
            if name in seen_islands:
                raise ContractError(f"islands[{index}] 重复名称")
            seen_islands.add(name)
            _text(island["summary"], f"islands[{index}].summary", 300)
            island_artists = island["artists"]
            if not isinstance(island_artists, list) or not island_artists:
                raise ContractError(f"islands[{index}].artists 必须是非空数组")
            if any(normalized_name(name_or_artist) not in artist_keys for name_or_artist in island_artists):
                raise ContractError(f"islands[{index}].artists 引用了清单之外的歌手")

    if mode == "taste_summary":
        seen_themes: set[str] = set()
        for index, item in enumerate(value["semantic_themes"]):
            theme = _object(item, {"theme", "tracks", "note"}, f"semantic_themes[{index}]")
            marker = normalized_name(theme["theme"])
            if marker in seen_themes:
                raise ContractError(f"semantic_themes[{index}] 重复主题")
            seen_themes.add(marker)
            _text(theme["theme"], f"semantic_themes[{index}].theme", 120)
            tracks = theme["tracks"]
            if not isinstance(tracks, list) or len(tracks) < 3:
                raise ContractError(f"semantic_themes[{index}].tracks 至少需要 3 首支撑曲目")
            if any(normalized_name(title) not in title_keys for title in tracks):
                raise ContractError(f"semantic_themes[{index}].tracks 引用了清单之外的歌名")
            _text(theme["note"], f"semantic_themes[{index}].note", 600)

    profile = _object(value["taste_profile"], {"dominant_styles", "secondary_styles",
                                              "exploration_appetite"}, "taste_profile", optional={"mood_axes"})
    for field in ("dominant_styles", "secondary_styles"):
        styles = profile[field]
        if not isinstance(styles, list) or any(ref not in known_refs for ref in styles):
            raise ContractError(f"taste_profile.{field} 必须是风格本体引用数组")
    if profile["exploration_appetite"] not in ("high", "medium", "low"):
        raise ContractError("taste_profile.exploration_appetite 必须是 high、medium 或 low")
    # Legacy summaries remain readable; new prompts never request estimated
    # audio axes. The new source-evidence packet discards any legacy axes.
    if "mood_axes" in profile:
        _validate_style_axes(profile["mood_axes"], "taste_profile.mood_axes", allow_unknown=False)

    review = _object(value["editorial_review"],
                     {"headline", "review", "inner_world", "humor_notes"}, "editorial_review")
    _text(review["headline"], "editorial_review.headline", 120)
    _text(review["review"], "editorial_review.review", 2000)
    _text(review["inner_world"], "editorial_review.inner_world", 2000)
    notes = review["humor_notes"]
    if not isinstance(notes, list) or any(
        not isinstance(note, dict) or set(note) != {"note", "speculation"}
        or not isinstance(note["speculation"], bool) or not note["speculation"]
        or not isinstance(note["note"], str) or not note["note"].strip()
        for note in notes
    ):
        raise ContractError("humor_notes 每项必须含 note 与 speculation: true")

    for field in ("limitations", "uncertainties"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ContractError(f"{field} 必须是字符串数组")
    return deepcopy(value)
