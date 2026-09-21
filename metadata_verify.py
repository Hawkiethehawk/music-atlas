"""用平台公开搜索核验候选曲目元数据（曲名 / 艺人 / 专辑 / 封面 / 平台 ID）。

候选的曲名与艺人由推荐 Skill 凭既有知识给出，可能张冠李戴（曲名写成专辑名、
艺人记错、同名曲混淆）。本模块在候选被接受前用**平台公开搜索接口**交叉核验：

- 网易云 `/api/search/get`（与歌单同源，中文/华语覆盖最好）
- QQ 音乐 `client_search_cp`
- iTunes Search API（国际曲目覆盖好，同时给出 Apple Music 链接与封面）

命中后用平台规范名称覆盖展示字段，未命中则报告“未核实”，由调用方丢弃候选。
只读公开搜索接口：不使用登录态、个性化页面、播放历史或任何历史运行结果。
程序只做名称比对与字段规范化，不判断音乐事实，也不参与排序评分。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

SEARCH_TIMEOUT_SECONDS = 8
SEARCH_LIMIT = 10
# 单字段（曲名、艺人）相似度下限：低于此值认为不是同一首歌。
MIN_FIELD_SIMILARITY = 0.7
# 曲名相似度单独下限：不允许靠艺人分把不同的歌拉过线。
MIN_TRACK_SIMILARITY = 0.8
# 综合匹配分下限：低于此值视为未核实。
MIN_MATCH_SCORE = 0.85
# 高置信命中：直接采用，不再查询其他来源。
HIGH_CONFIDENCE_SCORE = 0.92

_BRACKET = re.compile(r"[\(\（\[【].*?[\)\）\]】]")
_FEAT = re.compile(r"\b(feat|ft|with|prod)\.?\s+.*$", re.IGNORECASE)
_VERSION_SUFFIX = re.compile(
    r"\s*[-–—]\s*(remaster(ed)?|live|radio edit|single version|album version|"
    r"deluxe|explicit|bonus track|demo|acoustic)\b.*$",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+")
_SPACES = re.compile(r"\s+")
# 版本/演绎标记：命中带了候选未声明的版本标记时，不是同一录音版本（混音/翻唱/现场等）。
_VERSION_MARKERS = re.compile(
    r"(?:^|[^\w])(remix|flip|bootleg|mashup|cover|instrumental|karaoke|live|"
    r"remaster(?:ed)?|sped\s?up|slowed|reverb|acoustic|demo|version|mix)(?:[^\w]|$)",
    re.IGNORECASE,
)


def _version_markers(value: Any) -> set[str]:
    """从原始文本提取版本标记（在归一化去括号之前检测）。"""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return {match.group(1).replace(" ", "").lower() for match in _VERSION_MARKERS.finditer(text)}


def normalize_name(value: Any) -> str:
    """归一化名称：全角转半角、去括号补充、去 feat./版本后缀、去标点。"""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = _BRACKET.sub(" ", text)
    text = _FEAT.sub(" ", text)
    text = _VERSION_SUFFIX.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def name_similarity(left: Any, right: Any) -> float:
    """0 到 1 的名称相似度；包含关系优先于编辑距离。"""

    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        return 0.9 if len(shorter) / len(longer) >= 0.6 else 0.72
    return round(SequenceMatcher(None, a, b).ratio(), 4)


def _get_json(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    request = Request(url, data=data, headers=headers or {})
    with urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _hit(
    title: Any,
    artist: Any,
    artists: list[str],
    album: Any,
    cover: Any,
    platform_id: Any,
    url: Any,
) -> dict[str, Any]:
    return {
        "title": str(title or "").strip(),
        "artist": str(artist or "").strip(),
        "artists": [name for name in artists if name],
        "album": str(album or "").strip(),
        "cover": str(cover).strip() if cover else None,
        "platform_id": str(platform_id or "").strip(),
        "url": str(url).strip() if url else None,
    }


def search_netease(title: str, artist: str) -> list[dict[str, Any]]:
    """网易云公开搜索（与歌单同源，中文覆盖最好）。"""

    body = urlencode({
        "s": f"{artist} {title}".strip(),
        "type": "1",
        "limit": str(SEARCH_LIMIT),
        "offset": "0",
    }).encode("utf-8")
    payload = _get_json(
        "https://music.163.com/api/search/get",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://music.163.com/",
            "User-Agent": "MusicAtlas/1.0 (+local)",
        },
    )
    songs = (payload.get("result") or {}).get("songs") or []
    results: list[dict[str, Any]] = []
    for song in songs:
        if not isinstance(song, dict):
            continue
        names = [item.get("name") for item in song.get("artists") or [] if isinstance(item, dict)]
        album = song.get("album") if isinstance(song.get("album"), dict) else {}
        track_id = song.get("id")
        results.append(_hit(
            song.get("name"),
            names[0] if names else "",
            [str(name) for name in names if name],
            album.get("name"),
            album.get("picUrl"),
            track_id,
            f"https://music.163.com/song?id={track_id}" if track_id else None,
        ))
    return results


def search_qq(title: str, artist: str) -> list[dict[str, Any]]:
    """QQ 音乐公开搜索。"""

    term = quote(f"{artist} {title}".strip())
    payload = _get_json(
        f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?format=json&limit={SEARCH_LIMIT}&w={term}",
        headers={"Referer": "https://y.qq.com/", "User-Agent": "MusicAtlas/1.0 (+local)"},
    )
    songs = ((payload.get("data") or {}).get("song") or {}).get("list") or []
    results: list[dict[str, Any]] = []
    for song in songs:
        if not isinstance(song, dict):
            continue
        names = [item.get("name") for item in song.get("singer") or [] if isinstance(item, dict)]
        mid = song.get("songmid")
        album_mid = song.get("albummid")
        results.append(_hit(
            song.get("songname"),
            names[0] if names else "",
            [str(name) for name in names if name],
            song.get("albumname"),
            f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else None,
            mid,
            f"https://y.qq.com/n/ryqq/songDetail/{mid}" if mid else None,
        ))
    return results


def search_itunes(title: str, artist: str) -> list[dict[str, Any]]:
    """iTunes Search（国际覆盖好，提供 Apple Music 链接与封面）。"""

    term = quote(f"{artist} {title}".strip())
    payload = _get_json(f"https://itunes.apple.com/search?term={term}&entity=song&limit={SEARCH_LIMIT}")
    results: list[dict[str, Any]] = []
    for hit in payload.get("results") or []:
        if not isinstance(hit, dict):
            continue
        artwork = hit.get("artworkUrl100")
        results.append(_hit(
            hit.get("trackName"),
            hit.get("artistName"),
            [hit.get("artistName")] if hit.get("artistName") else [],
            hit.get("collectionName"),
            artwork.replace("100x100", "600x600") if isinstance(artwork, str) else None,
            hit.get("trackId"),
            hit.get("trackViewUrl"),
        ))
    return results


SOURCES: dict[str, Callable[[str, str], list[dict[str, Any]]]] = {
    "netease": search_netease,
    "qq": search_qq,
    "itunes": search_itunes,
}


@lru_cache(maxsize=1024)
def cover_url_status(url: Any) -> str:
    """检查公开封面是否可达；只缓存结果，不让封面失败影响身份核验。"""

    value = str(url or "").strip()
    if not value or not value.lower().startswith(("http://", "https://")):
        return "missing"
    try:
        request = Request(value, headers={"User-Agent": "MusicAtlas/1.0 (+local)"})
        with urlopen(request, timeout=4) as response:
            status = getattr(response, "status", None) or response.getcode()
            content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
            if int(status) == 200 and content_type.startswith("image/"):
                return "verified"
    except Exception:  # noqa: BLE001 — 可达性失败只影响封面选择
        pass
    return "unreachable"


@lru_cache(maxsize=512)
def netease_song_cover(platform_track_id: str) -> str | None:
    """按已核验的网易云歌曲 ID 读取专辑图，禁止用同名搜索结果替代。"""

    track_id = str(platform_track_id or "").strip()
    if not track_id.isdigit():
        return None
    try:
        payload = _get_json(
            f"https://music.163.com/api/song/detail/?ids=%5B{track_id}%5D",
            headers={"Referer": "https://music.163.com/", "User-Agent": "MusicAtlas/1.0 (+local)"},
        )
        songs = payload.get("songs") or []
        song = songs[0] if songs and isinstance(songs[0], dict) else {}
        album = song.get("album") if isinstance(song.get("album"), dict) else song.get("al")
        cover = album.get("picUrl") if isinstance(album, dict) else None
        return str(cover).strip() if cover else None
    except Exception:  # noqa: BLE001 — 封面失败不能改变已核验的曲目身份
        return None
def netease_song_covers(platform_track_ids: list[str]) -> dict[str, str]:
    """批量读取网易云专辑图：该接口一次可带多首，避免逐首往返（逐首约需 1–3 秒/首）。"""

    ids = [str(item).strip() for item in (platform_track_ids or []) if str(item).strip().isdigit()]
    if not ids:
        return {}
    covers: dict[str, str] = {}
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        try:
            payload = _get_json(
                "https://music.163.com/api/song/detail/?ids=%5B" + ",".join(batch) + "%5D",
                headers={"Referer": "https://music.163.com/", "User-Agent": "MusicAtlas/1.0 (+local)"},
            )
        except Exception:  # noqa: BLE001 — 封面失败不能改变已核验的曲目身份
            continue
        for song in payload.get("songs") or []:
            if not isinstance(song, dict):
                continue
            track_id = str(song.get("id") or "").strip()
            album = song.get("album") if isinstance(song.get("album"), dict) else song.get("al")
            cover = album.get("picUrl") if isinstance(album, dict) else None
            if track_id and cover:
                covers[track_id] = str(cover).strip()
    return covers


# 采用优先级：平台名称以第一个命中的来源为准（同源平台优先）。
SOURCE_PRIORITY = ("netease", "qq", "itunes")


def _score_hit(title: str, artist: str, hit: dict[str, Any]) -> float:
    """打分：曲名必须单独达标，版本标记不得多于候选，艺人以主艺人为准。"""

    track_score = name_similarity(title, hit.get("title"))
    if track_score < MIN_TRACK_SIMILARITY:
        return 0.0
    # 命中带了候选没有的版本标记（混音/翻唱/现场/重制）→ 不是同一版本，拒绝。
    if _version_markers(hit.get("title")) - _version_markers(title):
        return 0.0
    names = hit.get("artists") or [hit.get("artist")]
    primary = str(names[0] or "") if names else ""
    primary_score = name_similarity(artist, primary)
    if primary_score >= MIN_FIELD_SIMILARITY:
        artist_score = primary_score
    else:
        # 主艺人不匹配时（如原曲+混音者双署名），只接受几乎同名的曲目，并降权。
        secondary = max((name_similarity(artist, name) for name in names[1:]), default=0.0)
        if secondary < MIN_FIELD_SIMILARITY or track_score < 0.95:
            return 0.0
        artist_score = secondary * 0.9
    return round((track_score + artist_score) / 2, 4)


def _best_hit(title: str, artist: str, hits: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    best, best_score = None, 0.0
    for hit in hits:
        score = _score_hit(title, artist, hit)
        if score > best_score:
            best, best_score = hit, score
    return best, best_score


def _verified(source: str, hit: dict[str, Any], score: float, *, cover: Any = None,
              cover_source: str | None = None, cover_candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "source": source,
        "score": score,
        "title": hit["title"],
        "artist": hit["artist"],
        "album": hit["album"],
        "cover": str(cover).strip() if cover else None,
        "cover_source": cover_source,
        "cover_candidates": cover_candidates or [],
        "platform_track_id": hit["platform_id"],
        "url": hit["url"],
    }


def _verified_with_cover(source: str, hit: dict[str, Any], score: float,
                         clean_title: str, clean_artist: str, remaining_sources: list[str]) -> dict[str, Any]:
    """保留最高身份命中，封面单独从已匹配且可达的公开来源中选取。"""

    candidates: list[dict[str, Any]] = []
    primary_cover = hit.get("cover")
    primary_status = cover_url_status(primary_cover)
    candidates.append({"source": source, "url": primary_cover, "status": primary_status})
    if primary_status == "verified":
        return _verified(source, hit, score, cover=primary_cover, cover_source=source, cover_candidates=candidates)
    # 缺少或失效的首选封面时，继续查询其他公开来源补图；身份来源仍保持不变。
    for fallback_source in remaining_sources:
        try:
            fallback_hits = SOURCES[fallback_source](clean_title, clean_artist)
        except Exception:  # noqa: BLE001
            continue
        fallback_hit, fallback_score = _best_hit(clean_title, clean_artist, fallback_hits)
        if fallback_hit is None or fallback_score < MIN_MATCH_SCORE:
            continue
        fallback_cover = fallback_hit.get("cover")
        status = cover_url_status(fallback_cover)
        candidates.append({"source": fallback_source, "url": fallback_cover, "status": status})
        if status == "verified":
            return _verified(source, hit, score, cover=fallback_cover, cover_source=fallback_source, cover_candidates=candidates)
    return _verified(source, hit, score, cover=None, cover_source=None, cover_candidates=candidates)


def verification_enabled() -> bool:
    """平台元数据核验开关；测试可用 `ATLAS_METADATA_VERIFY=off` 关闭。"""

    return os.environ.get("ATLAS_METADATA_VERIFY", "").strip().lower() not in ("0", "off", "false", "no")


def _skipped_result(title: str, artist: str) -> dict[str, Any]:
    """核验关闭时保留原值，不覆盖任何字段。"""

    return {
        "source": "skipped",
        "score": 1.0,
        "title": title,
        "artist": artist,
        "album": "",
        "cover": None,
        "platform_track_id": "",
        "url": None,
    }


def verify_candidate(title: Any, artist: Any) -> dict[str, Any] | None:
    """核验单曲；命中返回平台规范元数据，未命中或网络失败返回 None。

    先按优先级逐源查询，遇到高置信命中立即返回；否则继续查询其余来源取最高分。
    """

    clean_title = str(title or "").strip()
    clean_artist = str(artist or "").strip()
    if not clean_title or not clean_artist:
        return None
    if not verification_enabled():
        return _skipped_result(clean_title, clean_artist)
    remaining = list(SOURCE_PRIORITY)
    best: tuple[str, dict[str, Any], float] | None = None
    while remaining:
        source = remaining.pop(0)
        try:
            hits = SOURCES[source](clean_title, clean_artist)
        except Exception:  # noqa: BLE001 — 单个来源失败不影响其他来源核验
            continue
        hit, score = _best_hit(clean_title, clean_artist, hits)
        if hit is not None and score >= HIGH_CONFIDENCE_SCORE:
            return _verified_with_cover(source, hit, score, clean_title, clean_artist, remaining)
        if hit is not None and (best is None or score > best[2]):
            best = (source, hit, score)
    if best is not None and best[2] >= MIN_MATCH_SCORE:
        return _verified_with_cover(best[0], best[1], best[2], clean_title, clean_artist, [source for source in SOURCE_PRIORITY if source != best[0]])
    return None


def verify_many(
    items: list[tuple[str, str]],
    *,
    concurrency: int = 8,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any] | None]:
    """批量核验，保持输入顺序；相同 (曲名, 艺人) 只查询一次。"""

    unique: dict[tuple[str, str], int] = {}
    order: list[tuple[str, str]] = []
    for title, artist in items:
        key = (str(title or "").strip(), str(artist or "").strip())
        if key not in unique:
            unique[key] = len(order)
            order.append(key)
    results: list[dict[str, Any] | None] = [None] * len(order)
    if order:
        workers = max(1, min(int(concurrency), len(order)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(verify_candidate, title, artist): index
                       for index, (title, artist) in enumerate(order)}
            done = 0
            for future in futures:
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception:  # noqa: BLE001 — 核验失败按未命中处理
                    results[index] = None
                done += 1
                if progress is not None:
                    progress(done, len(order))
    return [results[unique[(str(title or "").strip(), str(artist or "").strip())]]
            for title, artist in items]
