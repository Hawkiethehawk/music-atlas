/**
 * 工作流界面事件健壮性测试（Playwright + MockEventSource + 路由拦截）。
 *
 * 通过覆盖 window.EventSource 注入可编程的事件流，配合 page.route 拦截 API，
 * 在不依赖真实服务器行为的情况下验证前端渲染逻辑：
 * - 正常序列：四阶段推进、Step 2/3 各四个真实环节、日志可查看；
 * - 乱序事件：任务状态与计数按最大 seq 收敛，不随数组顺序回退；
 * - 重复事件：相同 seq 只记录一次（日志幂等）；
 * - 丢帧事件：seq 跳号时渲染正常并收敛到最新状态；
 * - SSE 中断：回退轮询、轮询去重、SSE 恢复后继续渲染；
 * - 轮询失败：提示错误并按 1.5s 退避重试；
 * - 终态：完成详情保留、刷新 Atlas、提示与按钮状态幂等。
 */

import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createIsolatedServer, waitFor } from "./helpers.mjs";

let server;
let browser;

before(async () => {
  server = await createIsolatedServer({ label: "ui" });
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
  await server?.stop();
});

/* ---------------- 事件夹具（结构与 web_workflow.py emit 一致） ---------------- */

function iso() {
  return new Date().toISOString();
}

function makeJob() {
  return {
    id: "ui-test-job",
    status: "queued",
    stage: "queued",
    created_at: iso(),
    updated_at: iso(),
    runtime_dir: "runtime/web-jobs/ui-test-job",
    events: [{ event: "queued", status: "queued", stage: "queued", at: iso(), seq: 0 }],
    stderr_tail: "",
    exit_code: null,
  };
}

function makeRecommendationOnlyJob() {
  const job = makeJob();
  job.workflow_mode = "recommendation_only";
  job.events[0].workflow_mode = "recommendation_only";
  return job;
}

function nextEvent(job, partial) {
  const seq = (job.events.at(-1)?.seq ?? 0) + 1;
  const event = { at: iso(), seq, ...partial };
  job.events.push(event);
  job.updated_at = event.at;
  if (partial.status === "completed" || partial.event === "failed") {
    job.status = partial.event === "failed" ? "failed" : "completed";
  } else if (partial.status === "running") job.status = "running";
  else if (partial.status === "awaiting_limit") job.status = "awaiting_limit";
  if (typeof partial.stage === "string") job.stage = partial.stage;
  return event;
}

function addSnapshotStage(job, trackCount = 4) {
  nextEvent(job, { event: "started", status: "running", stage: "snapshot", source_kind: "local_json",
    analysis_parallelism: 5, recommendation_parallelism: 4, message: "开始整理歌单" });
  nextEvent(job, { event: "task_started", status: "running", stage: "snapshot", task_kind: "snapshot_import",
    task_id: "snapshot-import", task_index: 1, task_total: 1, task_status: "running", message: "正在读取歌单" });
  nextEvent(job, { event: "completed", status: "running", stage: "snapshot", track_count: trackCount,
    snapshot_id: "snap-test", source: { kind: "local_json", input: "playlist_sample.json" } });
  nextEvent(job, { event: "task_completed", status: "running", stage: "snapshot", task_kind: "snapshot_import",
    task_id: "snapshot-import", task_index: 1, task_total: 1, task_status: "validated", completed: 1, total: 1,
    track_completed: trackCount, track_total: trackCount, message: `歌单已整理 · ${trackCount} 首` });
}

function addAwaitingLimitStage(job, trackCount = 120) {
  nextEvent(job, { event: "awaiting_limit", status: "awaiting_limit", stage: "snapshot",
    track_count: trackCount, snapshot_id: "snap-test", timeout_seconds: 1800,
    message: `歌单已读取 · ${trackCount} 首，等待选择处理数量` });
  nextEvent(job, { event: "task_started", status: "awaiting_limit", stage: "snapshot", task_kind: "track_limit",
    task_id: "track-limit", task_index: 1, task_total: 1, task_status: "running",
    track_count: trackCount, message: "等待选择处理数量" });
}

function addLimitAppliedStage(job, { limit = 100, sourceTrackCount = 120 } = {}) {
  nextEvent(job, { event: "limit_applied", status: "running", stage: "snapshot",
    requested_track_limit: limit, source_track_count: sourceTrackCount, track_count: limit,
    snapshot_id: `snap-test-limit${limit}`, message: `已确定处理数量 · 前 ${limit} 首` });
  nextEvent(job, { event: "task_completed", status: "running", stage: "snapshot", task_kind: "track_limit",
    task_id: "track-limit", task_index: 1, task_total: 1, task_status: "validated", completed: 1, total: 1,
    track_count: limit, message: `处理数量已确定 · 前 ${limit} 首` });
}

function addAnalysisStage(job, { completedBatches = 2, trackCount = 4 } = {}) {
  nextEvent(job, { event: "started", status: "running", stage: "analysis", parallelism: 5,
    message: "Step 2：采集公开资料，由 Agent 总结整体风格并归纳三大兴趣岛" });
  nextEvent(job, { event: "task_started", status: "running", stage: "analysis", task_kind: "track_fact_collect",
    task_id: "track-facts", task_status: "running", message: "正在用公开平台记录复核曲目身份" });
  nextEvent(job, { event: "task_completed", status: "running", stage: "analysis", task_kind: "track_fact_collect",
    task_id: "track-facts", task_status: "validated", verified_count: trackCount, source_recorded_count: trackCount,
    unverified_count: 0, track_completed: trackCount, track_total: trackCount, source_track_count: trackCount,
    message: "曲目事实来源已写入审计包" });
  nextEvent(job, { event: "task_started", status: "running", stage: "analysis", task_kind: "agent_style_analysis",
    task_id: "agent-style-analysis", task_status: "running", message: "Agent 正在总结整体风格并归纳三个兴趣岛" });
  if (completedBatches >= 2) nextEvent(job, { event: "task_completed", status: "running", stage: "analysis",
    task_kind: "agent_style_analysis", task_id: "agent-style-analysis", task_status: "validated", island_count: 3,
    message: "整体风格总结与三大兴趣岛已生成" });
}

