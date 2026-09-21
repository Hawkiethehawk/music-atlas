import test from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createIsolatedServer } from "./helpers.mjs";

/**
 * 回归：推荐阶段任务列表必须顺序一致。
 * 旧 bug：platform_discovery 完成后，“本地复核”在还没有任何事件时被 legacy 逻辑标记为已完成，
 * 出现“第三个已完成、第二个还在进行”的矛盾（多轮交替事件下也会复现）。
 */
test("推荐阶段任务状态顺序一致：下游不越过上游", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "wf-order", authRequired: true });
  t.after(() => server.stop());
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.goto(server.baseUrl + "/");
  await page.waitForFunction(() => document.body.classList.contains("atlas-ready"));

  let seq = 0;
  const ev = (partial) => ({ at: new Date().toISOString(), seq: ++seq, ...partial });
  const statesOf = (job) => page.evaluate((j) => workflowPipelineRecords(j, "recommendation").map((r) => [r.key, r.state]), job);

  // 场景 A（用户截图场景）：召回完成、编排进行中、复核还没有任何事件。
  const jobA = {
    status: "running", stage: "recommendation",
    events: [
      ev({ event: "task_started", stage: "recommendation", task_kind: "platform_discovery", task_status: "running" }),
      ev({ event: "task_completed", stage: "recommendation", task_kind: "platform_discovery", task_status: "validated" }),
      ev({ event: "task_started", stage: "recommendation", task_kind: "agent_recommendation_curate", task_status: "running", message: "Agent 正在编排候选与推荐文案（第 1/3 次）" }),
    ],
  };
  assert.deepEqual(await statesOf(jobA), [
    ["platform_discovery", "done"],
    ["agent_recommendation_curate", "running"],
    ["recommendation_review", "waiting"],
    ["atlas_export", "waiting"],
  ], "编排进行中时，复核不能显示已完成");

  // 场景 B：多轮交替（复核第 1 轮已完成，但编排第 2 轮重新开始）。
  const jobB = {
    status: "running", stage: "recommendation",
    events: [
      ev({ event: "task_started", stage: "recommendation", task_kind: "platform_discovery", task_status: "running" }),
      ev({ event: "task_completed", stage: "recommendation", task_kind: "platform_discovery", task_status: "validated" }),
      ev({ event: "task_completed", stage: "recommendation", task_kind: "agent_recommendation_curate", task_status: "validated" }),
      ev({ event: "task_completed", stage: "recommendation", task_kind: "recommendation_review", task_status: "validated" }),
      ev({ event: "task_started", stage: "recommendation", task_kind: "agent_recommendation_curate", task_status: "running" }),
    ],
  };
  assert.deepEqual(await statesOf(jobB), [
    ["platform_discovery", "done"],
    ["agent_recommendation_curate", "running"],
    ["recommendation_review", "waiting"],
    ["atlas_export", "waiting"],
  ], "上游进行中时，下游不能显示已完成");

  // 场景 C：复核真正进行中（有事件）时正常显示。
  const jobC = {
    status: "running", stage: "recommendation",
    events: [...jobB.events.slice(0, 3),
      ev({ event: "task_started", stage: "recommendation", task_kind: "recommendation_review", task_status: "running" })],
  };
  assert.deepEqual(await statesOf(jobC), [
    ["platform_discovery", "done"],
    ["agent_recommendation_curate", "done"],
    ["recommendation_review", "running"],
    ["atlas_export", "waiting"],
  ]);
});
