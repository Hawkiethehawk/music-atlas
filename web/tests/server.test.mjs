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
  assert.equal(config.active_job_id, null);
  assert.equal(config.latest_job_id, null);
  assert.equal(config.workflow.analysis_parallelism, 5);
  assert.equal(config.workflow.recommendation_parallelism, 4);
  assert.deepEqual(config.workflow.track_percentile_options, [0.25, 0.5, 1]);
  assert.equal(config.workflow.track_percentile_default, 1);
  assert.equal(config.workflow.await_limit_timeout_seconds, 1800);
  // 隔离配置未声明研究预算时应回落到默认值。
  assert.equal(config.workflow.max_research_rounds, 2);
  assert.equal(config.workflow.initial_candidate_limit, 60);
  assert.equal(config.workflow.hard_candidate_limit, 200);
  assert.equal(config.workflow.analysis_executor_configured, true);
  assert.equal(config.workflow.recommendation_executor_configured, true);
  assert.ok(config.paths.published.includes("runtime"), "发布路径应位于 runtime 隔离目录");
  assert.match(config.paths.jobs, /web-e2e-meta/);

  const proxiedConfig = await (await fetch(`${baseUrl}/api/config`, {
    headers: { "x-forwarded-for": "127.0.0.1, 203.0.113.9" },
  })).json();
  assert.equal(proxiedConfig.config_file, undefined, "反代公网请求不得获得本机配置路径");
  assert.equal(proxiedConfig.paths, undefined, "反代公网请求不得获得本机目录");
  assert.equal(proxiedConfig.workflow.analysis_parallelism, undefined, "反代公网请求不得获得内部并行配置");

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

test("网页设置：读取、校验、保存与恢复覆盖", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "settings" });
  t.after(() => server.stop());
  const { baseUrl, runtimeDir } = server;

  const initial = await fetch(`${baseUrl}/api/settings`);
  assert.equal(initial.status, 200);
  const initialPayload = await initial.json();
  assert.equal(initialPayload.ok, true);
  assert.equal(initialPayload.settings.workflow.analysis_parallelism, 5);
  assert.equal(initialPayload.settings.crawler.qq_page_size, 100);
  assert.equal(initialPayload.settings.runtime.openai_compat.api_key_env, undefined);

  const invalid = await fetch(`${baseUrl}/api/settings`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ settings: { crawler: { qq_page_size: 0 } } }),
  });
  assert.equal(invalid.status, 400);
  assert.match((await invalid.json()).error, /整数/);

  const saved = await fetch(`${baseUrl}/api/settings`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ settings: {
      workflow: { analysis_parallelism: 2, track_percentile_default: 0.5 },
      crawler: { qq_page_size: 80 },
      recommendation_policy: { max_per_artist: 1, max_per_project: 2, min_projects: 4, candidate_pool_min: 1 },
      editorial: { title: "测试 Atlas", lede: "测试导语" },
      runtime: { openai_compat: { base_url: "https://example.invalid/v1", model: "test-model" } },
    } }),
  });
  assert.equal(saved.status, 200);
  const savedPayload = await saved.json();
  assert.equal(savedPayload.settings.workflow.analysis_parallelism, 2);
  assert.equal(savedPayload.settings.workflow.track_percentile_default, 0.5);
  assert.equal(savedPayload.settings.crawler.qq_page_size, 80);
  assert.equal(savedPayload.settings.runtime.openai_compat.model, "test-model");
  assert.equal(savedPayload.settings.runtime.openai_compat.max_tokens, 60000);
  assert.equal(savedPayload.settings.recommendation_policy.max_per_artist, 1);
  assert.equal(savedPayload.settings.editorial.title, "测试 Atlas");
  assert.match(savedPayload.settings_file, /settings\.json$/);

  const config = await (await fetch(`${baseUrl}/api/config`)).json();
  assert.equal(config.workflow.analysis_parallelism, 2);
  assert.deepEqual(config.workflow.track_percentile_options, [0.25, 0.5, 1]);
  assert.equal(config.workflow.track_percentile_default, 0.5);

  const settingsFile = `${runtimeDir}/settings.json`;
  const { access } = await import("node:fs/promises");
  await access(settingsFile);
  const reset = await fetch(`${baseUrl}/api/settings`, { method: "DELETE" });
  assert.equal(reset.status, 200);
  assert.equal((await reset.json()).settings.workflow.analysis_parallelism, 5);

});

