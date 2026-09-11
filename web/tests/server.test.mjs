/**
 * server.js 协议层测试（纯 Node，不依赖浏览器）。
 * 覆盖静态服务、元数据接口、错误路径与执行器未配置时的拒绝行为。
 */

import test from "node:test";
import assert from "node:assert/strict";
import { createIsolatedServer } from "./helpers.mjs";

test("静态服务：首页、404 页面与 /data 禁止访问", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "static" });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const home = await fetch(`${baseUrl}/`);
  assert.equal(home.status, 200);
  assert.match(home.headers.get("content-type"), /text\/html/);
  const html = await home.text();
  assert.match(html, /<html/i);

  const missing = await fetch(`${baseUrl}/no-such-page.js`);
  assert.equal(missing.status, 404);
  assert.match(missing.headers.get("content-type"), /text\/html/);

  const data = await fetch(`${baseUrl}/data/current.json`);
  assert.equal(data.status, 403);
});

test("元数据接口：/api/config 与 /api/health", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "meta" });
  t.after(() => server.stop());
  const { baseUrl, runtimeDir } = server;

  const configResponse = await fetch(`${baseUrl}/api/config`);
  assert.equal(configResponse.status, 200);
  const config = await configResponse.json();
  assert.equal(config.ok, true);
  assert.equal(config.workflow.analysis_parallelism, 5);
  assert.equal(config.workflow.recommendation_parallelism, 4);
  assert.equal(config.workflow.analysis_executor_configured, true);
  assert.equal(config.workflow.recommendation_executor_configured, true);
  assert.ok(config.paths.published.includes("runtime"), "发布路径应位于 runtime 隔离目录");
  assert.match(config.paths.jobs, /web-e2e-meta/);

  // 未发布数据时 health 为 503，data_available=false，且服务本身可用。
  const health = await fetch(`${baseUrl}/api/health`);
  assert.equal(health.status, 503);
  const healthPayload = await health.json();
  assert.equal(healthPayload.ok, false);
  assert.equal(healthPayload.data_available, false);

  // 发布一份最小 payload 后 health 恢复 200（验证 published 路径与 payload 校验）。
  const { writeFile, mkdir } = await import("node:fs/promises");
  await mkdir(runtimeDir, { recursive: true });
  await writeFile(
    `${runtimeDir}/current.json`,
    JSON.stringify({ payload_type: "music_atlas_web" }),
    "utf8",
  );
  const okHealth = await fetch(`${baseUrl}/api/health`);
  assert.equal(okHealth.status, 200);
  assert.equal((await okHealth.json()).data_available, true);
});

test("任务接口错误路径：非法提交与未知任务", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "errors" });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const empty = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  });
  assert.equal(empty.status, 400);
  assert.match((await empty.json()).error, /歌单公开链接/);

  const unsupported = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: "https://example.com/playlist" }),
  });
  assert.equal(unsupported.status, 400);
  assert.match((await unsupported.json()).error, /只支持/);

  const insecure = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: "http://music.163.com/playlist?id=1" }),
  });
  assert.equal(insecure.status, 400);

  const unknownJob = await fetch(`${baseUrl}/api/jobs/does-not-exist`);
  assert.equal(unknownJob.status, 404);
  const unknownEvents = await fetch(`${baseUrl}/api/jobs/does-not-exist/events`);
  assert.equal(unknownEvents.status, 404);
});

test("执行器未配置时拒绝创建任务（503）", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "noexec", executors: false });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const config = await (await fetch(`${baseUrl}/api/config`)).json();
  assert.equal(config.workflow.analysis_executor_configured, false);

  const rejected = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: "https://music.163.com/playlist?id=123456" }),
  });
  assert.equal(rejected.status, 503);
  assert.match((await rejected.json()).error, /暂不可生成推荐/);
});
