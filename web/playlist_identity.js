"use strict";

const crypto = require("node:crypto");

const SOURCE_BY_HOST = Object.freeze({
  "music.apple.com": { kind: "apple_music", platform: "apple_music" },
  "music.163.com": { kind: "netease_public", platform: "netease" },
  "163cn.tv": { kind: "netease_public", platform: "netease" },
  "www.163cn.tv": { kind: "netease_public", platform: "netease" },
  "y.qq.com": { kind: "qq_public", platform: "qq_music" },
  "i.y.qq.com": { kind: "qq_public", platform: "qq_music" },
});
const NETEASE_SHORT_HOSTS = new Set(["163cn.tv", "www.163cn.tv"]);

function extractSourceCandidates(value) {
  const text = String(value || "").trim();
  const matches = text.match(/https?:\/\/[^\s"'<>，。；、」』）】]+/g) || [];
  return matches
    .map((item) => item.replace(/[)\]）】}>,.;；，。」』]+$/g, ""))
    .filter(Boolean);
}

function extractPlaylistName(raw, url) {
  const text = String(raw || "").trim();
  const before = (url ? text.split(url)[0] : text).trim();
  const index = Math.max(before.lastIndexOf(":"), before.lastIndexOf("："));
  if (index < 0) return "";
  const name = before.slice(index + 1).trim();
  return name && name.length <= 60 ? name : "";
}

function parseSource(value) {
  const raw = String(value || "").trim();
  if (!raw) throw new Error("请提交歌单公开链接");
  const candidates = extractSourceCandidates(raw);
  const attempts = candidates.length ? candidates : [raw];
  for (const candidate of attempts) {
    const normalized = candidate.replace(/^http:\/\//i, "https://");
    let parsed;
    try { parsed = new URL(normalized); } catch { continue; }
    if (parsed.protocol !== "https:") continue;
    parsed.hostname = parsed.hostname.toLowerCase().replace(/\.$/, "");
    const source = SOURCE_BY_HOST[parsed.hostname];
    if (!source) continue;
    return {
      ...source,
      source_url: parsed.toString(),
      playlist_name: extractPlaylistName(raw, candidate),
    };
  }
  throw new Error("只支持 Apple Music、网易云音乐或 QQ 音乐公开歌单链接");
}

function neteasePlaylistId(value) {
  let parsed;
  try { parsed = new URL(String(value || "")); } catch { return ""; }
  for (const query of [parsed.search.slice(1), parsed.hash.replace(/^#\/?/, "")]) {
    const candidate = new URLSearchParams(query.includes("?") ? query.slice(query.indexOf("?") + 1) : query).get("id") || "";
    if (/^\d+$/.test(candidate)) return candidate;
  }
  const match = String(value || "").match(/\/playlist\/(\d+)|[?&#]id=(\d+)/i);
  return match ? (match[1] || match[2] || "") : "";
}

function applePlaylistId(parsed) {
  const parts = parsed.pathname.split("/").filter(Boolean);
  return [...parts].reverse().find((part) => /^pl\.[A-Za-z0-9.-]+$/.test(part)) || "";
}

function qqPlaylistId(parsed) {
  for (const key of ["id", "disstid", "playlistId", "playlistid"]) {
    const value = parsed.searchParams.get(key) || "";
    if (/^[A-Za-z0-9_-]+$/.test(value)) return value;
  }
  const match = parsed.pathname.match(/\/(?:playlist|taoge)\/(?:detail\/)?([A-Za-z0-9_-]+)/i);
  return match ? match[1] : "";
}

function fallbackKey(platform, sourceUrl) {
  const digest = crypto.createHash("sha256").update(sourceUrl).digest("hex").slice(0, 24);
  return `${platform || "url"}:url:${digest}`;
}

async function resolveNeteaseShortLink(sourceUrl, { fetchImpl = globalThis.fetch, timeoutMs = 6000 } = {}) {
  if (typeof fetchImpl !== "function") throw new Error("当前 Node 运行时不支持短链解析");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let current = new URL(sourceUrl);
  try {
    for (let hop = 0; hop < 5; hop += 1) {
      const host = current.hostname.toLowerCase().replace(/\.$/, "");
      if (current.protocol !== "https:" || (!NETEASE_SHORT_HOSTS.has(host) && host !== "music.163.com")) {
        throw new Error("网易云短链跳转到了不受支持的地址");
      }
      if (host === "music.163.com") return current.toString();
      const response = await fetchImpl(current, {
        method: "GET",
        redirect: "manual",
        signal: controller.signal,
        headers: { "User-Agent": "MusicAtlas/1.0", Referer: "https://music.163.com/" },
      });
      if (response.status < 300 || response.status >= 400) throw new Error(`网易云短链返回 ${response.status}`);
      const location = response.headers.get("location");
      if (!location) throw new Error("网易云短链没有返回跳转地址");
      current = new URL(location, current);
    }
    throw new Error("网易云短链跳转次数过多");
  } finally {
    clearTimeout(timer);
  }
}

async function canonicalizePlaylistSource(value, options = {}) {
  const parsedSource = parseSource(value);
  let parsed = new URL(parsedSource.source_url);
  let playlistId = "";
  let canonicalUrl = parsedSource.source_url;

  if (parsedSource.platform === "netease") {
    if (NETEASE_SHORT_HOSTS.has(parsed.hostname)) {
      canonicalUrl = await resolveNeteaseShortLink(parsed.toString(), options);
      parsed = new URL(canonicalUrl);
    }
    playlistId = neteasePlaylistId(parsed.toString());
    if (!playlistId) throw new Error("无法从网易云链接识别歌单 ID");
    canonicalUrl = `https://music.163.com/playlist?id=${playlistId}`;
  } else if (parsedSource.platform === "apple_music") {
    playlistId = applePlaylistId(parsed);
    parsed.hash = "";
    canonicalUrl = parsed.toString();
  } else if (parsedSource.platform === "qq_music") {
    playlistId = qqPlaylistId(parsed);
    parsed.hash = "";
    canonicalUrl = parsed.toString();
  }

  return {
    ...parsedSource,
    source_url: canonicalUrl,
    canonical_url: canonicalUrl,
    canonical_key: playlistId ? `${parsedSource.platform}:${playlistId}` : fallbackKey(parsedSource.platform, canonicalUrl),
    playlist_id: playlistId,
  };
}

async function canonicalizePlaylistSourceSafe(value, options = {}) {
  const parsedSource = parseSource(value);
  try {
    return await canonicalizePlaylistSource(value, options);
  } catch (error) {
    return {
      ...parsedSource,
      canonical_url: parsedSource.source_url,
      canonical_key: fallbackKey(parsedSource.platform, parsedSource.source_url),
      playlist_id: "",
      canonical_error: error && error.message ? error.message : "歌单链接规范化失败",
    };
  }
}

module.exports = {
  SOURCE_BY_HOST,
  extractSourceCandidates,
  extractPlaylistName,
  parseSource,
  neteasePlaylistId,
  resolveNeteaseShortLink,
  canonicalizePlaylistSource,
  canonicalizePlaylistSourceSafe,
};
