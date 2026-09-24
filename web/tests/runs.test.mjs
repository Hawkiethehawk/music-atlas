import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

async function login(baseUrl, path, body) {
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const raw = response.headers.get("set-cookie") || "";
  const match = raw.match(/=([^;]+)/);
  return { status: response.status, payload: await response.json(), cookie: match ? raw.split(";")[0] : "" };
}

test("运行历史：写入、终态更新、用户自助与管理员查询", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "runs", authRequired: true });
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  const admin = store.createUser("RunAdmin", "admin-password-2026", "admin");
  const user = store.createUser("RunUser", "user-password-2026", "user");
  store.createRun({ jobId: "job-a", userId: user.id, platform: "netease_public", playlistId: "111", playlistName: "甲歌单", trackCount: 91, runtimeDir: "runtime/job-a" });
  store.updateRun("job-a", { status: "completed", analyzedCount: 91, recommendationCount: 10 });
  store.createRun({ jobId: "job-b", userId: user.id, platform: "qq_public", playlistId: "222", playlistName: "乙歌单", trackCount: 30, runtimeDir: "runtime/job-b" });
  store.updateRun("job-b", { status: "failed", error: "Agent 执行超时" });
  store.close();

  const userLogin = await login(server.baseUrl, "/api/auth/login", { username: "RunUser", password: "user-password-2026" });
  assert.equal(userLogin.status, 200);
  const mine = await fetch(`${server.baseUrl}/api/me/runs`, { headers: { cookie: userLogin.cookie } });
  assert.equal(mine.status, 200);
  const minePayload = await mine.json();
  assert.equal(minePayload.total, 2);
  assert.equal(minePayload.stats.completed, 1);
  assert.equal(minePayload.stats.failed, 1);
  const [latest] = minePayload.runs;
  assert.equal(latest.job_id, "job-b", "按开始时间倒序");
  assert.equal(latest.playlist_name, "乙歌单");
  assert.ok(latest.duration_ms !== null, "终态应记录耗时");

  const adminLogin = await login(server.baseUrl, "/api/admin/login", { username: "RunAdmin", password: "admin-password-2026" });
  assert.equal(adminLogin.status, 200);
  const byUser = await fetch(`${server.baseUrl}/api/admin/users/${user.id}/runs`, { headers: { cookie: adminLogin.cookie } });
  const byUserPayload = await byUser.json();
  assert.equal(byUserPayload.total, 2);
  assert.equal(byUserPayload.runs[0].playlist_id, "222");
  const all = await fetch(`${server.baseUrl}/api/admin/runs`, { headers: { cookie: adminLogin.cookie } });
  const allPayload = await all.json();
  assert.equal(allPayload.total, 2);
  assert.equal(allPayload.stats.total, 2);
  const filtered = await fetch(`${server.baseUrl}/api/admin/runs?status=failed`, { headers: { cookie: adminLogin.cookie } });
  assert.equal((await filtered.json()).total, 1);

  const anonymous = await fetch(`${server.baseUrl}/api/me/runs`);
  assert.equal(anonymous.status, 401, "未登录不能查看运行历史");
});

test("运行历史保留单组推荐数，同时从已完成报告标明三组总量", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "runs-groups", authRequired: true });
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  const user = store.createUser("RunGroups", "groups-password-2026", "user");
  const jobId = "20260924000000-1234abcd";
  const runtimeDir = path.join(server.runtimeDir, "jobs", jobId);
  await mkdir(runtimeDir, { recursive: true });
  await writeFile(path.join(runtimeDir, "web_job_report.json"), JSON.stringify({
    status: "completed", recommendation_groups: { count: 3, total_unique_recommendation_count: 30 },
    web_export: { recommendation_count: 10 },
  }), "utf8");
  store.createRun({ jobId, userId: user.id, runtimeDir });
  store.updateRun(jobId, { status: "completed", recommendationCount: 10 });
  store.close();
  const loginResponse = await login(server.baseUrl, "/api/auth/login", { username: "RunGroups", password: "groups-password-2026" });
  const response = await fetch(`${server.baseUrl}/api/me/runs`, { headers: { cookie: loginResponse.cookie } });
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(payload.runs[0].recommendation_count, 10);
  assert.equal(payload.runs[0].recommendation_group_count, 3);
  assert.equal(payload.runs[0].total_unique_recommendation_count, 30);
});