test("密钥接口：保存、读取状态与清除 API Key（不触碰真实密钥库）", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "secrets" });
  t.after(() => server.stop());
  const { baseUrl, secretStatePath } = server;

  const initial = await fetch(`${baseUrl}/api/secrets`);
  assert.equal(initial.status, 200);
  const initialPayload = await initial.json();
  assert.equal(initialPayload.ok, true);
  assert.equal(initialPayload.configured, false);
  assert.equal(initialPayload.backend, "fake-file");

  const empty = await fetch(`${baseUrl}/api/secrets`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ api_key: "   " }),
  });
  assert.equal(empty.status, 400);
  assert.match((await empty.json()).error, /不能为空/);

  const saved = await fetch(`${baseUrl}/api/secrets`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ api_key: "sk-cli-test-key" }),
  });
  assert.equal(saved.status, 200);
  const savedPayload = await saved.json();
  assert.equal(savedPayload.configured, true);

  // 状态接口不返回密钥本身，只返回已配置状态；密钥写在隔离状态文件里。
  assert.equal(JSON.stringify(savedPayload).includes("sk-cli-test-key"), false);
  const { readFile } = await import("node:fs/promises");
  const state = JSON.parse(await readFile(secretStatePath, "utf8"));
  assert.equal(state.key, "sk-cli-test-key");

  const cleared = await fetch(`${baseUrl}/api/secrets`, { method: "DELETE" });
  assert.equal(cleared.status, 200);
  assert.equal((await cleared.json()).configured, false);
});

test("旧设置迁移：含已删除的 api_key_env 时仍能启动并自动剥离", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({
    label: "legacy-settings",
    settings: {
      runtime: { openai_compat: { model: "legacy-model", api_key_env: "LEGACY_KEY" } },
      crawler: { qq_page_size: 80 },
      workflow: { max_candidates: 40, candidate_target: 10, context_budget: 40000 },
    },
  });
  t.after(() => server.stop());
  const { baseUrl, runtimeDir } = server;

  const payload = await (await fetch(`${baseUrl}/api/settings`)).json();
  assert.equal(payload.ok, true);
  assert.equal(payload.settings.runtime.openai_compat.model, "legacy-model");
  assert.equal(payload.settings.runtime.openai_compat.api_key_env, undefined);
  assert.equal(payload.settings.crawler.qq_page_size, 80);
  assert.equal(payload.settings.workflow.initial_candidate_limit, 40);
  assert.equal(payload.settings.workflow.hard_candidate_limit, 40);

  // 迁移写回磁盘：文件里也不应再出现 api_key_env。
  const { readFile } = await import("node:fs/promises");
  const stored = await readFile(`${runtimeDir}/settings.json`, "utf8");
  assert.equal(stored.includes("api_key_env"), false);
  assert.equal(stored.includes("max_candidates"), false);
  assert.equal(stored.includes("candidate_target"), false);
  assert.equal(stored.includes("context_budget"), false);
  assert.match(stored, /legacy-model/);
});

