import test from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createIsolatedServer } from "./helpers.mjs";

/**
 * 回归：推荐阶段任务列表必须顺序一致。
 * 召回尚未完成时，即使后续锁定事件提前送达，也不能显示下游已完成。
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

  // 场景 A：召回完成但尚未锁定，硬约束保持进行中。
  const jobA = {
    status: "running", stage: "recommendation",
    events: [
      ev({ event: "task_started", stage: "recommendation", task_kind: "platform_discovery", task_status: "running" }),
      ev({ event: "task_completed", stage: "recommendation", task_kind: "platform_discovery", task_status: "validated" }),
    ],
  };
  assert.deepEqual(await statesOf(jobA), [
    ["platform_discovery", "done"],
    ["tracks_locked", "running"],
  ], "实际锁定之前不能声称硬约束已完成");

  // 场景 B：乱序到达的锁定事件不能越过仍在进行的召回。
  const jobB = {
    status: "running", stage: "recommendation",
    events: [
      ev({ event: "task_started", stage: "recommendation", task_kind: "platform_discovery", task_status: "running" }),
      ev({ event: "tracks_locked", stage: "recommendation" }),
    ],
  };
  assert.deepEqual(await statesOf(jobB), [
    ["platform_discovery", "running"],
    ["tracks_locked", "waiting"],
  ], "上游进行中时，下游不能显示已完成");

  // 场景 C：召回完成且三组曲目锁定后才显示完成。
  const jobC = {
    status: "running", stage: "recommendation",
    events: [...jobA.events,
      ev({ event: "tracks_locked", stage: "recommendation" })],
  };
  assert.deepEqual(await statesOf(jobC), [
    ["platform_discovery", "done"],
    ["tracks_locked", "done"],
  ]);
});