function addAnalysisAggregate(job, trackCount = 4) {
  nextEvent(job, { event: "task_started", status: "running", stage: "analysis", task_kind: "relationship_collect",
    task_id: "relationship-collect", task_status: "running", message: "正在核验成员、合作与共享音乐人路径" });
  nextEvent(job, { event: "task_completed", status: "running", stage: "analysis", task_kind: "relationship_collect",
    task_id: "relationship-collect", task_status: "validated", resolved_artist_count: 3, related_project_count: 2,
    message: "已核验 3 位代表艺人 · 2 条共享项目路径" });
  nextEvent(job, { event: "task_started", status: "running", stage: "analysis", task_kind: "analysis_packet_validate",
    task_id: "analysis-packet-validate", task_status: "running", message: "正在校验分析包" });
  nextEvent(job, { event: "task_completed", status: "running", stage: "analysis", task_kind: "analysis_packet_validate",
    task_id: "analysis-packet-validate", task_status: "validated", message: "分析包结构与真实性边界校验通过" });
  nextEvent(job, { event: "completed", status: "running", stage: "analysis", analysis_id: "analysis-test",
    classified_track_count: trackCount, source_track_count: trackCount, parallelism: 5, coverage_report_path: null });
}

function addRecommendationStage(job, { candidateCount = 47, groups = 3 } = {}) {
  nextEvent(job, { event: "started", status: "running", stage: "recommendation", parallelism: 4,
    message: "Step 3：分析包校验通过，开始公开平台候选召回" });
  nextEvent(job, { event: "task_started", status: "running", stage: "recommendation", task_kind: "platform_discovery",
    task_id: "platform-discovery", task_status: "running", message: "正在从公开平台记录构造真实候选" });
  nextEvent(job, { event: "task_completed", status: "running", stage: "recommendation", task_kind: "platform_discovery",
    task_id: "platform-discovery", task_status: "validated", candidate_count: candidateCount,
    message: `已取得 ${candidateCount} 首排除原歌单与一周缓存后的真实候选` });
  nextEvent(job, { event: "task_started", status: "running", stage: "recommendation", task_kind: "agent_recommendation_curate",
    task_id: "agent-recommendation-curate", task_status: "running", attempt: 1, message: "Agent 正在编排候选与推荐文案" });
  nextEvent(job, { event: "task_completed", status: "running", stage: "recommendation", task_kind: "agent_recommendation_curate",
    task_id: "agent-recommendation-curate", task_status: "validated", candidate_count: candidateCount,
    message: "Agent 候选编排与推荐文案已返回" });
  nextEvent(job, { event: "task_started", status: "running", stage: "recommendation", task_kind: "recommendation_review",
    task_id: "recommendation-review", task_status: "running", message: "正在本地复核候选事实、来源、数量与去重" });
  nextEvent(job, { event: "task_completed", status: "running", stage: "recommendation", task_kind: "recommendation_review",
    task_id: "recommendation-review", task_status: "validated", candidate_count: candidateCount, atlas_group_count: groups,
    message: "本地事实、来源、去重、数量与三组 Atlas 校验通过" });
  nextEvent(job, { event: "completed", status: "running", stage: "recommendation", recommendation_count: 10,
    recommendation_group_count: groups, total_unique_recommendation_count: 30 });
}

function addRecommendationOnlyStage(job, { candidateCount = 47, groups = 3, target = 10, complete = false, includeStart = true } = {}) {
  if (includeStart) {
    nextEvent(job, { event: "started", status: "running", stage: "recommendation",
      reused_analysis_id: "analysis-reused-test", message: "新 Atlas：复用当前 Step 2 分析包，重新执行 Step 3" });
    nextEvent(job, { event: "task_started", status: "running", stage: "recommendation", task_kind: "platform_discovery",
      task_id: "platform-discovery", task_index: 1, task_total: 1, task_status: "running",
      message: "正在基于当前兴趣岛重新召回候选" });
  }
  if (!complete) return;
  nextEvent(job, { event: "task_completed", status: "running", stage: "recommendation", task_kind: "platform_discovery",
    task_id: "platform-discovery", task_status: "validated", candidate_count: candidateCount, message: "真实候选召回完成" });
  nextEvent(job, { event: "task_started", status: "running", stage: "recommendation", task_kind: "agent_recommendation_curate",
    task_id: "agent-recommendation-curate", task_status: "running", message: "Agent 正在编排候选与推荐文案" });
  nextEvent(job, { event: "task_completed", status: "running", stage: "recommendation", task_kind: "agent_recommendation_curate",
    task_id: "agent-recommendation-curate", task_status: "validated", candidate_count: candidateCount, message: "Agent 文案已返回" });
  nextEvent(job, { event: "task_started", status: "running", stage: "recommendation", task_kind: "recommendation_review",
    task_id: "recommendation-review", task_status: "running", message: "正在本地复核" });
  nextEvent(job, { event: "task_completed", status: "running", stage: "recommendation", task_kind: "recommendation_review",
    task_id: "recommendation-review", task_status: "validated", candidate_count: candidateCount, atlas_group_count: groups,
    message: "本地复核通过" });
  nextEvent(job, { event: "completed", status: "running", stage: "recommendation", recommendation_count: target,
    recommendation_group_count: groups, total_unique_recommendation_count: target * groups });
  nextEvent(job, { event: "completed", status: "completed", stage: "export",
    recommendation_count: target, recommendation_group_count: groups });
  nextEvent(job, { event: "task_completed", status: "completed", stage: "export", task_kind: "atlas_export",
    task_id: "atlas-export", task_index: 1, task_total: 1, task_status: "published", completed: 1, total: 1,
    recommendation_count: target, recommendation_group_count: groups, message: "新 Atlas 已更新" });
}

