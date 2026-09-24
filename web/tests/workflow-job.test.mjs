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
import { chmod, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { createIsolatedServer, readSseEvents, waitFor, PROJECT_ROOT } from "./helpers.mjs";

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

  // 原始 stderr 仅保存在服务端任务状态，任务响应和 SSE 不向浏览器提供。
  assert.equal(finalJob.status, "failed");
  assert.equal(finalJob.exit_code, 2);
  assert.equal(Object.prototype.hasOwnProperty.call(finalJob, "stderr_tail"), false);
  assert.ok(seen.every((frame) => !Object.prototype.hasOwnProperty.call(frame.job, "stderr_tail")));
  assert.equal(Object.prototype.hasOwnProperty.call(finalJob, "runtime_dir"), false);
  const statePath = path.join(server.runtimeDir, "jobs", job.id, "web_job_state.json");
  const saved = await waitFor(async () => {
    try {
      const value = JSON.parse(await readFile(statePath, "utf8"));
      return value.exit_code === 2 ? value : null;
    } catch { return null; }
  });
  assert.ok(saved.stderr_tail.trim().length > 0, "服务端仍需保留诊断尾部");
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

async function createZeroExitServer(label, { prematureCompleted = false } = {}) {
  return createIsolatedServer({ label, beforeStart: async ({ runtimeDir, config }) => {
    const shim = path.join(runtimeDir, "zero-exit-python.sh");
    const earlyEvent = prematureCompleted
      ? "echo '{\"event\":\"completed\",\"status\":\"completed\",\"stage\":\"export\"}'\n"
      : "";
    await writeFile(shim, `#!/bin/sh\n${earlyEvent}sleep 2\nexit 0\n`);
    await chmod(shim, 0o755);
    config.runtime.python = path.relative(PROJECT_ROOT, shim);
    await writeFile(path.join(runtimeDir, "web.config.json"), JSON.stringify(config, null, 2));
  } });
}

async function writeCompletionArtifacts(server, id, { reorder = false, unpublished = false,
  publication = "published", auditStatus = "not_performed", reviewStatus = "not_performed",
  processedCount = 91, playlistCount = processedCount, sourcedCount = 46,
  reportPlaylistCount = playlistCount, reportProcessedCount = processedCount,
  payloadPlaylistCount = playlistCount, payloadProcessedCount = processedCount } = {}) {
  const jobDir = path.join(server.runtimeDir, "jobs", id);
  const payloadPath = path.join(jobDir, "web_payload.json");
  const publishedPath = path.join(server.runtimeDir, "current.json");
  const selected = Array.from({ length: 3 }, (_, groupIndex) => ({
    id: `atlas-${groupIndex + 1}`,
    recommendations: Array.from({ length: 10 }, (_, trackIndex) => ({
      canonical_track_id: `netease:${groupIndex * 10 + trackIndex + 1}`,
    })),
  }));
  const rendered = selected.map((group) => ({ id: group.id,
    recommendations: group.recommendations.map((track) => ({ id: track.canonical_track_id })),
  }));
  if (reorder) [rendered[1].recommendations[0], rendered[1].recommendations[1]]
    = [rendered[1].recommendations[1], rendered[1].recommendations[0]];
  const selection = { status: "tracks_locked", snapshot_id: "snapshot-test", atlas_groups: selected };
  const artistOnly = playlistCount >= 1000;
  const mode = artistOnly ? "artist_summary" : "public_facts_only";
  const sourceCoverage = { mode: artistOnly ? "artist_only" : "track_with_context",
    track_evidence_count: artistOnly ? 0 : sourcedCount,
    album_background_count: 0, artist_background_count: 0,
    no_style_evidence_count: artistOnly ? processedCount : processedCount - sourcedCount,
    weighted_artist_track_count: artistOnly ? sourcedCount : 0,
    artist_count: 2, sourced_artist_count: sourcedCount ? 1 : 0 };
  const packet = { analysis_id: "analysis-test", source_track_count: processedCount,
    source_playlist_track_count: playlistCount,
    analysis_mode: mode, style_analysis: { evidence_model: "sourced_tags_v1",
      source_coverage: sourceCoverage },
    favorite_tracks: Array.from({ length: processedCount }, () => ({})),
    source_tags: { records: Array.from({ length: processedCount }, () => ({})) },
    recommendation_policy: { analysis_quality: { [artistOnly ? "min_artist_weight_share"
      : "min_track_or_album_share"]: artistOnly ? 0.3 : 0.5 } } };
  const payload = { payload_type: "music_atlas_web",
    status: { run: "completed", publication, evidence_audit: auditStatus, review: reviewStatus },
    source: { snapshotId: "snapshot-test" }, analysis: { analysisId: "analysis-test",
      sourceTrackCount: payloadProcessedCount, sourcePlaylistTrackCount: payloadPlaylistCount,
      evidenceModel: "sourced_tags_v1", mode,
      sourceCoverage },
    audit: { status: auditStatus }, review: { status: reviewStatus },
    atlas_group_count: 3, atlas_groups: rendered,
    recommendations: rendered[0].recommendations };
  const report = { status: "completed", payload_path: payloadPath, current_data_path: publishedPath,
    snapshot_id: "snapshot-test", analysis_id: "analysis-test",
    source_track_count: reportPlaylistCount, processed_track_count: reportProcessedCount,
    recommendation_groups: { count: 3, total_unique_recommendation_count: 30 } };
  const serialized = JSON.stringify(payload);
  await writeFile(path.join(jobDir, "web_selection.json"), JSON.stringify(selection));
  await writeFile(path.join(jobDir, "musician_analysis.json"), JSON.stringify(packet));
  await writeFile(payloadPath, serialized);
  await writeFile(publishedPath, unpublished ? JSON.stringify({ ...payload, generated_at: "other-job" }) : serialized);
  await writeFile(path.join(jobDir, "web_job_report.json"), JSON.stringify(report));
}

test("exit 0 未发最终事件且缺少完整产物时不得标记 completed", {
  concurrency: false, skip: process.platform === "win32",
}, async (t) => {
  const server = await createZeroExitServer("zero-without-artifacts");
  t.after(() => server.stop());
  const created = await submitJob(server.baseUrl);
  assert.equal(created.status, 202);
  const { job } = await created.json();
  const terminal = await waitForFailure(server.baseUrl, job.id);
  assert.equal(terminal.exit_code, 0);
  assert.equal(terminal.status, "failed");
  assert.match(terminal.events.at(-1).error, /发布校验未通过/);
  assert.equal((await (await fetch(`${server.baseUrl}/api/atlas`)).json()).empty, true);
});

test("子进程提前声称 completed 仍须等待退出和来源覆盖校验", {
  concurrency: false, skip: process.platform === "win32",
}, async (t) => {
  const server = await createZeroExitServer("early-false-success", { prematureCompleted: true });
  t.after(() => server.stop());
  const response = await submitJob(server.baseUrl);
  assert.equal(response.status, 202);
  const id = (await response.json()).job.id;
  await writeCompletionArtifacts(server, id, { sourcedCount: 0 });
  await new Promise((resolve) => setTimeout(resolve, 400));
  const pending = (await (await fetch(`${server.baseUrl}/api/jobs/${id}`)).json()).job;
  assert.notEqual(pending.status, "completed", "子进程的成功事件不能跳过服务端发布校验");
  const terminal = await waitForFailure(server.baseUrl, id);
  assert.equal(terminal.status, "failed");
  assert.match(terminal.events.at(-1).error, /覆盖|质量门槛/);
});

test("exit 0 完整正式产物可补终态，草稿或伪造审计不可补", {
  concurrency: false, skip: process.platform === "win32",
}, async (t) => {
  const scenarios = [
    { label: "formal-no-independent-audit", options: {}, expected: "completed" },
    { label: "percentile-track-coverage", options: { playlistCount: 182 }, expected: "completed" },
    { label: "percentile-artist-coverage", options: { playlistCount: 1917,
      processedCount: 959, sourcedCount: 288 }, expected: "completed" },
    { label: "wrong-report-playlist-count", options: { playlistCount: 182,
      reportPlaylistCount: 91 }, expected: "failed" },
    { label: "wrong-report-processed-count", options: { playlistCount: 182,
      reportProcessedCount: 182 }, expected: "failed" },
    { label: "wrong-payload-playlist-count", options: { playlistCount: 182,
      payloadPlaylistCount: 91 }, expected: "failed" },
    { label: "wrong-payload-processed-count", options: { playlistCount: 182,
      payloadProcessedCount: 182 }, expected: "failed" },
    { label: "still-draft", options: { publication: "draft" }, expected: "failed" },
    { label: "false-audit-claim", options: { auditStatus: "accepted" }, expected: "failed" },
    { label: "false-review-claim", options: { reviewStatus: "accepted" }, expected: "failed" },
    { label: "zero-sourced-tags", options: { sourcedCount: 0 }, expected: "failed" },
    { label: "below-sourced-threshold", options: { sourcedCount: 45 }, expected: "failed" },
  ];
  for (const { label, options, expected } of scenarios) {
    const server = await createZeroExitServer(label);
    t.after(() => server.stop());
    const response = await submitJob(server.baseUrl);
    assert.equal(response.status, 202);
    const id = (await response.json()).job.id;
    await writeCompletionArtifacts(server, id, options);
    const terminal = await waitForFailure(server.baseUrl, id);
    assert.equal(terminal.exit_code, 0);
    assert.equal(terminal.status, expected, label);
  }
});

test("exit 0 缺失事件的回退仅在已发布内容与锁定顺序一致时成功", {
  concurrency: false, skip: process.platform === "win32",
}, async (t) => {
  const invalid = await createZeroExitServer("zero-wrong-order");
  t.after(() => invalid.stop());
  const invalidResponse = await submitJob(invalid.baseUrl);
  assert.equal(invalidResponse.status, 202);
  const invalidId = (await invalidResponse.json()).job.id;
  await writeCompletionArtifacts(invalid, invalidId, { reorder: true });
  const rejected = await waitForFailure(invalid.baseUrl, invalidId);
  assert.equal(rejected.exit_code, 0);
  assert.equal(rejected.status, "failed", "曲目顺序不一致不能伪装为完整发布");

  const unpublished = await createZeroExitServer("zero-wrong-publication");
  t.after(() => unpublished.stop());
  const unpublishedResponse = await submitJob(unpublished.baseUrl);
  assert.equal(unpublishedResponse.status, 202);
  const unpublishedId = (await unpublishedResponse.json()).job.id;
  await writeCompletionArtifacts(unpublished, unpublishedId, { unpublished: true });
  const notPublished = await waitForFailure(unpublished.baseUrl, unpublishedId);
  assert.equal(notPublished.exit_code, 0);
  assert.equal(notPublished.status, "failed", "用户发布文件与任务 payload 不一致不能标记成功");

  const valid = await createZeroExitServer("zero-complete-artifacts");
  t.after(() => valid.stop());
  const validResponse = await submitJob(valid.baseUrl);
  assert.equal(validResponse.status, 202);
  const validId = (await validResponse.json()).job.id;
  await writeCompletionArtifacts(valid, validId);
  const accepted = await waitForFailure(valid.baseUrl, validId);
  assert.equal(accepted.exit_code, 0);
  assert.equal(accepted.status, "completed", "完整报告、payload 和用户发布路径一致才允许补终态");
});
