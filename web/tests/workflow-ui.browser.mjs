/**
 * 工作流界面事件健壮性测试（Playwright + MockEventSource + 路由拦截）。
 *
 * 通过覆盖 window.EventSource 注入可编程的事件流，配合 page.route 拦截 API，
 * 在不依赖真实服务器行为的情况下验证前端渲染逻辑：
 * - 正常序列：四阶段推进、5+4 并行槽位、批次计数、事件上限 12 条；
 * - 乱序事件：任务状态与计数按最大 seq 收敛，不随数组顺序回退；
 * - 重复事件：相同 seq 只渲染一次（DOM 幂等）；
 * - 丢帧事件：seq 跳号时渲染正常并收敛到最新状态；
 * - SSE 中断：回退轮询、轮询去重、SSE 恢复后继续渲染；
 * - 轮询失败：提示错误并按 1.5s 退避重试；
 * - 终态：详细进度隐藏、刷新 Atlas、提示与按钮状态幂等。
 */

import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createIsolatedServer } from "./helpers.mjs";

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

function nextEvent(job, partial) {
  const seq = (job.events.at(-1)?.seq ?? 0) + 1;
  const event = { at: iso(), seq, ...partial };
  job.events.push(event);
  job.updated_at = event.at;
  if (partial.status === "completed" || partial.event === "failed") {
    job.status = partial.event === "failed" ? "failed" : "completed";
  } else if (partial.status === "running") job.status = "running";
  if (typeof partial.stage === "string") job.stage = partial.stage;
  return event;
}

function addSnapshotStage(job, trackCount = 4) {
  nextEvent(job, { event: "started", status: "running", stage: "snapshot", source_kind: "local_json",
    analysis_parallelism: 5, recommendation_parallelism: 4, message: "开始整理歌单" });
  nextEvent(job, { event: "task_started", status: "running", stage: "snapshot", task_kind: "snapshot_import",
    task_id: "snapshot-import", task_index: 1, task_total: 1, task_status: "running", message: "正在识别平台并拉取歌单" });
  nextEvent(job, { event: "completed", status: "running", stage: "snapshot", track_count: trackCount,
    snapshot_id: "snap-test", source: { kind: "local_json", input: "playlist_sample.json" } });
  nextEvent(job, { event: "task_completed", status: "running", stage: "snapshot", task_kind: "snapshot_import",
    task_id: "snapshot-import", task_index: 1, task_total: 1, task_status: "validated", completed: 1, total: 1,
    track_completed: trackCount, track_total: trackCount, message: `歌单已整理 · ${trackCount} 首` });
}

function addAnalysisStage(job, { batches = 2, tracksPerBatch = 2, completedBatches = batches } = {}) {
  nextEvent(job, { event: "started", status: "running", stage: "analysis", parallelism: 5,
    message: "Step 2：5 个分析任务并行，全部完成后才进入 Step 3" });
  nextEvent(job, { event: "stage_detail", status: "running", stage: "analysis", task_kind: "analysis_batch",
    task_total: batches, completed: 0, total: batches, track_completed: 0, track_total: batches * tracksPerBatch,
    parallelism: 5, parallel_slots: 5, message: `已准备 ${batches} 个分析批次` });
  for (let index = 1; index <= completedBatches; index += 1) {
    nextEvent(job, { event: "task_started", status: "running", stage: "analysis", task_kind: "analysis_batch",
      task_id: `analysis-batch-${index}`, task_index: index, task_total: batches, task_status: "running",
      track_count: tracksPerBatch, parallelism: 5 });
    nextEvent(job, { event: "task_completed", status: "running", stage: "analysis", task_kind: "analysis_batch",
      task_id: `analysis-batch-${index}`, task_index: index, task_total: batches, task_status: "validated",
      completed: index, total: batches, track_completed: index * tracksPerBatch, track_total: batches * tracksPerBatch,
      message: `第 ${index}/${batches} 批分析完成` });
  }
}

function addAnalysisAggregate(job, trackCount = 4) {
  nextEvent(job, { event: "task_started", status: "running", stage: "analysis", task_kind: "analysis_aggregate",
    task_id: "analysis-aggregate", task_index: 1, task_total: 1, task_status: "running", message: "所有批次已返回，正在合并兴趣、关系与覆盖率" });
  nextEvent(job, { event: "completed", status: "running", stage: "analysis", analysis_id: "analysis-test",
    classified_track_count: trackCount, source_track_count: trackCount, parallelism: 5, coverage_report_path: null });
  nextEvent(job, { event: "task_completed", status: "running", stage: "analysis", task_kind: "analysis_aggregate",
    task_id: "analysis-aggregate", task_index: 1, task_total: 1, task_status: "validated", completed: 1, total: 1,
    track_completed: trackCount, track_total: trackCount, classified_track_count: trackCount,
    message: "分析包已校验，允许进入推荐阶段" });
}