function addExportStage(job, count = 10) {
  nextEvent(job, { event: "started", status: "running", stage: "export", message: "生成网页脱敏数据并原子切换当前版本" });
  nextEvent(job, { event: "task_started", status: "running", stage: "export", task_kind: "atlas_export",
    task_id: "atlas-export", task_index: 1, task_total: 1, task_status: "running", message: "正在生成并发布当前 Atlas" });
  nextEvent(job, { event: "completed", status: "completed", stage: "export",
    payload_path: "runtime/web-jobs/ui-test-job/web_payload.json", recommendation_count: count });
  nextEvent(job, { event: "task_completed", status: "completed", stage: "export", task_kind: "atlas_export",
    task_id: "atlas-export", task_index: 1, task_total: 1, task_status: "published", completed: 1, total: 1,
    recommendation_count: count, message: "Atlas 已更新" });
}

/* ---------------- 页面与路由装配 ---------------- */

const MINIMAL_ATLAS = {
  ok: true,
  payload_type: "music_atlas_web",
  issue: { title: "测试 Atlas", lede: "测试导语" },
  status: { publication: "draft" },
  recommendations: [],
};

async function installRoutes(page, job, counters) {
  counters.limits = counters.limits || [];
  counters.cancels = counters.cancels || [];
  await page.route("**/api/config", (route) => route.fulfill({
    json: { ok: true, active_job_id: counters.activeJobId || null, latest_job_id: counters.latestJobId || null, workflow: { analysis_executor_configured: true, recommendation_executor_configured: true,
      analysis_parallelism: 5, recommendation_parallelism: 4,
      track_percentile_options: [0.25, 0.5, 1], track_percentile_default: 1,
      await_limit_timeout_seconds: 1800 } },
  }));
  await page.route("**/api/atlas", (route) => {
    counters.atlas += 1;
    return route.fulfill({ json: counters.atlasPayload || MINIMAL_ATLAS });
  });
  await page.route("**/api/atlas/new", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    counters.newAtlas = (counters.newAtlas || 0) + 1;
    if (counters.newAtlasHtml) return route.fulfill({ status: 404, contentType: "text/html", body: "<!doctype html><p>404</p>" });
    return route.fulfill({ status: 202, json: { ok: true, job: counters.newAtlasJob || job } });
  });
  await page.route(/\/api\/jobs\/ui-test-job\/limit$/, (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    counters.limits.push(JSON.parse(route.request().postData() || "{}"));
    return route.fulfill({ status: 202, json: { ok: true, job } });
  });
  await page.route(/\/api\/jobs\/ui-test-job\/cancel$/, (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    counters.cancels.push(true);
    return route.fulfill({ status: 200, json: { ok: true, job } });
  });
  await page.route(/\/api\/jobs\/ui-test-job$/, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    counters.poll += 1;
    return route.fulfill({ json: { ok: true, job } });
  });
  await page.route(/\/api\/jobs$/, (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    counters.create += 1;
    return route.fulfill({ status: 202, json: { ok: true, job } });
  });
}

async function openWorkflowPage(browser, job, counters) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error" || message.type() === "warning") consoleErrors.push(message.text());
  });
  await page.addInitScript(() => {
    class MockEventSource {
      constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.onmessage = null;
        this.onerror = null;
        this.closed = false;
        window.__mockSources.push(this);
      }
      emit(payload) {
        if (!this.closed && this.onmessage) this.onmessage({ data: JSON.stringify(payload) });
      }
      fail() {
        if (!this.closed && this.onerror) this.onerror(new Event("error"));
      }
      close() {
        this.closed = true;
        this.readyState = 2;
      }
    }
    window.__mockSources = [];
    window.EventSource = MockEventSource;
  });
  await installRoutes(page, job, counters);
  await page.goto(`${server.baseUrl}/#/sources`);
  await page.waitForFunction(() => document.getElementById("workflow-form") !== null);
  return { context, page, consoleErrors };
}

async function submitForm(page) {
  await page.fill("#wf-source-url", "https://music.163.com/playlist?id=123456");
  await page.click("#workflow-form button[type=submit]");
  await page.waitForFunction(() => window.__mockSources.length > 0);
}

async function pushJob(page, job) {
  await page.evaluate((payload) => {
    const source = window.__mockSources.at(-1);
    if (!source) throw new Error("没有活动的 MockEventSource");
    source.emit({ ok: true, job: payload, event: payload.events.at(-1) });
  }, structuredClone(job));
}

async function activeSourceIndex(page) {
  return page.evaluate(() => window.__mockSources.length - 1);
}

/* ---------------- 用例 ---------------- */

