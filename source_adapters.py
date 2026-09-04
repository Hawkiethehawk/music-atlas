#!/usr/bin/env python3
"""Step 1 source adapters and normalization into PlaylistSnapshot."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from contracts import ContractError, SCHEMA_VERSION, normalized_text, track_key, utc_now, write_json


class PlaylistReader(Protocol):
    def read(
        self,
        input_path: Path,
        *,
        platform: str,
        playlist_id: str,
        playlist_name: str,
        declared_count: int | None = None,
        declared_count_file: Path | None = None,
    ) -> dict[str, Any]:
        ...


def _as_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "href", "link"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _as_string(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "text", "label", "title", "artistName"):
            if value.get(key) is not None:
                return normalized_text(value[key])
        return ""
    return normalized_text(value)


def _as_artist_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = _as_string(item)
        marker = name.casefold()
        if name and marker not in seen:
            seen.add(marker)
            result.append(name)
    return result


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _track_id(item: dict[str, Any], song_url: str) -> str:
    direct = _first_value(item, "platform_track_id", "track_id", "song_id", "id")
    if direct is not None and not isinstance(direct, dict):
        value = normalized_text(direct)
        if value:
            return value
    parsed = urlparse(song_url)
    query_id = parse_qs(parsed.query).get("i", [""])[0].strip()
    if query_id:
        return query_id
    match = re.search(r"/(\d+)(?:[/?#]|$)", parsed.path)
    return match.group(1) if match else ""


def _extract_items(payload: Any) -> tuple[list[Any], dict[str, Any]]:
    if isinstance(payload, list):
        return payload, {}
    if not isinstance(payload, dict):
        raise ContractError("歌单输入必须是数组或包含歌曲数组的对象")
    for key in ("tracks", "items", "songs", "entries", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value, payload
    raise ContractError("歌单输入对象中找不到 tracks/items/songs/entries/data 数组")


def _count_from_mapping(mapping: dict[str, Any]) -> int | None:
    keys = (
        "declared_track_count",
        "track_count",
        "total_track_count",
        "total_count",
        "tracks_count",
        "count",
        "tracks",
    )
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
    metadata = mapping.get("metadata")
    if isinstance(metadata, dict):
        return _count_from_mapping(metadata)
    return None


def _declared_count(
    raw_metadata: dict[str, Any],
    *,
    explicit: int | None,
    count_file: Path | None,
) -> int:
    if explicit is not None:
        if isinstance(explicit, bool) or explicit < 0:
            raise ContractError("declared_count 必须是大于等于 0 的整数")
        return explicit
    from_input = _count_from_mapping(raw_metadata)
    if from_input is not None:
        return from_input
    if count_file is not None:
        try:
            import json

            value = json.loads(count_file.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ContractError(f"找不到 Step 1 数量清单：{count_file}") from exc
        except OSError as exc:
            raise ContractError(f"无法读取 Step 1 数量清单：{count_file}：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise ContractError(f"Step 1 数量清单不是有效 JSON：{count_file}") from exc
        if isinstance(value, dict):
            count = _count_from_mapping(value)
            if count is not None:
                return count
        raise ContractError(f"Step 1 数量清单中没有可用的动态歌曲总数：{count_file}")
    raise ContractError(
        "原始歌单没有提供 Step 1 声明数量；请传入 declared_count_file，"
        "或直接使用已标准化的 PlaylistSnapshot"
    )


def _normalize_track(item: Any, position: int, platform: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ContractError(f"第 {position} 首歌曲不是对象")
    title = _as_string(_first_value(item, "title", "name", "song_name", "track_name"))
    artist = _as_string(_first_value(item, "artist", "artist_name", "artistName", "main_artist"))
    if not title or not artist:
        raise ContractError(f"第 {position} 首歌曲缺少 title 或 artist")

    credited = _as_artist_list(_first_value(item, "artists", "artist_list", "credited_artists"))
    if not credited:
        credited = [artist]
    if artist.casefold() not in {name.casefold() for name in credited}:
        credited.insert(0, artist)
    song_url = _as_url(_first_value(item, "song_url", "track_url", "url", "href"))
    album_url = _as_url(_first_value(item, "album_url", "release_url"))
    links: dict[str, str] = {}
    if song_url:
        links[platform] = song_url
    extra_links = item.get("links")
    if isinstance(extra_links, dict):
        for link_platform, link_value in extra_links.items():
            link = _as_url(link_value)
            if link:
                links[normalized_text(link_platform)] = link

    result: dict[str, Any] = {
        "position": position,
        "title": title,
        "artist": artist,
        "artists": credited,
        "album": _as_string(_first_value(item, "album", "album_name", "albumName", "release")),
        "duration": _as_string(_first_value(item, "duration", "length")),
        "platform_track_id": _track_id(item, song_url),
        "track_key": track_key(title, artist),
        "links": links,
    }
    if album_url:
        result["album_link"] = album_url
    return result


class LocalJsonReader:
    """Read a local export. It is also the adapter used by future platform readers."""

    def read(
        self,
        input_path: Path,
        *,
        platform: str,
        playlist_id: str,
        playlist_name: str,
        declared_count: int | None = None,
        declared_count_file: Path | None = None,
    ) -> dict[str, Any]:
        try:
            import json

            raw_bytes = input_path.read_bytes()
            payload = json.loads(raw_bytes.decode("utf-8"))
        except FileNotFoundError as exc:
            raise ContractError(f"找不到歌单输入：{input_path}") from exc
        except OSError as exc:
            raise ContractError(f"无法读取歌单输入：{input_path}：{exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"歌单输入不是有效 UTF-8 JSON：{input_path}") from exc

        raw_items, raw_metadata = _extract_items(payload)
        tracks = [_normalize_track(item, position, platform) for position, item in enumerate(raw_items, 1)]
        declared = _declared_count(
            raw_metadata,
            explicit=declared_count,
            count_file=declared_count_file,
        )
        actual = len(tracks)
        input_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": f"{platform}-{input_sha256[:16]}",
            "platform": platform,
            "playlist_id": playlist_id,
            "playlist_name": playlist_name,
            "declared_track_count": declared,
            "track_count": actual,
            "reader_status": "complete" if declared == actual else "incomplete",
            "captured_at": utc_now(),
            "input_sha256": input_sha256,
            "reader": {
                "type": "local_json",
                "source_file_name": input_path.name,
                "declared_count_source": (
                    "argument"
                    if declared_count is not None
                    else "input_metadata"
                    if _count_from_mapping(raw_metadata) is not None
                    else "declared_count_file"
                ),
            },
            "tracks": tracks,
        }


class NeteaseJsonReader(LocalJsonReader):
    """网易云歌单导出适配器占位：沿用统一 JSON 字段归一化，不读个性化推荐。"""


class CsvPlaylistReader:
    """Small CSV import adapter for future platform exports."""

    def read(
        self,
        input_path: Path,
        *,
        platform: str,
        playlist_id: str,
        playlist_name: str,
        declared_count: int | None = None,
        declared_count_file: Path | None = None,
    ) -> dict[str, Any]:
        try:
            with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
                items = list(csv.DictReader(handle))
        except FileNotFoundError as exc:
            raise ContractError(f"找不到歌单输入：{input_path}") from exc
        except OSError as exc:
            raise ContractError(f"无法读取歌单输入：{input_path}：{exc}") from exc
        raw_bytes = input_path.read_bytes()
        raw_metadata = {}
        tracks = [_normalize_track(item, position, platform) for position, item in enumerate(items, 1)]
        declared = _declared_count(raw_metadata, explicit=declared_count, count_file=declared_count_file)
        actual = len(tracks)
        input_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": f"{platform}-{input_sha256[:16]}",
            "platform": platform,
            "playlist_id": playlist_id,
            "playlist_name": playlist_name,
            "declared_track_count": declared,
            "track_count": actual,
            "reader_status": "complete" if declared == actual else "incomplete",
            "captured_at": utc_now(),
            "input_sha256": input_sha256,
            "reader": {
                "type": "csv",
                "source_file_name": input_path.name,
                "declared_count_source": "argument" if declared_count is not None else "declared_count_file",
            },
            "tracks": tracks,
        }


NETEASE_DETAIL_URL = "https://music.163.com/api/playlist/detail"
NETEASE_REQUEST_HEADERS = {
    "Cookie": "os=pc; appver=2.9.7",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://music.163.com/",
}

QQ_MUSICU_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
QQ_REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://y.qq.com/",
    "Origin": "https://y.qq.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}
QQ_PAGE_SIZE = 100
QQ_MAX_PAGES = 50


def _playlist_id_from_arg(value: str) -> str:
    """Extract the numeric playlist id from an id string or a share URL."""

    text = normalized_text(value)
    if not text:
        raise ContractError("网易云歌单 ID 不能为空")
    if re.fullmatch(r"\d+", text):
        return text
    parsed = urlparse(text)
    host = (parsed.netloc or "").casefold()
    if "music.163.com" in host:
        query_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        if query_id.isdigit():
            return query_id
    match = re.search(r"/playlist/(\d+)", parsed.path or text)
    if match:
        return match.group(1)
    raise ContractError(f"无法从网易云歌单来源解析数字 ID：{value}")


def _fetch_netease_playlist_detail(playlist_id: str) -> bytes:
    """Fetch the anonymous public playlist detail endpoint."""

    request = Request(
        f"{NETEASE_DETAIL_URL}?id={playlist_id}",
        headers=NETEASE_REQUEST_HEADERS,
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ContractError(f"网易云接口请求失败：{exc}") from exc


def _parse_netease_detail(payload: Any) -> tuple[list[dict[str, Any]], int, str]:
    """Normalize a playlist-detail payload into snapshot tracks.

    Returns ``(tracks, declared_track_count, playlist_name)``. Malformed
    track entries are skipped, not fatal; a resulting difference between
    declared and actual counts surfaces as ``reader_status="incomplete"``
    through the existing Step 1 contract.
    """

    if not isinstance(payload, dict) or payload.get("code") != 200:
        code = payload.get("code") if isinstance(payload, dict) else "非对象"
        raise ContractError(f"网易云接口返回异常：code={code}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ContractError("网易云接口缺少 result 对象")
    raw_tracks = result.get("tracks") or []
    raw_track_ids = result.get("trackIds") or []
    if not isinstance(raw_tracks, list) or not isinstance(raw_track_ids, list):
        raise ContractError("网易云接口的 tracks/trackIds 必须是数组")
    playlist_name = normalized_text(result.get("name"))
    normalized: list[dict[str, Any]] = []
    for raw_item in raw_tracks:
        if not isinstance(raw_item, dict):
            continue
        title = normalized_text(raw_item.get("name"))
        credited = [
            normalized_text(artist.get("name"))
            for artist in (raw_item.get("artists") or [])
            if isinstance(artist, dict) and normalized_text(artist.get("name"))
        ]
        if not title or not credited:
            continue
        track_id = normalized_text(raw_item.get("id"))
        album_value = raw_item.get("album")
        album = normalized_text(album_value.get("name")) if isinstance(album_value, dict) else ""
        links: dict[str, str] = {}
        if track_id:
            links["netease"] = f"https://music.163.com/song?id={track_id}"
        normalized.append(
            {
                "position": len(normalized) + 1,
                "title": title,
                "artist": credited[0],
                "artists": credited,
                "album": album,
                "duration": "",
                "platform_track_id": track_id,
                "track_key": track_key(title, credited[0]),
                "links": links,
            }
        )
    declared = len(raw_track_ids) if raw_track_ids else len(normalized)
    return normalized, declared, playlist_name


class NeteasePublicPlaylistReader:
    """Read a *public* Netease playlist via the anonymous detail endpoint.

    No login state is used or accepted: private playlists (such as the
    account's own favourites) are outside this reader's reach by design.
    The ``input_path`` argument is ignored; the playlist comes from
    ``playlist_id`` (numeric id or a music.163.com share URL).
    """

    def read(
        self,
        input_path: Path | None,
        *,
        platform: str,
        playlist_id: str,
        playlist_name: str,
        declared_count: int | None = None,
        declared_count_file: Path | None = None,
    ) -> dict[str, Any]:
        playlist_arg = _playlist_id_from_arg(playlist_id)
        raw = _fetch_netease_playlist_detail(playlist_arg)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"网易云接口响应不是有效 UTF-8 JSON：{exc}") from exc
        tracks, declared_from_api, api_playlist_name = _parse_netease_detail(payload)
        declared = _declared_count(
            {"declared_track_count": declared_from_api},
            explicit=declared_count,
            count_file=declared_count_file,
        )
        actual = len(tracks)
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": f"{platform}-{hashlib.sha256(raw).hexdigest()[:16]}",
            "platform": platform,
            "playlist_id": playlist_arg,
            "playlist_name": api_playlist_name or normalized_text(playlist_name),
            "declared_track_count": declared,
            "track_count": actual,
            "reader_status": "complete" if declared == actual else "incomplete",
            "captured_at": utc_now(),
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "reader": {
                "type": "netease_public",
                "source_playlist_id": playlist_arg,
                "declared_count_source": (
                    "argument" if declared_count is not None else "api_track_ids"
                ),
            },
            "tracks": tracks,
        }


def _playlist_id_from_qq_arg(value: str) -> str:
    """Extract the numeric playlist id from an id string or a share URL."""

    text = normalized_text(value)
    if not text:
        raise ContractError("QQ 音乐歌单 ID 不能为空")
    if re.fullmatch(r"\d+", text):
        return text
    parsed = urlparse(text)
    host = (parsed.netloc or "").casefold()
    if host.endswith("qq.com"):
        query_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        if query_id.isdigit():
            return query_id
        match = re.search(r"/playlist/(\d+)", parsed.path or "")
        if match:
            return match.group(1)
    match = re.search(r"playlist[/=#]*(\d+)", text)
    if match:
        return match.group(1)
    raise ContractError(f"无法从 QQ 音乐歌单来源解析数字 ID：{value}")


def _qq_diss_payload(playlist_id: str, song_begin: int, song_num: int) -> dict[str, Any]:
    return {
        "comm": {"ct": 24, "cv": 0},
        "req_1": {
            "module": "music.srfDissInfo.DissInfo",
            "method": "CgiGetDiss",
            "param": {
                "disstid": int(playlist_id),
                "dirid": 0,
                "tag": False,
                "song_begin": song_begin,
                "song_num": song_num,
                "userinfo": False,
                "orderlist": True,
                "onlysonglist": False,
            },
        },
    }


def _fetch_qq_playlist_page(playlist_id: str, song_begin: int, song_num: int) -> bytes:
    """Fetch one page of an anonymous public QQ Music playlist."""

    request = Request(
        QQ_MUSICU_URL,
        data=json.dumps(_qq_diss_payload(playlist_id, song_begin, song_num)).encode("utf-8"),
        headers=QQ_REQUEST_HEADERS,
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ContractError(f"QQ 音乐接口请求失败：{exc}") from exc


def _parse_qq_diss_page(payload: Any) -> dict[str, Any]:
    """Normalize one CgiGetDiss page into snapshot tracks.

    Returns a dict with ``tracks``, ``total`` (declared song count),
    ``hasmore`` (API pagination flag; ``None`` when absent), ``raw_count``
    (rows received before normalization) and ``playlist_name``. Malformed
    song entries are skipped, not fatal; a resulting difference between
    the declared total and the collected count surfaces as
    ``reader_status="incomplete"`` through the existing Step 1 contract.
    """

    if not isinstance(payload, dict) or payload.get("code") != 0:
        code = payload.get("code") if isinstance(payload, dict) else "非对象"
        raise ContractError(f"QQ 音乐接口返回异常：code={code}")
    req = payload.get("req_1")
    if not isinstance(req, dict) or req.get("code") != 0:
        req_code = req.get("code") if isinstance(req, dict) else "缺失"
        raise ContractError(f"QQ 音乐歌单请求被拒绝：req_1.code={req_code}")
    data = req.get("data")
    if not isinstance(data, dict):
        raise ContractError("QQ 音乐接口缺少 data 对象")
    raw_songs = data.get("songlist") or []
    if not isinstance(raw_songs, list):
        raise ContractError("QQ 音乐接口的 songlist 必须是数组")
    dirinfo = data.get("dirinfo")
    playlist_name = normalized_text(dirinfo.get("title")) if isinstance(dirinfo, dict) else ""
    total = data.get("total_song_num")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = 0
    hasmore_raw = data.get("hasmore")
    hasmore = bool(hasmore_raw) if isinstance(hasmore_raw, int) and not isinstance(hasmore_raw, bool) else None
    normalized: list[dict[str, Any]] = []
    for raw_song in raw_songs:
        if not isinstance(raw_song, dict):
            continue
        title = normalized_text(raw_song.get("name") or raw_song.get("title"))
        singers = [
            normalized_text(singer.get("name"))
            for singer in (raw_song.get("singer") or [])
            if isinstance(singer, dict) and normalized_text(singer.get("name"))
        ]
        if not title or not singers:
            continue
        song_mid = normalized_text(raw_song.get("mid"))
        song_id = normalized_text(raw_song.get("id"))
        album_value = raw_song.get("album")
        album = (
            normalized_text(album_value.get("name"))
            if isinstance(album_value, dict)
            else normalized_text(album_value)
        )
        links: dict[str, str] = {}
        if song_mid:
            links["qq_music"] = f"https://y.qq.com/n/ryqq/songDetail/{song_mid}"
        normalized.append(
            {
                "position": len(normalized) + 1,
                "title": title,
                "artist": singers[0],
                "artists": singers,
                "album": album,
                "duration": "",
                "platform_track_id": song_mid or song_id,
                "track_key": track_key(title, singers[0]),
                "links": links,
            }
        )
    return {
        "tracks": normalized,
        "total": total,
        "hasmore": hasmore,
        "raw_count": len(raw_songs),
        "playlist_name": playlist_name,
    }


class QQPublicPlaylistReader:
    """Read a *public* QQ Music playlist via the anonymous musicu gateway.

    No login state is used or accepted. The ``input_path`` argument is
    ignored; the playlist comes from ``playlist_id`` (numeric id or a
    y.qq.com share URL).
    """

    def read(
        self,
        input_path: Path | None,
        *,
        platform: str,
        playlist_id: str,
        playlist_name: str,
        declared_count: int | None = None,
        declared_count_file: Path | None = None,
    ) -> dict[str, Any]:
        playlist_arg = _playlist_id_from_qq_arg(playlist_id)
        collected: list[dict[str, Any]] = []
        raw_collected = 0
        total = 0
        api_playlist_name = ""
        first_page_bytes = b""
        for page_index in range(QQ_MAX_PAGES):
            raw = _fetch_qq_playlist_page(playlist_arg, page_index * QQ_PAGE_SIZE, QQ_PAGE_SIZE)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContractError(f"QQ 音乐接口响应不是有效 UTF-8 JSON：{exc}") from exc
            page = _parse_qq_diss_page(payload)
            if page_index == 0:
                first_page_bytes = raw
                api_playlist_name = page["playlist_name"]
            collected.extend(page["tracks"])
            raw_collected += page["raw_count"]
            total = page["total"] or total
            if page["hasmore"] is True:
                continue
            if page["hasmore"] is False:
                break
            # 接口未返回 hasmore 时按原始行数推断分页边界；
            # 坏行跳过导致的 declared 差异交给 incomplete 语义处理，
            # 绝不因收集数少于声明数而重复拉取已读页面。
            if not page["tracks"] or page["raw_count"] < QQ_PAGE_SIZE:
                break
            if total and raw_collected >= total:
                break
        declared = _declared_count(
            {"declared_track_count": total or len(collected)},
            explicit=declared_count,
            count_file=declared_count_file,
        )
        actual = len(collected)
        for position, track in enumerate(collected, 1):
            track["position"] = position
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": f"{platform}-{hashlib.sha256(first_page_bytes).hexdigest()[:16]}",
            "platform": platform,
            "playlist_id": playlist_arg,
            "playlist_name": api_playlist_name or normalized_text(playlist_name),
            "declared_track_count": declared,
            "track_count": actual,
            "reader_status": "complete" if declared == actual else "incomplete",
            "captured_at": utc_now(),
            "input_sha256": hashlib.sha256(first_page_bytes).hexdigest(),
            "reader": {
                "type": "qq_public",
                "source_playlist_id": playlist_arg,
                "fetched_pages": (actual + QQ_PAGE_SIZE - 1) // QQ_PAGE_SIZE or 1,
                "declared_count_source": (
                    "argument" if declared_count is not None else "api_total_song_num"
                ),
            },
            "tracks": collected,
        }


READERS: dict[str, type[PlaylistReader]] = {
    "local_json": LocalJsonReader,
    "apple_music_json": LocalJsonReader,
    "netease_json": NeteaseJsonReader,
    "netease_public": NeteasePublicPlaylistReader,
    "qq_public": QQPublicPlaylistReader,
    "csv": CsvPlaylistReader,
}


def build_snapshot(
    input_path: Path,
    *,
    reader_name: str,
    platform: str,
    playlist_id: str,
    playlist_name: str,
    declared_count: int | None = None,
    declared_count_file: Path | None = None,
) -> dict[str, Any]:
    reader_type = READERS.get(reader_name)
    if reader_type is None:
        raise ContractError(f"不支持的 Step 1 reader：{reader_name}")
    return reader_type().read(
        input_path,
        platform=platform,
        playlist_id=playlist_id,
        playlist_name=playlist_name,
        declared_count=declared_count,
        declared_count_file=declared_count_file,
    )


def save_snapshot(snapshot: dict[str, Any], output_path: Path) -> None:
    write_json(output_path, snapshot)
