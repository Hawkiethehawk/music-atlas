#!/usr/bin/env python3
"""Step 1 source adapters and normalization into PlaylistSnapshot."""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

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


READERS: dict[str, type[PlaylistReader]] = {
    "local_json": LocalJsonReader,
    "apple_music_json": LocalJsonReader,
    "netease_json": NeteaseJsonReader,
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