test("正常序列：四阶段推进、Step 2/3 真实环节与事件上限", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page, consoleErrors } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);

    addSnapshotStage(job);
    addAnalysisStage(job, { completedBatches: 1 });
    await pushJob(page, job);

    const flow = page.locator("#flow");
    await expectFlowOn(page);
    assert.equal(await flow.locator(".wf-stage").count(), 4, "应渲染四个阶段");
    const stageStates = await flow.locator(".wf-stage").evaluateAll((nodes) =>
      nodes.map((node) => `${node.dataset.stage}:${node.className.includes("done") ? "done" : node.className.includes("current") ? "current" : "pending"}`));
    assert.deepEqual(stageStates, [
      "snapshot:done", "analysis:current", "recommendation:pending", "export:pending",
    ]);
    assert.equal(await flow.locator('[data-wf-tasks="analysis"] .wf-task-row').count(), 4, "分析面板固定显示四个真实环节");
    assert.equal(await flow.locator('[data-wf-tasks="analysis"] .wf-task-row.waiting').count(), 2, "尚未执行的真实环节显示等待");
    assert.equal(await flow.locator('[data-wf-tasks="recommendation"] .wf-task-row').count(), 4, "推荐面板固定显示四个真实环节");
    assert.equal(await flow.locator('[data-wf-tasks="recommendation"] .wf-task-row.waiting').count(), 4);
    assert.equal(await page.locator('[data-wf-stat="analysis"]').textContent(), "进行中 · 1/4");
    assert.match(await page.locator('[data-wf-count]').textContent(), /^4\/4 首已处理$/);
    assert.doesNotMatch(await flow.textContent(), /0\/0 批|并行槽位空闲|等待下一个任务/);
    assert.equal(await flow.locator(".wf-events").count(), 0, "不应再显示下方最近事件列表");
    assert.equal(await flow.locator('[data-wf-stage-panel="analysis"]').getAttribute("open"), "", "当前阶段默认展开");
    assert.equal(await flow.locator('[data-wf-stage-panel="snapshot"]').getAttribute("open"), null, "已完成阶段默认折叠");

    addAnalysisStage(job);
    addAnalysisAggregate(job);
    addRecommendationStage(job);
    await pushJob(page, job);
    assert.equal(await flow.locator('[data-wf-tasks="recommendation"] .wf-task-row.waiting').count(), 1, "导出在推荐完成后等待执行");
    assert.equal(await flow.locator('[data-wf-tasks="analysis"] .wf-task-row.waiting').count(), 0);
    assert.equal(await page.locator('[data-wf-stat="recommendation"]').textContent(), "等待 · 3/4");
    assert.match(await page.locator('[data-wf-note="recommendation"]').textContent(), /第二步校验通过后/);

    await page.click("[data-wf-log-toggle]");
    assert.match(await page.locator("[data-wf-log-text]").textContent(), /曲目事实来源已写入审计包/);
    assert.deepEqual(consoleErrors, [], "不应有控制台错误或警告");
  } finally {
    await context.close();
  }
});

test("乱序事件：每个真实环节按最大 seq 收敛，不随数组顺序回退", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    addSnapshotStage(job);
    addAnalysisStage(job);
    addAnalysisAggregate(job);
    const events = [...job.events];
    const task = (kind, name) => events.find((event) => event.task_kind === kind && event.event === name);
    job.events = [
      events[0], ...events.filter((event) => event.stage === "snapshot"),
      events.find((event) => event.stage === "analysis" && event.event === "started"),
      task("track_fact_collect", "task_completed"), task("agent_style_analysis", "task_completed"),
      task("track_fact_collect", "task_started"), task("agent_style_analysis", "task_started"),
      ...events.filter((event) => ["relationship_collect", "analysis_packet_validate"].includes(event.task_kind)),
      events.find((event) => event.stage === "analysis" && event.event === "completed"),
    ].filter(Boolean);
    await pushJob(page, job);
    for (const key of ["track_fact_collect", "agent_style_analysis", "relationship_collect", "analysis_packet_validate"]) {
      assert.match(await page.locator(`#flow [data-task-key="${key}"]`).textContent(), /已完成/,
        `${key} 应按最大 seq 的完成事件显示已完成`);
    }
    assert.equal(await page.locator('[data-wf-stat="analysis"]').textContent(), "已完成 · 4/4");
  } finally {
    await context.close();
  }
});

test("重复事件：相同 seq 只渲染一次", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    addSnapshotStage(job);
    addAnalysisStage(job);
    addAnalysisAggregate(job);
    // 复制最近两条事件，制造重复投递（相同 seq、相同内容）。
    job.events.push({ ...job.events.at(-1) });
    job.events.push({ ...job.events.at(-2) });
    await pushJob(page, job);

    const flow = page.locator("#flow");
    await page.click("[data-wf-log-toggle]");
    const logText = await page.locator("[data-wf-log-text]").textContent();
    const message = "曲目事实来源已写入审计包";
    assert.equal((logText.match(new RegExp(message, "g")) || []).length, 1, "相同 seq 的事件只应记录一次");
    assert.equal(await flow.locator('[data-task-key="track_fact_collect"]').count(), 1, "任务行不因重复事件翻倍");
  } finally {
    await context.close();
  }
});

test("丢帧事件：seq 跳号时渲染正常并收敛到最新状态", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    addSnapshotStage(job);
    addAnalysisStage(job);
    addAnalysisAggregate(job);
    addRecommendationStage(job);
    // 模拟丢帧：只保留偶数 seq 事件，且不含 export 终态（聚焦运行中收敛）。
    job.events = job.events.filter((event) => event.seq % 2 === 0 && event.stage !== "export");
    await pushJob(page, job);

    assert.equal(await page.locator('#flow [data-wf-tasks="recommendation"] .wf-task-row').count(), 4,
      "丢帧后四个推荐环节仍保持可见");
    assert.equal(await page.locator('#flow [data-task-key="platform_discovery"]').count(), 1,
      "丢帧后候选召回环节仍可定位");
  } finally {
    await context.close();
  }
});

test("SSE 中断：回退轮询、轮询去重、SSE 恢复后继续渲染", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    addSnapshotStage(job);
    addAnalysisStage(job, { completedBatches: 1 });
    await pushJob(page, job);
    await page.click("[data-wf-log-toggle]");
    const logBefore = await page.locator("#flow [data-wf-log-text]").textContent();
    assert.match(logBefore, /正在读取歌单/);

    // 注入 SSE 断线：前端应在约 1s 后回退轮询。
    const sourceIndex = await activeSourceIndex(page);
    await page.evaluate((index) => window.__mockSources[index].fail(), sourceIndex);
    await page.waitForTimeout(1600);
    assert.ok(counters.poll >= 1, "断线后应发起轮询");

    // 轮询返回相同事件（服务器快照）：DOM 不应出现重复事件节点。
    const logAfterPoll = await page.locator("#flow [data-wf-log-text]").textContent();
    assert.equal(logAfterPoll, logBefore, "轮询去重：日志内容不变");

    // 轮询路径发现任务仍在运行，会重建 SSE 流（新的 MockEventSource 实例）。
    await page.waitForFunction(() => window.__mockSources.length >= 2, undefined, { timeout: 3000 });
    // 新流继续推送事件，渲染应继续更新。
    addAnalysisStage(job);
    addAnalysisAggregate(job);
    await pushJob(page, job);
    const logAfterResume = await page.locator("#flow [data-wf-log-text]").textContent();
    assert.match(logAfterResume, /整体风格总结与三大兴趣岛已生成|开始分析歌单/, "SSE 恢复后日志继续更新");
  } finally {
    await context.close();
  }
});

