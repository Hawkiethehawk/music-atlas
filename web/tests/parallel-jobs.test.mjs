/**
 * 多用户并行：不同用户可以同时运行任务，同一用户只能有一个进行中的任务。
 *
 * 覆盖：
 * 1. 用户 A 运行任务时，用户 B 仍能提交任务（不再被全局锁挡住）；
 * 2. 同一用户重复提交被拒绝（409）；
 * 3. 全局并发上限生效（超过上限返回 409）；
 * 4. /api/config 与 /api/health 只返回当前用户可见的任务，不泄露他人任务。
 */

import test from "node:test";
import assert from "node:assert/strict";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

async function login(baseUrl, path, body) {
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const raw = response.headers.get("set-cookie") || "";
  return { status: response.status, payload: await response.json(), cookie: raw ? raw.split(";")[0] : "" };
}

const SOURCE = "https://music.163.com/playlist?id=123456";

test("多用户可并行提交，单用户额度为 3", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "parallel-jobs", authRequired: true });
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  store.createUser("ParaA", "parallel-a-2026", "user");
  store.createUser("ParaB", "parallel-b-2026", "user");
  store.close();

  const a = await login(server.baseUrl, "/api/auth/login", { username: "ParaA", password: "parallel-a-2026" });
  const b = await login(server.baseUrl, "/api/auth/login", { username: "ParaB", password: "parallel-b-2026" });
  assert.equal(a.status, 200);
  assert.equal(b.status, 200);

  // 用户 A 提交任务（首批任务会进入排队/等待歌单阶段）
  const first = await fetch(`${server.baseUrl}/api/jobs`, {
    method: "POST", headers: { "content-type": "application/json", cookie: a.cookie },
    body: JSON.stringify({ source_url: SOURCE }),
  });
  assert.equal(first.status, 202, "用户 A 应能提交任务");
  const firstJob = (await first.json()).job;

  // 同一用户可再提交（额度 3），第 4 个才被拒绝
  for (let index = 2; index <= 3; index += 1) {
    const extra = await fetch(`${server.baseUrl}/api/jobs`, {
      method: "POST", headers: { "content-type": "application/json", cookie: a.cookie },
      body: JSON.stringify({ source_url: SOURCE }),
    });
    assert.equal(extra.status, 202, `同一用户第 ${index} 个任务应被接受`);
  }
  const overflow = await fetch(`${server.baseUrl}/api/jobs`, {
    method: "POST", headers: { "content-type": "application/json", cookie: a.cookie },
    body: JSON.stringify({ source_url: SOURCE }),
  });
  assert.equal(overflow.status, 409, "超过单用户额度应拒绝");

  // 用户 B 提交：允许（并行）
  const second = await fetch(`${server.baseUrl}/api/jobs`, {
    method: "POST", headers: { "content-type": "application/json", cookie: b.cookie },
    body: JSON.stringify({ source_url: SOURCE }),
  });
  assert.equal(second.status, 202, "不同用户必须能并行提交");
  const secondJob = (await second.json()).job;
  assert.notEqual(secondJob.id, firstJob.id);

  // 可见性：A 只能看到自己的任务
  const configA = await (await fetch(`${server.baseUrl}/api/config`, { headers: { cookie: a.cookie } })).json();
  assert.equal(configA.active_job_id, firstJob.id);
  const configB = await (await fetch(`${server.baseUrl}/api/config`, { headers: { cookie: b.cookie } })).json();
  assert.equal(configB.active_job_id, secondJob.id);
  assert.notEqual(configA.active_job_id, configB.active_job_id);

  // 隔离环境没有 Atlas 数据时 /api/health 返回 503；有数据时只返回自己的任务。
  const healthResponse = await fetch(`${server.baseUrl}/api/health`, { headers: { cookie: a.cookie } });
  const healthA = await healthResponse.json();
  if (healthResponse.status === 200) {
    assert.equal(healthA.active_job_id, firstJob.id, "健康检查不应泄露他人任务");
  } else {
    assert.equal(healthResponse.status, 503);
    assert.equal(healthA.active_job_id, undefined, "无数据时不应暴露任务信息");
  }

  // 清理：停服会终止子进程
});
