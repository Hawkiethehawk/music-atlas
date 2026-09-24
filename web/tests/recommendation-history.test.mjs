import test from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

function writeHistory(runtimeDir, name, payload) {
  const dir = path.join(runtimeDir, "recommendation-history");
  mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, name), JSON.stringify(payload, null, 2) + "\n", "utf8");
}

async function login(baseUrl, pathName, body) {
  const response = await fetch(`${baseUrl}${pathName}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const raw = response.headers.get("set-cookie") || "";
  return { status: response.status, payload: await response.json(), cookie: raw ? raw.split(";")[0] : "" };
}

const now = () => new Date().toISOString();
const old = () => new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString();

test("推荐去重缓存：列表、三层清除与权限边界", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "history", authRequired: true });
  t.after(() => server.stop());
  const runtimeDir = path.dirname(server.authDbPath);
  const store = createAuthStore(server.authDbPath);
  const user = store.createUser("HistUser", "user-password-2026", "user");
  const other = store.createUser("OtherUser", "other-password-2026", "user");
  store.createUser("HistAdmin", "admin-password-2026", "admin");
  store.close();

  writeHistory(runtimeDir, "alpha.json", {
    schema_version: "1.0", user_id: String(user.id), retention_days: 7,
    playlist: { kind: "netease_public", playlist_id: "111", name: "甲歌单" },
    entries: [
      { generated_at: now(), canonical_track_ids: ["a1", "a2"], track_keys: ["k1", "k2"] },
      { generated_at: old(), canonical_track_ids: ["a3"], track_keys: ["k3"] },
    ],
  });
  writeHistory(runtimeDir, "beta.json", {
    schema_version: "1.0", user_id: String(other.id), retention_days: 7,
    playlist: { kind: "qq_public", playlist_id: "222" },
    entries: [{ generated_at: now(), canonical_track_ids: ["b1"], track_keys: ["kb1"] }],
  });

  const adminLogin = await login(server.baseUrl, "/api/admin/login", { username: "HistAdmin", password: "admin-password-2026" });
  const userLogin = await login(server.baseUrl, "/api/auth/login", { username: "HistUser", password: "user-password-2026" });
  const adminHeaders = { cookie: adminLogin.cookie, "content-type": "application/json" };
  const userHeaders = { cookie: userLogin.cookie, "content-type": "application/json" };

  // 管理端列表：关联用户名；过期条目不计入活跃
  const listed = await fetch(`${server.baseUrl}/api/admin/recommendation-history`, { headers: adminHeaders });
  const listPayload = await listed.json();
  assert.equal(listPayload.items.length, 2);
  const alpha = listPayload.items.find((item) => item.file === "alpha.json");
  assert.equal(alpha.username, "HistUser");
  assert.equal(alpha.playlist.playlist_id, "111");
  assert.equal(alpha.total_entries, 2);
  assert.equal(alpha.active_entries, 1);
  assert.equal(alpha.excluded_tracks, 2, "只统计有效条目的排除曲目数");
  assert.equal(alpha.entries.length, 2);

  // ① 单次记录清除
  const stamp = alpha.entries[0].generated_at;
  const removedEntry = await fetch(`${server.baseUrl}/api/admin/recommendation-history/alpha.json/entries/${encodeURIComponent(stamp)}`, { method: "DELETE", headers: adminHeaders });
  assert.equal(removedEntry.status, 200);
  assert.equal((await removedEntry.json()).removed, 1);

  // ② 清理过期条目
  const pruned = await fetch(`${server.baseUrl}/api/admin/recommendation-history/alpha.json/prune`, { method: "POST", headers: adminHeaders });
  const prunePayload = await pruned.json();
  assert.equal(pruned.status, 200);
  assert.equal(prunePayload.removed, 1);
  assert.equal(prunePayload.kept, 0);

  // 用户自助：只看得到自己的
  const mine = await fetch(`${server.baseUrl}/api/me/recommendation-history`, { headers: userHeaders });
  const minePayload = await mine.json();
  assert.equal(minePayload.items.length, 1);
  assert.equal(minePayload.items[0].file, "alpha.json");

  // 用户不能清别人的
  const forbidden = await fetch(`${server.baseUrl}/api/me/recommendation-history/beta.json`, { method: "DELETE", headers: userHeaders });
  assert.equal(forbidden.status, 403);

  // ③ 用户清除自己的整个歌单缓存
  const cleared = await fetch(`${server.baseUrl}/api/me/recommendation-history/alpha.json`, { method: "DELETE", headers: userHeaders });
  assert.equal(cleared.status, 200);

  // 管理端清除剩余记录
  const adminClear = await fetch(`${server.baseUrl}/api/admin/recommendation-history/beta.json`, { method: "DELETE", headers: adminHeaders });
  assert.equal(adminClear.status, 200);
  const finalList = await fetch(`${server.baseUrl}/api/admin/recommendation-history`, { headers: adminHeaders });
  assert.equal((await finalList.json()).items.length, 0);

  // 未登录不能访问
  const anonymous = await fetch(`${server.baseUrl}/api/me/recommendation-history`);
  assert.equal(anonymous.status, 401);
});

test("管理员按账户、平台或全部清空：维度独立、权限与范围防误清", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "history-bulk", authRequired: true });
  t.after(() => server.stop());
  const runtimeDir = path.dirname(server.authDbPath);
  const store = createAuthStore(server.authDbPath);
  const first = store.createUser("ScopeFirst", "user-password-2026", "user");
  const second = store.createUser("ScopeSecond", "user-password-2026", "user");
  store.createUser("ScopeAdmin", "admin-password-2026", "admin");
  store.close();
  for (const [name, userId, kind] of [
    ["first-netease.json", first.id, "netease_public"],
    ["first-qq.json", first.id, "qq_public"],
    ["second-netease.json", second.id, "netease_public"],
    ["second-apple.json", second.id, "apple_music"],
  ]) writeHistory(runtimeDir, name, {
    schema_version: "1.0", user_id: String(userId), retention_days: 7,
    playlist: { kind, playlist_id: name },
    entries: [{ generated_at: now(), canonical_track_ids: [name], track_keys: [name] }],
  });
  const adminLogin = await login(server.baseUrl, "/api/admin/login", { username: "ScopeAdmin", password: "admin-password-2026" });
  const userLogin = await login(server.baseUrl, "/api/auth/login", { username: "ScopeFirst", password: "user-password-2026" });
  const adminHeaders = { cookie: adminLogin.cookie, "content-type": "application/json" };
  const userHeaders = { cookie: userLogin.cookie, "content-type": "application/json" };
  const list = async () => (await (await fetch(`${server.baseUrl}/api/admin/recommendation-history`, { headers: adminHeaders })).json()).items;
  const clear = (body, headers = adminHeaders) => fetch(`${server.baseUrl}/api/admin/recommendation-history/bulk`, {
    method: "DELETE", headers, body: JSON.stringify(body),
  });
  const initial = await list();
  assert.equal(initial.length, 4);
  assert.deepEqual(initial.filter(item => item.platform === "netease").map(item => item.file).sort(),
    ["first-netease.json", "second-netease.json"]);
  assert.equal((await clear({ scope: "all", expected_files: initial.map(item => item.file), expected_entries: 4 }, userHeaders)).status, 404);
  assert.equal((await clear({ scope: "all", expected_files: ["first-qq.json"], expected_entries: 4 })).status, 409);
  assert.equal((await clear({ scope: "all", expected_files: initial.map(item => item.file), expected_entries: 3 })).status, 409);
  assert.equal((await clear({ scope: "account", user_id: "", expected_files: [], expected_entries: 0 })).status, 400);

  const accountFiles = initial.filter(item => item.user_id === String(first.id)).map(item => item.file);
  const accountResult = await clear({ scope: "account", user_id: String(first.id), expected_files: accountFiles, expected_entries: 2 });
  assert.equal(accountResult.status, 200);
  assert.equal((await accountResult.json()).removed_files, 2);
  assert.deepEqual((await list()).map(item => item.file).sort(), ["second-apple.json", "second-netease.json"]);

  const platformResult = await clear({ scope: "platform", platform: "netease", expected_files: ["second-netease.json"], expected_entries: 1 });
  assert.equal(platformResult.status, 200);
  assert.equal((await platformResult.json()).removed_entries, 1);
  assert.deepEqual((await list()).map(item => item.file), ["second-apple.json"]);

  const allResult = await clear({ scope: "all", expected_files: ["second-apple.json"], expected_entries: 1 });
  assert.equal(allResult.status, 200);
  assert.equal((await list()).length, 0);
});