test("轮询失败：提示错误并按退避重试", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  let failing = false;
  await page.route(/\/api\/jobs\/ui-test-job$/, (route) => {
    if (route.request().method() !== "GET" || !failing) return route.fallback();
    counters.poll += 1;
    return route.fulfill({ status: 500, json: { ok: false, error: "任务状态不可用" } });
  });
  try {
    await submitForm(page);
    addSnapshotStage(job);
    await pushJob(page, job);

    failing = true;
    const sourceIndex = await activeSourceIndex(page);
    await page.evaluate((index) => window.__mockSources[index].fail(), sourceIndex);
    await page.waitForFunction(() => document.getElementById("toast").classList.contains("on")
      && document.getElementById("toast").textContent.length > 0, undefined, { timeout: 5000 });
    assert.match(await page.locator("#toast").textContent(), /任务状态不可用/);

    const pollCount = counters.poll;
    // 1.5s 退避后应再次轮询。
    await page.waitForTimeout(2200);
    assert.ok(counters.poll > pollCount, "轮询失败后应按退避间隔重试");
  } finally {
    await context.close();
  }
});

test("终态处理：完成保留详情、刷新 Atlas 且提示幂等", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    addSnapshotStage(job);
    addAnalysisStage(job);
    addAnalysisAggregate(job);
    addRecommendationStage(job);
    addExportStage(job);
    await pushJob(page, job);

    // 完成态：详细进度保留、Atlas 刷新、toast 提示。
    await page.waitForFunction(() => document.getElementById("flow").classList.contains("on")
      && document.querySelector("#flow .fstep")?.textContent.includes("运行已完成"), undefined, { timeout: 3000 });
    await page.waitForTimeout(600);
    assert.ok(counters.atlas >= 1, "完成后应刷新 /api/atlas");
    await page.waitForFunction(() => document.getElementById("toast").textContent.includes("已完成"), undefined, { timeout: 3000 });

    // 幂等：重复收到同一终态（SSE 重复投递）不再叠加提示或刷新。
    const atlasAfterFirst = counters.atlas;
    const toastBefore = await page.locator("#toast").textContent();
    await pushJob(page, job);
    await page.waitForTimeout(300);
    assert.equal(counters.atlas, atlasAfterFirst, "重复终态不应再次触发 Atlas 刷新");
    assert.equal(await page.locator("#toast").textContent(), toastBefore, "提示不叠加");
  } finally {
    await context.close();
  }
});

test("刷新恢复：通过 latest_job_id 显示最近完成状态", async () => {
  const job = makeJob();
  addSnapshotStage(job);
  addAnalysisStage(job);
  addAnalysisAggregate(job);
  addRecommendationStage(job);
  addExportStage(job);
  const counters = { create: 0, poll: 0, atlas: 0, latestJobId: job.id };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await page.waitForFunction(() => document.getElementById("flow").classList.contains("on")
      && document.querySelector("#flow .fstep")?.textContent.includes("运行已完成"), undefined, { timeout: 3000 });
    assert.ok(counters.poll >= 1, "刷新时应读取最近完成任务");
    assert.match(await page.locator("#flow").textContent(), /运行已完成/);
  } finally {
    await context.close();
  }
});

test("失败终态：保留进度框、失败原因与可展开日志，刷新后仍可恢复", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    const atlasBefore = counters.atlas;
    addSnapshotStage(job);
    addAnalysisStage(job, { completedBatches: 1 });
    await pushJob(page, job);
    job.stderr_tail = "Traceback: analysis timeout";
    job.exit_code = 1;
    nextEvent(job, { event: "failed", status: "failed", stage: "analysis", error: "分析研究总超时预算耗尽" });
    await pushJob(page, job);

    await page.waitForFunction(() => document.getElementById("flow").classList.contains("on")
      && document.querySelector("#flow .fstep")?.textContent.includes("运行未完成"), undefined, { timeout: 3000 });
    assert.match(await page.locator("[data-wf-terminal-error]").textContent(), /分析研究总超时预算耗尽/);
    assert.match(await page.locator("#toast").textContent(), /分析研究总超时预算耗尽/);
    assert.match(await page.locator('[data-task-key="agent_style_analysis"]').textContent(), /失败/);
    await page.click("[data-wf-log-toggle]");
    assert.equal(await page.locator("[data-wf-log-panel]").evaluate((node) => node.classList.contains("hidden")), false);
    const logText = await page.locator("[data-wf-log-text]").textContent();
    assert.match(logText, /退出码: 1/);
    assert.match(logText, /分析研究总超时预算耗尽/);
    assert.match(logText, /Traceback: analysis timeout/);
    assert.equal(counters.atlas, atlasBefore, "失败不应刷新 Atlas 数据");

    await page.reload();
    await page.waitForFunction(() => document.getElementById("flow").classList.contains("on")
      && document.querySelector("#flow .fstep")?.textContent.includes("运行未完成"), undefined, { timeout: 3000 });
    assert.match(await page.locator("[data-wf-terminal-error]").textContent(), /分析研究总超时预算耗尽/,
      "刷新后失败进度与错误详情仍应恢复");
  } finally {
    await context.close();
  }
});

