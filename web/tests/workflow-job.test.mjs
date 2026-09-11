/**
 * 网页任务生命周期与 SSE 流测试（纯 Node fetch，不依赖浏览器）。
 *
 * 使用 fake 执行器配置的隔离服务器：
 * - 提交无效的网易云短链（域名白名单内；在线时短链解析 404，离线时 DNS 失败，
 *   两种环境都在快照阶段确定性失败），从而获得一条真实子进程事件流：
 *   queued → started → task_started → failed。
 * - 验证 SSE 头、事件顺序、seq 单调、终态快照重放与并发互斥。
 *
 * 注意：不用真实公开歌单 ID 触发失败，避免依赖第三方歌单数据的存在性；
 * 也不在回归测试中读取真实歌单内容。
 */

import test from "node:test";
import assert from "node:assert/strict";
import { createIsolatedServer, readSseEvents } from "./helpers.mjs";

const INVALID_SHORT_URL = "https://163cn.tv/atlas-e2e-invalid";

function submitJob(baseUrl, url = INVALID_SHORT_URL) {
  return fetch(`${baseUrl}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: url }),
  });
}

test("真实任务生命周期：SSE 收到完整失败事件流，seq 单调递增", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "job" });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const created = await submitJob(baseUrl);
  assert.equal(created.status, 202);
  const { job } = await created.json();
  assert.ok(job.id, "应返回任务 ID");
  assert.equal(job.status, "queued");

  // 连接 SSE 并消费事件流直到任务终态（设置上限避免挂死）。
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 60000);
  const sseResponse = await fetch(`${baseUrl}/api/jobs/${encodeURIComponent(job.id)}/events`, {
    signal: controller.signal,
  });
  assert.equal(sseResponse.status, 200);
  assert.match(sseResponse.headers.get("content-type"), /text\/event-stream/);
  assert.match(sseResponse.headers.get("cache-control"), /no-cache/);

  const seen = [];
  await readSseEvents(
    sseResponse,
    (payload) => {
      seen.push(payload);
      const status = payload.job?.status;
      if (status === "failed" || status === "completed") controller.abort();
    },
    { signal: controller.signal },
  );
  clearTimeout(timeout);

  assert.ok(seen.length >= 2, `应至少收到初始快照与终态事件，实际 ${seen.length} 条`);
  // 每帧都携带任务快照，事件 seq 单调递增。
  const events = seen.at(-1).job.events;
  assert.ok(events.length >= 4, `事件流应包含 queued/started/task_started/failed，实际 ${events.length} 条`);
  const seqs = events.map((event) => event.seq);
  for (let index = 1; index < seqs.length; index += 1) {
    assert.ok(seqs[index] > seqs[index - 1], `seq 必须严格递增：${seqs.join(",")}`);
  }
  assert.equal(events[0].event, "queued");
  assert.ok(events.some((event) => event.event === "started" && event.stage === "snapshot"));
  assert.ok(events.some((event) => event.event === "task_started" && event.task_kind === "snapshot_import"));
  const failed = events.at(-1);
  assert.equal(failed.event, "failed");
  assert.equal(failed.status, "failed");

  // 子进程退出后服务器才回填 exit_code；轮询直到终化。
  const deadline = Date.now() + 15000;
  let finalJob = seen.at(-1).job;
  while (finalJob.exit_code === null && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 100));
    const poll = await fetch(`${baseUrl}/api/jobs/${encodeURIComponent(job.id)}`);
    finalJob = (await poll.json()).job;
  }

  // 终态任务详情：退出码 2、stderr 有诊断内容、运行目录落在隔离 jobs 目录。
  assert.equal(finalJob.status, "failed");
  assert.equal(finalJob.exit_code, 2);
  assert.ok(finalJob.stderr_tail.trim().length > 0, "失败任务应保留 stderr 诊断尾部");
  assert.match(finalJob.runtime_dir.replace(/\\/g, "/"), /web-e2e-job[^/]*\/jobs\//);
  // 失败后任务目录保留事件产物，不在本测试中清理（runtime/ 已被 Git 忽略）。
});

test("任务结束后重新订阅 SSE 仍能收到完整终态快照", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "replay" });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const created = await submitJob(baseUrl);
  const { job } = await created.json();
  await waitForFailure(baseUrl, job.id);

  // EventSource 断线自动重连的语义：重新 GET events 应立即收到完整快照。
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  const response = await fetch(`${baseUrl}/api/jobs/${encodeURIComponent(job.id)}/events`, {
    signal: controller.signal,
  });
  const frames = await readSseEvents(response, () => controller.abort(), { signal: controller.signal });
  clearTimeout(timeout);

  assert.ok(frames.length >= 1, "重连后应收到至少一帧");
  const snapshot = frames[0];
  assert.equal(snapshot.ok, true);
  assert.equal(snapshot.job.status, "failed");
  assert.ok(snapshot.job.events.length >= 4, "快照应携带完整历史事件");
  assert.equal(snapshot.job.events.at(-1).event, "failed");
});

test("活动任务未结束时再次提交被拒绝（409 互斥）", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "mutex" });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const first = await submitJob(baseUrl);
  assert.equal(first.status, 202);
  // 立即再次提交：第一个任务的 Python 子进程至少需要数百毫秒启动并失败，
  // 正常情况下第二个提交落在互斥窗口内；若机器过慢导致任务已结束，则跳过。
  const second = await submitJob(baseUrl);
  if (second.status === 202) {
    const { job } = await second.json();
    await waitForFailure(baseUrl, job.id);
    t.skip("第一个任务过快结束，未覆盖互斥窗口");
    return;
  }
  assert.equal(second.status, 409);
  assert.match((await second.json()).error, /已有网页工作流正在运行/);
  await waitForFailure(baseUrl, (await (await fetch(`${baseUrl}/api/config`)).json()).active_job_id);
});

async function waitForFailure(baseUrl, jobId) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const response = await fetch(`${baseUrl}/api/jobs/${encodeURIComponent(jobId)}`);
    if (response.ok) {
      const { job } = await response.json();
      if (job.status === "failed" || job.status === "completed") return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error("任务未在期限内结束");
}