function addRecommendationStage(job, { workers = 4, round = 1, rounds = 2, completedWorkers = workers, target = 20 } = {}) {
  nextEvent(job, { event: "started", status: "running", stage: "recommendation", parallelism: 4,
    message: "Step 3：分析包校验通过，启动候选研究并行任务" });
  nextEvent(job, { event: "task_started", status: "running", stage: "recommendation", task_kind: "context_prepare",
    task_id: "recommendation-context", task_index: 1, task_total: 1, task_status: "running", message: "正在准备候选研究上下文" });
  nextEvent(job, { event: "task_completed", status: "running", stage: "recommendation", task_kind: "context_prepare",
    task_id: "recommendation-context", task_index: 1, task_total: 1, task_status: "validated", completed: 1, total: 1,
    message: "推荐研究上下文已准备" });
  nextEvent(job, { event: "round_started", status: "running", stage: "recommendation", task_kind: "recommendation_round",
    round, round_total: rounds, worker_total: workers, parallelism: 4, parallel_slots: workers,
    accepted_candidate_count: 0, candidate_target: target, message: `第 ${round}/${rounds} 轮候选研究，启动 ${workers} 个并行任务` });
  for (let index = 1; index <= completedWorkers; index += 1) {
    nextEvent(job, { event: "task_started", status: "running", stage: "recommendation", task_kind: "recommendation_worker",
      task_id: `recommendation-r${round}-w${index}`, round, round_total: rounds, worker_index: index, worker_total: workers,
      task_index: index, task_total: workers, task_status: "running", parallelism: 4,
      accepted_candidate_count: 0, candidate_target: target });
    nextEvent(job, { event: "task_completed", status: "running", stage: "recommendation", task_kind: "recommendation_worker",
      task_id: `recommendation-r${round}-w${index}`, round, round_total: rounds, worker_index: index, worker_total: workers,
      task_index: index, task_total: workers, task_status: "validated",
      accepted_candidate_count: Math.round((target / workers) * index), candidate_target: target,
      message: `第 ${round} 轮任务 ${index} 已返回` });
  }
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
  await page.route("**/api/config", (route) => route.fulfill({
    json: { ok: true, workflow: { analysis_executor_configured: true, recommendation_executor_configured: true,
      analysis_parallelism: 5, recommendation_parallelism: 4 } },
  }));
  await page.route("**/api/atlas", (route) => {
    counters.atlas += 1;
    return route.fulfill({ json: MINIMAL_ATLAS });
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

test("正常序列：四阶段推进、5+4 槽位、批次计数与事件上限", async () => {
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
    assert.equal(await flow.locator('[data-wf-tasks="analysis"] .wf-task-row').count(), 5, "分析面板固定 5 个槽位");
    assert.equal(await flow.locator('[data-wf-tasks="analysis"] .wf-task-row.waiting').count(), 4, "未启动批次显示等待");
    assert.equal(await flow.locator('[data-wf-tasks="recommendation"] .wf-task-row').count(), 4, "推荐面板固定 4 个槽位");
    assert.equal(await flow.locator('[data-wf-tasks="recommendation"] .wf-task-row.waiting').count(), 4);
    assert.equal(await page.locator('[data-wf-stat="analysis"]').textContent(), "1/2 批 · 2/4 首");
    assert.match(await page.locator('[data-wf-count]').textContent(), /^2\/4 首已处理$/);
    assert.ok((await flow.locator(".wf-event[data-event-key]").count()) <= 12, "事件列表不超过 12 条");

    addAnalysisStage(job);
    addAnalysisAggregate(job);
    addRecommendationStage(job);
    await pushJob(page, job);
    assert.equal(await flow.locator('[data-wf-tasks="recommendation"] .wf-task-row.waiting').count(), 0, "推荐槽位全部启动");
    assert.equal(await flow.locator('[data-wf-tasks="analysis"] .wf-task-row.waiting').count(), 3);
    assert.match(await page.locator('[data-wf-stat="recommendation"]').textContent(), /第 1\/2 轮 · 20\/20 候选/);
    assert.match(await page.locator('[data-wf-note="recommendation"]').textContent(), /仅在第二步完成并校验后启动第三步/);

    const eventsBefore = await flow.locator(".wf-event[data-event-key]").count();
    assert.ok(eventsBefore > 0);
    assert.deepEqual(consoleErrors, [], "不应有控制台错误或警告");
  } finally {
    await context.close();
  }
});

test("乱序事件：任务状态与计数按最大 seq 收敛，不随数组顺序回退", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    addSnapshotStage(job);
    addAnalysisStage(job);
    addAnalysisAggregate(job);

    // 构造乱序数组：completed（seq 较大）排在 started（seq 较小）之前，
    // 并在末尾追加一条旧计数（track_completed=2、seq 最大）验证 Math.max 收敛。
    const events = [...job.events];
    const byId = (id, name) => events.find((event) => event.task_id === id && event.event === name);
    job.events = [
      events[0],
      ...events.filter((event) => event.stage === "snapshot"),
      events.find((event) => event.stage === "analysis" && event.event === "started"),
      events.find((event) => event.stage === "analysis" && event.event === "stage_detail"),
      byId("analysis-batch-1", "task_completed"),
      byId("analysis-batch-2", "task_completed"),
      byId("analysis-batch-1", "task_started"),
      byId("analysis-batch-2", "task_started"),
      { ...byId("analysis-batch-2", "task_completed"), track_completed: 2 },
    ];
    await pushJob(page, job);

    const flow = page.locator("#flow");
    for (const taskId of ["analysis-batch-1", "analysis-batch-2"]) {
      const rowText = await flow.locator(`[data-task-key="${taskId}"]`).textContent();
      assert.match(rowText, /已完成/,
        `${taskId} 应按最大 seq 的 task_completed 显示已完成，而非数组末尾的 running`);
    }
    const stat = await page.locator('[data-wf-stat="analysis"]').textContent();
    assert.match(stat, /2\/2 批 · 4\/4 首/, "批次与曲目计数应收敛到最大值，不回退到旧计数 2");
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
    // 断言针对可见窗口（最后 12 条）内的唯一 seq 数。
    const visibleUnique = new Set(job.events.slice(-12).map((event) => String(event.seq))).size;
    assert.equal(await flow.locator(".wf-event[data-event-key]").count(), visibleUnique,
      "事件 DOM 数应等于可见窗口内唯一 seq 数");
    assert.equal(await flow.locator('[data-task-key="analysis-batch-1"]').count(), 1, "任务行不因重复事件翻倍");
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

    const stat = await page.locator('[data-wf-stat="recommendation"]').textContent();
    assert.match(stat, /第 1\/2 轮/, "丢帧后轮次信息仍收敛（worker 事件携带 round）");
    assert.equal(await page.locator('#flow [data-task-key="recommendation-r1-w1"]').count(), 1,
      "丢帧后推荐 worker 槽位仍渲染");
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
    const eventsBefore = await page.locator("#flow .wf-event[data-event-key]").count();
    assert.ok(eventsBefore > 0);

    // 注入 SSE 断线：前端应在约 1s 后回退轮询。
    const sourceIndex = await activeSourceIndex(page);
    await page.evaluate((index) => window.__mockSources[index].fail(), sourceIndex);
    await page.waitForTimeout(1600);
    assert.ok(counters.poll >= 1, "断线后应发起轮询");

    // 轮询返回相同事件（服务器快照）：DOM 不应出现重复事件节点。
    const eventsAfterPoll = await page.locator("#flow .wf-event[data-event-key]").count();
    assert.equal(eventsAfterPoll, eventsBefore, "轮询去重：事件节点数不变");

    // 轮询路径发现任务仍在运行，会重建 SSE 流（新的 MockEventSource 实例）。
    await page.waitForFunction(() => window.__mockSources.length >= 2, undefined, { timeout: 3000 });
    // 新流继续推送事件，渲染应继续更新。
    addAnalysisStage(job);
    addAnalysisAggregate(job);
    await pushJob(page, job);
    assert.ok((await page.locator("#flow .wf-event[data-event-key]").count()) > eventsBefore,
      "SSE 恢复后事件继续渲染");
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

test("终态处理：失败隐藏详情、完成刷新 Atlas 且提示幂等", async () => {
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

    // 完成态：详细进度隐藏、Atlas 刷新、toast 提示。
    await page.waitForFunction(() => !document.getElementById("flow").classList.contains("on"), undefined, { timeout: 3000 });
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

test("失败终态：隐藏详情并提示失败文案", async () => {
  const job = makeJob();
  const counters = { create: 0, poll: 0, atlas: 0 };
  const { context, page } = await openWorkflowPage(browser, job, counters);
  try {
    await submitForm(page);
    const atlasBefore = counters.atlas; // 页面 bootAtlas 已拉取一次
    addSnapshotStage(job);
    addAnalysisStage(job, { completedBatches: 1 });
    await pushJob(page, job);
    nextEvent(job, { event: "failed", status: "failed", stage: "analysis", error: "分析研究总超时预算耗尽" });
    await pushJob(page, job);

    await page.waitForFunction(() => !document.getElementById("flow").classList.contains("on"), undefined, { timeout: 3000 });
    await page.waitForFunction(() => document.getElementById("toast").textContent.includes("未完成"), undefined, { timeout: 3000 });
    await page.waitForTimeout(200);
    assert.equal(counters.atlas, atlasBefore, "失败不应刷新 Atlas 数据");
  } finally {
    await context.close();
  }
});

async function expectFlowOn(page) {
  await page.waitForFunction(() => document.getElementById("flow").classList.contains("on"), undefined, { timeout: 3000 });
}
