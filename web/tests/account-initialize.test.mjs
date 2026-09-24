import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

const headers = { "content-type": "application/json" };
const cookie = (response, name) => `${name}=${(response.headers.get("set-cookie") || "").match(new RegExp(`${name}=([^;]+)`))?.[1] || ""}`;

async function login(baseUrl, username, password, admin = false) {
  const response = await fetch(`${baseUrl}/api/${admin ? "admin/login" : "auth/login"}`, {
    method: "POST", headers, body: JSON.stringify({ username, password }),
  });
  assert.equal(response.status, 200);
  return cookie(response, admin ? "atlas_admin_session" : "atlas_session");
}

test("初始化只允许管理员，准确备份并清空目标账号业务数据，其他账号不变", { concurrency: false }, async (t) => {
  let targetId, otherId, createdAt;
  const server = await createIsolatedServer({ label: "initialize", authRequired: true, beforeStart: ({ authDbPath }) => {
    const store = createAuthStore(authDbPath);
    targetId = store.createUser("ResetTarget", "user-password-2026", "user").id;
    otherId = store.createUser("KeepOther", "user-password-2026", "user").id;
    store.createUser("ResetAdmin", "admin-password-2026", "admin");
    createdAt = store.userResetSnapshot(targetId).user.created_at;
    store.savePreferences(targetId, { mood: "rock" });
    store.upsertPlaylist(targetId, { source_url: "https://music.example/target", name: "目标歌单" });
    store.createRun({ jobId: "target-job", userId: targetId, platform: "netease_public" });
    store.updateRun("target-job", { status: "completed" });
    store.savePreferences(otherId, { mood: "jazz" });
    store.upsertPlaylist(otherId, { source_url: "https://music.example/other", name: "其他歌单" });
    store.createRun({ jobId: "other-job", userId: otherId, platform: "qq_public" });
    store.updateRun("other-job", { status: "completed" });
    store.close();
  } });
  t.after(() => server.stop());
  const { baseUrl, runtimeDir } = server;
  const atlasDir = path.join(runtimeDir, "users", String(targetId));
  const jobDir = path.join(runtimeDir, "jobs", "target-job");
  const historyDir = path.join(runtimeDir, "recommendation-history");
  fs.mkdirSync(atlasDir, { recursive: true });
  fs.writeFileSync(path.join(atlasDir, "current.json"), JSON.stringify({ user_id: targetId }));
  fs.mkdirSync(jobDir, { recursive: true });
  fs.writeFileSync(path.join(jobDir, "web_job_state.json"), JSON.stringify({ id: "target-job", user_id: targetId, status: "completed", events: [] }));
  fs.mkdirSync(historyDir, { recursive: true });
  fs.writeFileSync(path.join(historyDir, "target.json"), JSON.stringify({ user_id: String(targetId), playlist: { kind: "netease_public" }, entries: [] }));
  fs.writeFileSync(path.join(historyDir, "other.json"), JSON.stringify({ user_id: String(otherId), playlist: { kind: "qq_public" }, entries: [] }));

  const userCookie = await login(baseUrl, "ResetTarget", "user-password-2026");
  const adminCookie = await login(baseUrl, "ResetAdmin", "admin-password-2026", true);
  const endpoint = `${baseUrl}/api/admin/users/${targetId}/initialize`;
  assert.equal((await fetch(endpoint)).status, 404);
  assert.equal((await fetch(endpoint, { headers: { cookie: userCookie } })).status, 404);
  const preview = await (await fetch(endpoint, { headers: { cookie: adminCookie } })).json();
  assert.deepEqual({ ...preview.summary }, { playlists: 1, runs: 1, preferences: 1, sessions: 1, job_directories: 1, history_files: 1, atlas_present: true, active_jobs: 0 });
  const post = (confirm_username, confirmation_token) => fetch(endpoint, { method: "POST", headers: { ...headers, cookie: adminCookie }, body: JSON.stringify({ confirm_username, confirmation_token }) });
  assert.equal((await post("wrong", preview.confirmation_token)).status, 409);
  assert.equal((await post("ResetTarget", "stale-token")).status, 409);
  assert.equal(fs.existsSync(atlasDir), true);
  const resultResponse = await post("ResetTarget", preview.confirmation_token);
  assert.equal(resultResponse.status, 200);
  const result = await resultResponse.json();
  assert.equal(result.user.username, "ResetTarget");
  assert.equal(result.user.created_at, createdAt);
  assert.equal(result.user.role, "user");
  assert.equal(result.user.status, "enabled");
  assert.equal(result.user.last_login_at, null);
  const archive = path.join(runtimeDir, "backups", "account-reset", result.archive_id);
  assert.equal(fs.existsSync(path.join(archive, "manifest.json")), true);
  assert.equal(fs.existsSync(path.join(archive, "atlas", String(targetId), "current.json")), true);
  assert.equal(fs.existsSync(path.join(archive, "jobs", "target-job", "web_job_state.json")), true);
  assert.equal(fs.existsSync(path.join(archive, "history", "target.json")), true);
  assert.equal(fs.existsSync(atlasDir), false);
  assert.equal(fs.existsSync(jobDir), false);
  assert.equal(fs.existsSync(path.join(historyDir, "target.json")), false);
  assert.equal(fs.existsSync(path.join(historyDir, "other.json")), true);
  const oldSession = await (await fetch(`${baseUrl}/api/auth/me`, { headers: { cookie: userCookie } })).json();
  assert.equal(oldSession.user, null);
  const freshCookie = await login(baseUrl, "ResetTarget", "user-password-2026");
  assert.ok(freshCookie);

  const store = createAuthStore(server.authDbPath);
  t.after(() => store.close());
  assert.deepEqual(store.getPreferences(targetId), {});
  assert.deepEqual(store.listPlaylists(targetId), []);
  assert.equal(store.listRuns({ userId: targetId }).total, 0);
  assert.deepEqual(store.getPreferences(otherId), { mood: "jazz" });
  assert.equal(store.listPlaylists(otherId).length, 1);
  assert.equal(store.listRuns({ userId: otherId }).total, 1);
  assert.equal(store.userResetSnapshot(targetId).session_count, 1); // 仅新登录会话
  const auditDb = new DatabaseSync(server.authDbPath);
  assert.equal(auditDb.prepare("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'admin.user.initialize'").get().n, 1);
  auditDb.close();
});

test("管理员可初始化自己，旧管理会话立即失效且原密码可重新登录", { concurrency: false }, async (t) => {
  let adminId;
  const server = await createIsolatedServer({ label: "initialize-self", authRequired: true, beforeStart: ({ authDbPath }) => {
    const store = createAuthStore(authDbPath);
    adminId = store.createUser("SelfAdmin", "admin-password-2026", "admin").id;
    store.savePreferences(adminId, { source: "self" });
    store.close();
  } });
  t.after(() => server.stop());
  const oldCookie = await login(server.baseUrl, "SelfAdmin", "admin-password-2026", true);
  const endpoint = `${server.baseUrl}/api/admin/users/${adminId}/initialize`;
  const preview = await (await fetch(endpoint, { headers: { cookie: oldCookie } })).json();
  const response = await fetch(endpoint, { method: "POST", headers: { ...headers, cookie: oldCookie }, body: JSON.stringify({ confirm_username: "SelfAdmin", confirmation_token: preview.confirmation_token }) });
  assert.equal(response.status, 200);
  assert.equal((await fetch(`${server.baseUrl}/api/admin/users`, { headers: { cookie: oldCookie } })).status, 404);
  assert.ok(await login(server.baseUrl, "SelfAdmin", "admin-password-2026", true));
});