test("设置入口：打开配置表单、测试 AI 连通性并可关闭", async () => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const probes = [];
  try {
    // 拦截 AI 探测与密钥写入，避免测试依赖外部模型服务。
    await page.route("**/api/ai/test", (route) => {
      probes.push(route.request().method());
      return route.fulfill({ status: 200, json: { ok: true, model: "probe-model", base_url: "https://example.invalid/v1",
        key_source: "keyring", latency_ms: 812, reply_preview: "pong" } });
    });
    await page.route("**/api/secrets", (route) => {
      if (route.request().method() !== "PUT") return route.fallback();
      return route.fulfill({ status: 200, json: { ok: true, configured: true, backend: "fake-file" } });
    });
    await page.goto(`${server.baseUrl}/#/sources`);
    await page.waitForSelector("[data-settings-open]");
    await page.click("[data-settings-open]");
    await page.waitForSelector("#settings-form");
    assert.equal(await page.locator("#settings-ai-model").count(), 1);
    assert.equal(await page.locator("#settings-ai-key").getAttribute("type"), "password");
    assert.equal(await page.locator("#settings-ai-key-env").count(), 0, "不应再有环境变量名输入框");
    assert.equal(await page.locator("#settings-crawler-timeout").inputValue(), "30");
    // 底部操作条固定在弹窗正下方，不随内容滚动。
    assert.equal(await page.locator(".settings-actions").evaluate((node) => getComputedStyle(node).position), "static");
    assert.ok(await page.locator(".settings-dialog").evaluate((node) => node.scrollHeight <= node.clientHeight + 1),
      "弹窗自身不应滚动，而由内容区滚动");
    await page.fill("#settings-ai-base-url", "https://example.invalid/v1");
    await page.fill("#settings-ai-model", "test-model");
    await page.fill("#settings-ai-key", "sk-ui-test");
    await page.click("[data-settings-test-ai]");
    await page.waitForFunction(() => document.getElementById("settings-ai-test-result")?.textContent.includes("连通正常"));
    const note = await page.locator("#settings-ai-test-result").textContent();
    assert.match(note, /812 ms/);
    assert.match(note, /probe-model/);
    assert.match(note, /系统密钥库/);
    assert.deepEqual(probes, ["POST"]);
    await page.click("#settings-form button[type=submit]");
    await page.waitForFunction(() => document.getElementById("settings-message")?.textContent.includes("已保存"));
    page.on("dialog", (dialog) => dialog.accept());
    await page.click("[data-settings-reset]");
    await page.waitForFunction(() => document.getElementById("settings-message")?.textContent.includes("已恢复"));
    await page.click(".settings-close");
    assert.equal(await page.locator("#settings-form").count(), 0);
  } finally {
    await context.close();
  }
});

test("档位选择：歌单读完后才可选数量，上限为曲目数，提交后继续处理", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0, limits: [], cancels: [] };
  const { context, page, consoleErrors } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    // 歌单读取中：数量控件仍隐藏
    addSnapshotStage(job, 123);
    await pushJob(page, job);
    assert.ok(await page.locator("#wf-limit-field").evaluate((node) => node.classList.contains("hidden")),
      "歌单未读取完成前不应出现数量控件");

    // 歌单读取完成：出现数量控件，上限为真实曲目数
    addAwaitingLimitStage(job, 123);
    await pushJob(page, job);
    await page.waitForFunction(() => !document.getElementById("wf-limit-field").classList.contains("hidden"));
    assert.equal(await page.locator("#wf-limit-max").textContent(), "123");
    assert.equal(await page.locator("#wf-limit").inputValue(), "123", "默认选择 100% 完整歌单");
    assert.ok(await page.locator("#wf-submit").evaluate((node) => node.classList.contains("hidden")),
      "等待选择数量时隐藏“生成推荐”按钮");
    assert.ok(await page.locator("#flow").evaluate((node) => node.classList.contains("on")),
      "等待期间进度面板保持显示");
    assert.match(await page.locator("[data-wf-summary]").textContent(), /等待选择处理数量/);

    // 123 首按分位向上取整：25%=31、50%=62、100%=123；仅保留三种分位。
    const chipTexts = await page.locator("#wf-limit-chips .chip").evaluateAll((nodes) =>
      nodes.map((node) => `${node.textContent}:${node.dataset.limit}:${node.dataset.percentile || "manual"}`));
    assert.deepEqual(chipTexts, [
      "25% · 31 首:31:0.25", "50% · 62 首:62:0.5", "100% · 全部:123:1",
    ]);
    await page.click('#wf-limit-chips .chip[data-percentile="0.5"]');
    assert.equal(await page.locator("#wf-limit").inputValue(), "62");
    assert.equal(await page.locator("#wf-limit-chips .chip.on").getAttribute("data-percentile"), "0.5");

    await page.click('#wf-limit-chips .chip[data-percentile="1"]');
    assert.equal(await page.locator("#wf-limit").inputValue(), "123");
    assert.match(await page.locator("#wf-limit-chips .chip.on").textContent(), /100%/);
    assert.equal(await page.locator("#wf-limit").getAttribute("type"), "hidden");

    // 选择 50% 后提交，必须把分位与向上取整后的整数一起发送。
    await page.click('#wf-limit-chips .chip[data-percentile="0.5"]');
    await page.click("#wf-limit-confirm");
    await waitFor(() => counters.limits.length === 1, { message: "处理数量请求未发出" });
    assert.deepEqual(counters.limits, [{ limit: 62, percentile: 0.5 }]);

    addLimitAppliedStage(job, { limit: 62, sourceTrackCount: 123 });
    addAnalysisStage(job);
    addAnalysisAggregate(job);
    await pushJob(page, job);
    await page.waitForFunction(() => document.getElementById("wf-limit-field").classList.contains("hidden"));
    assert.ok(await page.locator("#flow").evaluate((node) => node.classList.contains("on")),
      "确定数量后进度面板继续显示");
    assert.deepEqual(consoleErrors, [], "不应有控制台错误或警告");
  } finally {
    await context.close();
  }
});

