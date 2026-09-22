"""Public playlist URL validation and canonical identity resolution."""

from __future__ import annotations

import hashlib
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from contracts import ContractError


SOURCE_KINDS = {"apple_music", "netease_public", "qq_public", "local_json", "csv"}
PUBLIC_HOSTS = {
    "apple_music": {"music.apple.com"},
    "netease_public": {"music.163.com", "163cn.tv", "www.163cn.tv"},
    "qq_public": {"y.qq.com", "i.y.qq.com"},
}
NETEASE_SHORT_HOSTS = {"163cn.tv", "www.163cn.tv"}


def validate_url(kind: str, value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or hostname not in PUBLIC_HOSTS[kind]:
        raise ContractError(f"{kind} 只接受受支持平台的 HTTPS 公开链接")
    return url


def netease_playlist_id_from_url(value: str) -> str:
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


def resolve_netease_source(url: str) -> tuple[str, str]:
    """Resolve a music.163.com URL or 163cn.tv short link to canonical URL."""

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if hostname == "music.163.com":
        playlist_id = netease_playlist_id_from_url(url)
        return playlist_id, f"https://music.163.com/playlist?id={playlist_id}"
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
    playlist_id = netease_playlist_id_from_url(resolved_url)
    return playlist_id, f"https://music.163.com/playlist?id={playlist_id}"


def source_id(kind: str, value: str) -> str:
    return f"{kind}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"
