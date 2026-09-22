import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import identityModule from "../playlist_identity.js";
import { createAuthStore } from "../auth_store.js";

const { canonicalizePlaylistSource, canonicalizePlaylistSourceSafe } = identityModule;
const PLAYLIST_ID = "18135667715";

function shortRedirectFetch() {
  return Promise.resolve({
    status: 302,
    headers: new Headers({ location: `https://music.163.com/m/playlist?userid=1&id=${PLAYLIST_ID}&app_version=9` }),
  });
}

test("网易云长链、hash 链接和短链归一到同一歌单身份", async () => {
  const sources = [
    `https://music.163.com/playlist?id=${PLAYLIST_ID}`,
    `https://music.163.com/#/playlist?userid=1&id=${PLAYLIST_ID}`,
    "https://163cn.tv/bgSP16lq",
  ];
  const rows = await Promise.all(sources.map((source) => canonicalizePlaylistSource(source, { fetchImpl: shortRedirectFetch })));
  assert.deepEqual(new Set(rows.map((row) => row.canonical_key)), new Set([`netease:${PLAYLIST_ID}`]));
  assert.deepEqual(new Set(rows.map((row) => row.canonical_url)), new Set([`https://music.163.com/playlist?id=${PLAYLIST_ID}`]));
});

test("短链解析失败保留独立 URL 身份，不误合并", async () => {
  const failed = async () => { throw new Error("offline"); };
  const first = await canonicalizePlaylistSourceSafe("https://163cn.tv/first", { fetchImpl: failed });
  const second = await canonicalizePlaylistSourceSafe("https://163cn.tv/second", { fetchImpl: failed });
  assert.notEqual(first.canonical_key, second.canonical_key);
  assert.match(first.canonical_key, /^netease:url:/);
});

test("旧 playlists 表自动迁移；同用户重复身份合并，不跨用户合并", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "atlas-playlist-migration-"));
  const dbPath = path.join(dir, "auth.sqlite");
  const legacy = new DatabaseSync(dbPath);
  legacy.exec(`CREATE TABLE playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, source_url TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(user_id, source_url));`);
  legacy.prepare("INSERT INTO playlists (user_id, source_url, name, platform, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)")
    .run(1, `https://music.163.com/#/playlist?id=${PLAYLIST_ID}`, "Liminal old", "netease", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z");
  legacy.prepare("INSERT INTO playlists (user_id, source_url, name, platform, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)")
    .run(1, "https://163cn.tv/bgSP16lq", "Liminal", "netease", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z");
  legacy.prepare("INSERT INTO playlists (user_id, source_url, name, platform, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)")
    .run(2, "https://163cn.tv/bgSP16lq", "Other user", "netease", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z");
  legacy.close();

  const store = createAuthStore(dbPath);
  t.after(async () => { store.close(); await fs.rm(dir, { recursive: true, force: true }); });
  const normalize = (source) => canonicalizePlaylistSource(source, { fetchImpl: shortRedirectFetch });
  const result = await store.reconcilePlaylists(normalize);
  assert.equal(result.merged, 1);
  const firstUser = store.listPlaylists(1);
  assert.equal(firstUser.length, 1);
  assert.equal(firstUser[0].canonical_key, `netease:${PLAYLIST_ID}`);
  assert.equal(firstUser[0].source_url, `https://music.163.com/playlist?id=${PLAYLIST_ID}`);
  assert.equal(firstUser[0].name, "Liminal");
  assert.equal(firstUser[0].created_at, "2026-01-01T00:00:00Z");
  assert.equal(store.listPlaylists(2).length, 1);
});
