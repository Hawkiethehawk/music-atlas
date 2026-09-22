/**
 * 三歌单回归测试集（100% 范围）
 *
 * 每次修改后运行：对固定的三个真实歌单跑完整流程，把每一步的状态、事件、
 * 耗时与三组 Atlas 类型分布保存到本地 regression/<时间戳>/ 下，便于对比。
 *
 * 用法：node scripts/regression.mjs [--no-wait]
 *   --no-wait   只提交任务不等待（用于并行观察）
 */

import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const BASE = process.env.ATLAS_BASE || "https://atlas.hawkie.cloud";
const LOGIN = { username: process.env.ATLAS_USER || "Hawkie", password: process.env.ATLAS_PASS || "Xiaoliyu123." };

const PLAYLISTS = [
  {
    id: "netease-hawkie-favorites",
    label: "网易云 · Hawk1e喜欢的音乐",
    input: "分享歌单: Hawk1e喜欢的音乐 https://163cn.tv/bgOsrL6p (@网易云音乐)",
  },
  {
    id: "netease-liminal",
    label: "网易云 · Liminal",
    input: "分享歌单: Liminal https://163cn.tv/bgSP16lq (@网易云音乐)",
  },
  {
    id: "apple-favorites",
    label: "Apple Music · 喜爱歌曲",
    input: "https://music.apple.com/us/playlist/%E5%96%9C%E7%88%B1%E6%AD%8C%E6%9B%B2/pl.u-9DU1g31kdJ?l=zh",
  },
];

const NO_WAIT = process.argv.includes("--no-wait");
const POLL_MS = 5000;
const TIMEOUT_MS = Number(process.env.REGRESSION_TIMEOUT_MS || 25 * 60 * 1000);

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 15);
const OUT_DIR = path.join(ROOT, "regression", stamp);

let cookie = "";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function api(pathname, options = {}) {
  const response = await fetch(`${BASE}${pathname}`, {
    ...options,
    headers: { "content-type": "application/json", ...(cookie ? { cookie } : {}), ...(options.headers || {}) },
  });
  const setCookie = response.headers.get("set-cookie");
  if (setCookie) cookie = setCookie.split(";")[0];
  const text = await response.text();
  let payload = null;
  try { payload = JSON.parse(text); } catch { payload = { raw: text.slice(0, 400) }; }
  return { status: response.status, payload };
}

async function login() {
  const { status, payload } = await api("/api/auth/login", { method: "POST", body: JSON.stringify(LOGIN) });
  if (!payload.ok) throw new Error(`登录失败（${status}）：${JSON.stringify(payload).slice(0, 200)}`);
  console.log(`已登录：${payload.user.username}（${payload.user.role}）`);
}

function summarizeJob(job) {
  const events = Array.isArray(job.events) ? job.events : [];
  const types = {};
  const count = (kind) => events.filter((event) => event.task_kind === kind).length;
  return {
    id: job.id,
    status: job.status,
    stage: job.stage,
    workflow_mode: job.workflow_mode,
    created_at: job.created_at,
    updated_at: job.updated_at,
    elapsed_seconds: job.created_at && job.updated_at
      ? Math.round((Date.parse(job.updated_at) - Date.parse(job.created_at)) / 1000) : null,
    track_count: events.findLast?.((event) => event.track_count)?.track_count ?? null,
    event_count: events.length,
    task_counts: {
      platform_discovery: count("platform_discovery"),
      agent_style_analysis: count("agent_style_analysis"),
      agent_recommendation_curate: count("agent_recommendation_curate"),
      recommendation_review: count("recommendation_review"),
      atlas_export: count("atlas_export"),
    },
    // 用户可见文案（用于断言不含 Agent / 次数）
    messages: events.map((event) => String(event.message || "")).filter(Boolean),
    errors: events.filter((event) => event.error).map((event) => String(event.error).slice(0, 200)),
    resume_markers: events
      .map((event) => String(event.message || ""))
      .filter((text) => text.includes("续跑") || text.includes("复用")),
    types,
  };
}

async function runPlaylist(playlist) {
  const started = Date.now();
  console.log(`\n=== ${playlist.label} ===`);
  const created = await api("/api/jobs", { method: "POST", body: JSON.stringify({ source_url: playlist.input }) });
  if (!created.payload.ok) {
    console.log(`提交失败（${created.status}）：${JSON.stringify(created.payload).slice(0, 200)}`);
    return { playlist: playlist.id, label: playlist.label, input: playlist.input,
             submitted: false, error: created.payload.error || `HTTP ${created.status}`, elapsed_seconds: 0 };
  }
  const jobId = created.payload.job.id;
  console.log(`任务：${jobId}`);

  return await waitForJob(created.payload.job, playlist, 0, started, false);
}