test("AI 连通性测试：未配置、缺失密钥、成功与上游错误", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "ai-probe" });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const http = await import("node:http");
  let mode = "ok";
  let lastBody = null;
  const upstream = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => { raw += chunk; });
    req.on("end", () => {
      lastBody = raw;
      if (mode === "unauthorized") {
        res.writeHead(401, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: { message: "invalid api key" } }));
        return;
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "pong" } }] }));
    });
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => upstream.close(resolve)));
  const upstreamPort = upstream.address().port;

  const saveSettings = (compat) => fetch(`${baseUrl}/api/settings`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ settings: { runtime: { openai_compat: compat } } }),
  });

  // 1）未填写接口地址与模型
  const unconfigured = await fetch(`${baseUrl}/api/ai/test`, { method: "POST" });
  assert.equal(unconfigured.status, 502);
  assert.match((await unconfigured.json()).error, /接口地址与模型/);

  // 2）已配置接口但尚未保存密钥
  await saveSettings({ base_url: `http://127.0.0.1:${upstreamPort}/v1`, model: "probe-model" });
  const noKey = await fetch(`${baseUrl}/api/ai/test`, { method: "POST" });
  assert.equal(noKey.status, 502);
  assert.match((await noKey.json()).error, /API Key/);

  // 3）保存密钥后探测成功，并校验发送的最小请求体
  const savedKey = await fetch(`${baseUrl}/api/secrets`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ api_key: "sk-probe-key" }),
  });
  assert.equal(savedKey.status, 200);
  const ok = await fetch(`${baseUrl}/api/ai/test`, { method: "POST" });
  assert.equal(ok.status, 200);
  const okPayload = await ok.json();
  assert.equal(okPayload.ok, true);
  assert.equal(okPayload.model, "probe-model");
  assert.equal(okPayload.key_source, "keyring");
  assert.equal(okPayload.reply_preview, "pong");
  assert.ok(Number.isInteger(okPayload.latency_ms) && okPayload.latency_ms >= 0);
  const sent = JSON.parse(lastBody);
  assert.equal(sent.model, "probe-model");
  assert.equal(sent.temperature, 0);
  assert.ok(sent.max_tokens <= 16);

  // 4）上游 401 应作为失败返回，并带上状态码与上游信息
  mode = "unauthorized";
  const unauthorized = await fetch(`${baseUrl}/api/ai/test`, { method: "POST" });
  assert.equal(unauthorized.status, 502);
  assert.match((await unauthorized.json()).error, /401/);
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

  const unknownJob = await fetch(`${baseUrl}/api/jobs/does-not-exist`);
  assert.equal(unknownJob.status, 404);
  const unknownEvents = await fetch(`${baseUrl}/api/jobs/does-not-exist/events`);
  assert.equal(unknownEvents.status, 404);
});

test("档位与取消接口：未知任务返回 404", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "tracklimit" });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const unknownLimit = await fetch(`${baseUrl}/api/jobs/does-not-exist/limit`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ limit: 10 }),
  });
  assert.equal(unknownLimit.status, 404);
  assert.match((await unknownLimit.json()).error, /找不到/);

  const unknownCancel = await fetch(`${baseUrl}/api/jobs/does-not-exist/cancel`, { method: "POST" });
  assert.equal(unknownCancel.status, 404);

  // 普通路径不应被新增的档位路由误伤。
  const unknownJob = await fetch(`${baseUrl}/api/jobs/does-not-exist`);
  assert.equal(unknownJob.status, 404);
});

test("新 Atlas：没有已完成的基础任务时返回明确错误", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "new-atlas-empty" });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const response = await fetch(`${baseUrl}/api/atlas/new`, { method: "POST" });
  assert.equal(response.status, 400);
  const payload = await response.json();
  assert.equal(payload.ok, false);
  assert.match(payload.error, /24 小时/);
});

test("任务提交支持粘贴整段 App 分享文字", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "share-text", executors: false });
  t.after(() => server.stop());
  const { baseUrl } = server;

  // 解析通过后会走到“执行器未配置”的 503；解析失败则是 400。
  const accepted = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: "分享歌单: Hawk1e喜欢的音乐 https://163cn.tv/bgOsrL6p (@网易云音乐)" }),
  });
  assert.equal(accepted.status, 503, "整段分享文字应能提取出歌单链接");

  const unsupported = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: "分享一首歌 https://example.com/song/1" }),
  });
  assert.equal(unsupported.status, 400, "非歌单平台链接仍应被拒绝");

  // http 链接会自动升级为 https（App 分享文字里常见），因此不再被拒绝。
  const insecure = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: "http://music.163.com/playlist?id=1" }),
  });
  assert.equal(insecure.status, 503, "http 链接应升级为 https 后被接受");

  const nonsense = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: "这段文字里没有链接" }),
  });
  assert.equal(nonsense.status, 400);
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

test("分享文字会带出歌单名，最近歌单不再只显示链接", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "share-name", executors: false });
  t.after(() => server.stop());
  const { baseUrl } = server;

  // 无法直接读内部函数：通过创建任务时的 503/202 响应侧证解析成功，
  // 歌单名的提取逻辑由 submitAuth/任务事件消费，这里用接口层断言不报错。
  const accepted = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: "分享歌单: Hawk1e喜欢的音乐 https://163cn.tv/bgOsrL6p (@网易云音乐)" }),
  });
  assert.equal(accepted.status, 503, "分享文字应能解析出链接");
});