test("等待选择数量时可以取消任务", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0, limits: [], cancels: [] };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    addSnapshotStage(job, 60);
    addAwaitingLimitStage(job, 60);
    await pushJob(page, job);
    await page.waitForFunction(() => !document.getElementById("wf-limit-field").classList.contains("hidden"));
    // 默认仍分析完整歌单；分位按真实总数向上取整。
    assert.equal(await page.locator("#wf-limit").inputValue(), "60");
    const smallChips = await page.locator("#wf-limit-chips .chip").evaluateAll((nodes) =>
      nodes.map((node) => `${node.textContent}:${node.dataset.limit}`));
    assert.deepEqual(smallChips, ["25% · 15 首:15", "50% · 30 首:30", "100% · 全部:60"]);

    await page.click("#wf-cancel");
    await waitFor(() => counters.cancels.length === 1, { message: "取请求未发出" });
    await page.waitForFunction(() => document.getElementById("toast").textContent.includes("取消"));
  } finally {
    await context.close();
  }
});

async function expectFlowOn(page) {
  await page.waitForFunction(() => document.getElementById("flow").classList.contains("on"), undefined, { timeout: 3000 });
}


test("新 Atlas 复用流程：不伪造分析进度，完成后保留正确状态并支持刷新恢复", async () => {
  const job = makeRecommendationOnlyJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page, consoleErrors } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    const flow = page.locator("#flow");
    await expectFlowOn(page);
    assert.match(await flow.locator(".fstep").textContent(), /正在生成新 Atlas/);
    assert.equal(await flow.locator(".wf-stage.reused").count(), 2, "整理与分析阶段应明确标记为已复用");
    assert.equal(await flow.locator('[data-wf-tasks="analysis"] .wf-task-row').count(), 4, "复用流程保留四个 Step 2 环节并标记已复用");
    assert.equal(await flow.locator('[data-wf-tasks="analysis"] .wf-task-row.reused').count(), 4);
    assert.match(await flow.locator('[data-wf-stat="analysis"]').textContent(), /分析包已复用/);
    assert.match(await flow.locator('[data-wf-note="analysis"]').textContent(), /本次不重新分析歌单/);
    assert.equal(await flow.locator('[data-wf-count]').textContent(), "正在生成新候选");
    assert.doesNotMatch(await flow.textContent(), /0\/0 批|0\/0 首|并行槽位空闲/);

    addRecommendationOnlyStage(job);
    await pushJob(page, job);
    assert.equal(await flow.locator('[data-wf-tasks="recommendation"] .wf-task-row').count(), 4);
    assert.equal(await flow.locator('[data-wf-stat="recommendation"]').textContent(), "进行中 · 0/4");
    await page.click("[data-wf-log-toggle]");
    assert.match(await flow.locator("[data-wf-log-text]").textContent(), /复用当前分析，开始生成新 Atlas/);

    addRecommendationOnlyStage(job, { complete: true, includeStart: false });
    await pushJob(page, job);
    await page.waitForFunction(() => document.querySelector("#flow .fstep")?.textContent.includes("新 Atlas 运行已完成"), undefined, { timeout: 3000 });
    assert.match(await flow.locator('[data-wf-summary]').textContent(), /新 Atlas 已生成/);
    assert.equal(await flow.locator('[data-wf-count]').textContent(), "47 个候选已核验 · 3 组 Atlas");
    assert.match(await page.locator("#toast").textContent(), /新 Atlas 已生成/);
    assert.doesNotMatch(await flow.textContent(), /0\/0 批|0\/0 首|并行槽位空闲/);

    counters.latestJobId = job.id;
    await page.reload();
    await page.waitForFunction(() => document.querySelector("#flow .fstep")?.textContent.includes("新 Atlas 运行已完成"), undefined, { timeout: 3000 });
    assert.equal(await page.locator('#flow [data-wf-tasks="analysis"] .wf-task-row.reused').count(), 4, "刷新后仍应恢复复用视图");
    assert.equal(await page.locator('#flow [data-wf-count]').textContent(), "47 个候选已核验 · 3 组 Atlas");
    assert.doesNotMatch(await page.locator("#flow").textContent(), /0\/0 批|0\/0 首|并行槽位空闲/);
    assert.deepEqual(consoleErrors, [], "不应有控制台错误或警告");
  } finally {
    await context.close();
  }
});

test("新 Atlas 失败：使用对应失败文案并恢复操作按钮", async () => {
  const job = makeRecommendationOnlyJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    addRecommendationOnlyStage(job);
    await pushJob(page, job);
    nextEvent(job, { event: "failed", status: "failed", stage: "recommendation", error: "候选暂时不足" });
    await pushJob(page, job);
    await page.waitForFunction(() => document.getElementById("toast").textContent.includes("新 Atlas 生成未完成"), undefined, { timeout: 3000 });
    assert.match(await page.locator("#toast").textContent(), /候选暂时不足/);
    assert.equal(await page.locator("#workflow-form button[type=submit]").isDisabled(), false);
  } finally {
    await context.close();
  }
});