async function waitForJob(initialJob, playlist, initialMaxTracks, started, limitAlreadySubmitted) {
  let job = initialJob;
  let limitSubmitted = limitAlreadySubmitted;
  let maxTracks = initialMaxTracks;
  while (!NO_WAIT) {
    if (Date.now() - started > TIMEOUT_MS) {
      await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
      return { playlist: playlist.id, label: playlist.label, input: playlist.input, job_id: job.id,
               submitted: true, timed_out: true, ...summarizeJob(job) };
    }
    await sleep(POLL_MS);
    const current = await api(`/api/jobs/${job.id}`);
    job = current.payload.job || job;
    const limitEvent = (job.events || []).findLast?.((event) => event.event === "awaiting_limit")
      || (job.events || []).filter((event) => event.event === "awaiting_limit").pop();
    if (limitEvent?.track_count) maxTracks = limitEvent.track_count;
    // 续跑时任务直接从推荐阶段开始，无需再提交范围
    if (job.status === "awaiting_limit" && !limitSubmitted && maxTracks > 0) {
      const ratio = 1;
      const limit = Math.max(1, Math.ceil(maxTracks * ratio));
      const applied = await api(`/api/jobs/${job.id}/limit`, {
        method: "POST", body: JSON.stringify({ limit, percentile: ratio }),
      });
      limitSubmitted = true;
      console.log(`已提交 100% 范围：${limit} 首（ok=${applied.payload.ok}，复用=${applied.payload.reused_from || "无"}）`);
      if (applied.payload.reused_from) {
        // 命中 24 小时复用：当前任务被替换为复用任务，后续要跟踪新任务 id。
        console.log(`命中复用，改用任务：${applied.payload.job.id}（源 ${applied.payload.reused_from}）`);
        return await waitForJob(applied.payload.job, playlist, maxTracks, started, true);
      }
      continue;
    }
    if (job.status === "completed" || job.status === "failed") break;
  }
  const summary = summarizeJob(job);
  console.log(`结果：${summary.status} / ${summary.stage}，耗时 ${summary.elapsed_seconds} 秒，事件 ${summary.event_count} 条`);
  const artifacts = await collectArtifacts(job.id);
  return { playlist: playlist.id, label: playlist.label, input: playlist.input, job_id: job.id,
           submitted: true, limit_submitted: limitSubmitted, max_tracks: maxTracks,
           ...summary, artifacts };
}

async function collectArtifacts(jobId) {
  const result = { atlas: null, groups: null };
  const atlas = await api("/api/atlas");
  if (atlas.payload?.ok) {
    const recs = atlas.payload.recommendations || [];
    const counter = {};
    for (const item of recs) {
      const key = item.candidateType || item.candidate_type || "unknown";
      counter[key] = (counter[key] || 0) + 1;
    }
    result.atlas = {
      recommendation_count: recs.length,
      type_distribution: counter,
      atlas_group_count: atlas.payload.atlas_group_count ?? null,
      interest_count: (atlas.payload.interests || []).length,
      title: atlas.payload.issue?.title || "",
      generated_at: atlas.payload.generated_at || atlas.payload.extra?.generated_at || "",
      job_id: jobId,
    };
  }
  return result;
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  await login();
  // 后端支持并行（全局 10 / 单用户 3），三个歌单同时跑以缩短回归时间。
  const results = await Promise.all(PLAYLISTS.map(async (playlist) => {
    try {
      return await runPlaylist(playlist);
    } catch (error) {
      console.log(`异常（${playlist.label}）：${error.message || error}`);
      return { playlist: playlist.id, label: playlist.label, input: playlist.input,
               error: String(error.message || error) };
    }
  }));
  await writeFile(path.join(OUT_DIR, "results.json"),
    JSON.stringify({ base: BASE, ran_at: new Date().toISOString(), results }, null, 2), "utf8");

  const lines = [`# 三歌单回归（100% 范围）`, ``, `- 时间：${new Date().toISOString()}`, `- 目标：${BASE}`, ``];
  lines.push(`| 歌单 | 状态 | 耗时 | 曲目 | 事件 | 三组分布 | 续跑标记 | 错误 |`, `|---|---|---|---|---|---|---|---|`);
  for (const item of results) {
    const dist = item.artifacts?.atlas?.type_distribution
      ? Object.entries(item.artifacts.atlas.type_distribution).map(([k, v]) => `${k}:${v}`).join(" ") : "—";
    lines.push(`| ${item.label} | ${item.status || item.error || "—"} | ${item.elapsed_seconds ?? "—"}s | ${item.max_tracks || "—"}`
      + ` | ${item.event_count ?? "—"} | ${dist} | ${(item.resume_markers || []).length} | ${(item.errors || []).join("；").slice(0, 80) || "—"} |`);
  }
  lines.push(``, `## 文案检查（不得出现 Agent / 第 N 次）`, ``);
  const offenders = [];
  for (const item of results) {
    for (const text of item.messages || []) {
      if (/\bAgent\b/.test(text) || /第\s*\d+\s*\/\s*\d+\s*次/.test(text)) offenders.push(`${item.label}：${text}`);
    }
  }
  lines.push(offenders.length ? offenders.map((t) => `- ${t}`).join("\n") : "- 未发现违规文案 ✓");
  await writeFile(path.join(OUT_DIR, "summary.md"), lines.join("\n") + "\n", "utf8");
  console.log(`\n结果已保存：${OUT_DIR}`);
  console.log(`汇总：${path.join(OUT_DIR, "summary.md")}`);
}

main().catch((error) => { console.error(error); process.exit(1); });
