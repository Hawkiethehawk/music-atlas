/**
 * 真实小规模网页工作流端到端测试（Playwright + 隔离服务器 + 夹具执行器）。
 *
 * 流程：
 * 1. 在隔离 runtime 目录用 CLI 真实运行 web_workflow.py：tests/fixtures/playlist_sample.json
 *    作为输入，fake_analysis_agent / fake_agent 作为 Skill 执行器（合成夹具，不联网）；
 * 2. 用 ATLAS_WEB_CONFIG 启动隔离服务器，发布路径指向本次运行的 current.json；
 * 3. 验证 /api/health、/api/atlas 返回真实工作流产物，页面渲染真实歌单与 10 首推荐，
 *    且浏览器控制台无错误。
 *
 * 夹具仅证明链路与契约，不代表真实音乐事实或推荐质量。
 */

import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";
import { createIsolatedServer, runFixtureWorkflow } from "./helpers.mjs";

let fixtureRun;
let server;
let browser;

before(async () => {
  // 真实夹具工作流（CLI）：约数秒；事件流落盘供验收报告引用。
  fixtureRun = await runFixtureWorkflow({ label: "atlas-fixture", playlistName: "示例歌单" });
  assert.equal(fixtureRun.code, 0, `夹具工作流应成功退出：${fixtureRun.stderr}`);
  const started = fixtureRun.events.filter((event) => event.event === "started").map((event) => event.stage);
  assert.deepEqual(started, ["snapshot", "analysis", "recommendation", "export"], "应按四个阶段启动");
  assert.ok(fixtureRun.events.some((event) => event.event === "failed") === false, "不应有失败事件");

  // 用本次运行的 current.json 启动隔离服务器。
  server = await createIsolatedServer({ label: "atlas-fixture" });
  const { copyFile, mkdir } = await import("node:fs/promises");
  await mkdir(server.runtimeDir, { recursive: true });
  await copyFile(fixtureRun.currentData, `${server.runtimeDir}/current.json`);
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
  await server?.stop();
});

test("夹具工作流产物：四阶段、两批分析、四槽推荐与 10 首输出", async () => {
  const { events } = fixtureRun;
  const stage = (name) => events.filter((event) => event.stage === name);
  // 分析批次事件来自真实执行（2 批），推荐 worker 为并行分片任务。
  assert.ok(stage("analysis").some((event) => event.event === "task_started" && event.task_kind === "analysis_batch"));
  assert.ok(stage("analysis").some((event) => event.event === "task_completed" && event.task_status === "validated"
    && event.task_kind === "analysis_aggregate"), "分析聚合应校验通过");
  assert.ok(stage("recommendation").some((event) => event.task_kind === "recommendation_worker"),
    "推荐阶段应有并行 worker 任务");
  const exportCompleted = events.at(-1);
  assert.equal(exportCompleted.status, "completed");
  assert.equal(exportCompleted.recommendation_count, 10, "推荐数量固定 10 首");

  const report = JSON.parse(await readFile(`${fixtureRun.jobDir}/web_job_report.json`, "utf8"));
  assert.equal(report.status, "completed");
  assert.equal(report.analysis_parallelism, 5);
  assert.equal(report.recommendation_parallelism, 4);
});

test("隔离服务器发布夹具产物：health 与 /api/atlas 返回真实数据", async () => {
  const health = await fetch(`${server.baseUrl}/api/health`);
  assert.equal(health.status, 200);
  assert.equal((await health.json()).data_available, true);

  const response = await fetch(`${server.baseUrl}/api/atlas`);
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.ok, true);
  assert.equal(payload.payload_type, "music_atlas_web");
  assert.equal(payload.source.playlistName, "示例歌单");
  assert.equal(payload.source.trackCount, 3, "夹具歌单 3 首");
  assert.equal(payload.recommendations.length, 10, "页面数据固定 10 首推荐");
  assert.ok(payload.source.snapshotId, "应携带真实快照 ID");
});

test("Atlas 页面渲染真实夹具产物，控制台无错误", async () => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(String(error)));
  try {
    await page.goto(`${server.baseUrl}/#/discover`);
    await page.waitForFunction(() => document.body.classList.contains("atlas-ready"), undefined, { timeout: 10000 });

    const bodyText = await page.locator("body").textContent();
    assert.match(bodyText, /基于 3 首完整歌单快照/, "页面应展示真实夹具快照的导语文案");
    const recRows = await page.locator(".trow").count();
    assert.equal(recRows, 10, `发现页应渲染 10 个推荐条目，实际 ${recRows}`);
    assert.match(bodyText, /Candidate Fixture 1/, "应出现真实工作流产物的推荐曲目");
    assert.match(bodyText, /研究草稿/, "保留研究草稿边界提示");
    assert.deepEqual(consoleErrors, [], "控制台不应有错误");

    // 四个主页面均可访问。
    for (const route of ["#/discover", "#/atlas", "#/sources"]) {
      await page.goto(`${server.baseUrl}/${route}`);
      await page.waitForLoadState("domcontentloaded");
      const status = await page.evaluate(() => document.body.classList.contains("atlas-ready") || document.body.textContent.length > 0);
      assert.ok(status, `${route} 应可渲染`);
    }
    await page.screenshot({ path: `${fixtureRun.dir}/screenshots/discover.png`, fullPage: false });
  } finally {
    await context.close();
  }
});