test("Atlas 批次导航：可返回上一批，最后一批显示新 Atlas 并复用分析启动", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0, newAtlas: 0 };
  counters.atlasPayload = {
    ...MINIMAL_ATLAS,
    recommendations: [{ id: "base", rank: 1, track: "Base", artist: "Artist", album: "Album", type: "style", typeLabel: "风格邻近", oneLiner: "base", why: "base", route: [], evidence: [], year: "—", hue: 20 }],
    atlas_groups: [1, 2, 3].map((n) => ({ id: `atlas-${n}`, label: `第 ${n} 组`, recommendations: [{ id: `rec-${n}`, rank: 1, track: `Track ${n}`, artist: "Artist", album: "Album", type: "style", typeLabel: "风格邻近", oneLiner: "test", why: "test", route: [], evidence: [], year: "—", hue: 20 }], artists: {}, albums: {}, sources: [] })),
  };
  counters.newAtlasJob = makeRecommendationOnlyJob();
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await page.goto(`${server.baseUrl}/#/discover`);
    await page.waitForFunction(() => document.body.textContent.includes("第 1/3 组"));
    assert.equal(await page.locator("[data-atlas-prev]").isDisabled(), true);
    assert.equal(await page.locator("[data-atlas-swap]").count(), 1);
    await page.click("[data-atlas-swap]");
    assert.match(await page.locator("body").textContent(), /第 2\/3 组/);
    assert.equal(await page.locator("[data-atlas-prev]").isDisabled(), false);
    await page.click("[data-atlas-prev]");
    assert.match(await page.locator("body").textContent(), /第 1\/3 组/);
    await page.click("[data-atlas-swap]");
    await page.click("[data-atlas-swap]");
    assert.match(await page.locator("body").textContent(), /第 3\/3 组/);
    assert.equal(await page.locator("[data-atlas-swap]").count(), 0);
    assert.equal(await page.locator("[data-atlas-new]").count(), 1);
    await page.click("[data-atlas-new]");
    await page.waitForFunction(() => document.body.textContent.includes("新 Atlas"));
    assert.equal(counters.newAtlas, 1);
    assert.equal(await page.locator("[data-atlas-new]").isDisabled(), true, "运行中按钮应禁用");
    await page.waitForFunction(() => window.__mockSources.length > 0);
    nextEvent(counters.newAtlasJob, { event: "failed", status: "failed", stage: "workflow", error: "候选暂时不足" });
    await pushJob(page, counters.newAtlasJob);
    await page.waitForFunction(() => !document.querySelector("[data-atlas-new]").disabled);
    assert.equal(await page.locator("[data-atlas-new]").isDisabled(), false, "异步失败后按钮应恢复可用");
  } finally {
    await context.close();
  }
});

test("新 Atlas 接口返回 HTML 时显示可理解提示，不暴露 JSON 解析错误", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0, newAtlas: 0, newAtlasHtml: true };
  counters.atlasPayload = {
    ...MINIMAL_ATLAS,
    recommendations: [{ id: "base", rank: 1, track: "Base", artist: "Artist", album: "Album", type: "style", typeLabel: "风格邻近", oneLiner: "base", why: "base", route: [], evidence: [], year: "—", hue: 20 }],
    atlas_groups: [1, 2, 3].map((n) => ({ id: `atlas-${n}`, label: `第 ${n} 组`, recommendations: [{ id: `rec-${n}`, rank: 1, track: `Track ${n}`, artist: "Artist", album: "Album", type: "style", typeLabel: "风格邻近", oneLiner: "test", why: "test", route: [], evidence: [], year: "—", hue: 20 }], artists: {}, albums: {}, sources: [] })),
  };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await page.goto(`${server.baseUrl}/#/atlas`);
    await page.waitForFunction(() => document.body.textContent.includes("第 1/3 组"));
    await page.click("[data-atlas-swap]");
    await page.click("[data-atlas-swap]");
    await page.click("[data-atlas-new]");
    await page.waitForFunction(() => document.getElementById("toast").textContent.includes("接口尚未加载"));
    const toastText = await page.locator("#toast").textContent();
    assert.doesNotMatch(toastText, /Unexpected token|valid JSON|doctype/i);
    assert.equal(await page.locator("[data-atlas-new]").isDisabled(), false, "失败后按钮应恢复可用");
  } finally {
    await context.close();
  }
});

test("封面首选地址失败时从 /api/meta 回退到备用公开封面", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0, limits: [], cancels: [] };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  const payload = {
    ok: true,
    payload_type: "music_atlas_web",
    issue: { title: "封面回退测试", lede: "测试" },
    status: { publication: "draft" },
    source: { snapshotId: "cover-fallback-test" },
    sources: [],
    recommendations: [{
      id: "cover-test-1", rank: 1, track: "Sympathy", artist: "Too Close To Touch",
      album: "Haven't Been Myself", cover: "https://assets.test/bad-cover.jpg", hue: 20,
      type: "style", typeLabel: "风格邻近", oneLiner: "测试推荐", why: "测试推荐理由",
      route: [], evidence: [], year: "—",
    }],
  };
  counters.atlasPayload = payload;
  await page.route("**/api/meta**", (route) => route.fulfill({ json: {
    ok: true, cover: "https://assets.test/good-cover.svg",
    links: { apple: "https://music.apple.com/", netease: "https://music.163.com/", qq: "https://y.qq.com/" },
  } }));
  await page.route("https://assets.test/bad-cover.jpg", (route) => route.fulfill({ status: 404, body: "" }));
  await page.route("https://assets.test/good-cover.svg", (route) => route.fulfill({
    status: 200, contentType: "image/svg+xml", body: "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"8\" height=\"8\"><rect width=\"8\" height=\"8\" fill=\"#c8622f\"/></svg>",
  }));
  try {
    await page.goto(`${server.baseUrl}/#/discover`);
    await page.reload();
    if (!(await page.locator(".cover[data-cover],.cover[data-artist]").count())) console.error("DEBUG", (await page.locator("body").textContent()).slice(0,500));
    await page.waitForSelector(".cover[data-cover],.cover[data-artist]", { state: "attached", timeout: 10000 });
    await page.waitForFunction(() => document.body.classList.contains("covers-ready"), undefined, { timeout: 10000 });
    const cover = page.locator(".cover[data-cover],.cover[data-artist]").first();
    assert.equal(await cover.locator("img.cover-img").count(), 1);
    assert.match(await cover.locator("img.cover-img").getAttribute("src"), /good-cover\.svg/);
    assert.ok(await cover.evaluate((node) => node.classList.contains("has-img")));
  } finally {
    await context.close();
  }
});
