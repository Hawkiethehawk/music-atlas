#!/usr/bin/env node
/**
 * Music Atlas · Editorial Atlas — 本机固定端口静态服务器
 * http://127.0.0.1:8420   （跨平台：Windows / macOS / Linux，仅依赖 Node 内置模块）
 *
 * API:
 *   GET /api/meta?artist=&track=  → { cover, links:{apple,netease,qq} }
 *     cover: 真实专辑封面（iTunes Search API，兜底网易云专辑图）
 *     links: 三平台的具体歌曲链接（解析失败时回退到平台搜索页）
 */
const http = require("http");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");

const ROOT = __dirname; // 服务目录固定为本文件所在目录
const PROJECT_ROOT = path.resolve(ROOT, "..");
// 默认读取项目内 config/web.json；测试通过 ATLAS_WEB_CONFIG 指向临时配置，
// 以隔离端口和运行目录启动独立实例，不影响生产配置。
const CONFIG_PATH = process.env.ATLAS_WEB_CONFIG
  ? path.resolve(process.env.ATLAS_WEB_CONFIG)
  : path.join(PROJECT_ROOT, "config", "web.json");
const DEFAULT_FILE = "editorial-atlas.html";
const WORKFLOW_SCRIPT = path.join(PROJECT_ROOT, "web_workflow.py");

function loadWebConfig() {
  let config;
  try {
    config = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  } catch (error) {
    throw new Error(`无法读取项目配置 ${CONFIG_PATH}：${error.message}`);
  }
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new Error(`项目配置必须是 JSON 对象：${CONFIG_PATH}`);
  }
  return config;
}

function resolveProjectPath(value, fieldName) {
  if (typeof value !== "string" || !value.trim() || path.isAbsolute(value)) {
    throw new Error(`${fieldName} 必须是项目目录内的相对路径`);
  }
  const candidate = path.resolve(PROJECT_ROOT, value);
  const relative = path.relative(PROJECT_ROOT, candidate);
  if (!relative || relative.startsWith(".." + path.sep) || path.isAbsolute(relative)) {
    throw new Error(`${fieldName} 必须位于项目目录内`);
  }
  return candidate;
}

function resolveConfiguredExecutable(value) {
  const executable = typeof value === "string" && value.trim() ? value.trim() : "python";
  if (path.isAbsolute(executable) || executable.includes("/") || executable.includes("\\")) {
    return resolveProjectPath(executable, "runtime.python");
  }
  return executable;
}

function quoteCommandPath(value) {
  if (process.platform === "win32") return `"${String(value).replace(/"/g, '\\"')}"`;
  return `'${String(value).replace(/'/g, "'\\''")}'`;
}

function configureExecutor(value, fieldName, python) {
  if (value === null || value === undefined || value === "") {
    return { command: "", path: null, configured: false, error: null };
  }
  const scriptPath = resolveProjectPath(value, fieldName);
  if (path.extname(scriptPath).toLowerCase() !== ".py") {
    throw new Error(`${fieldName} 必须指向项目内的 Python 执行器脚本`);
  }
  if (!fs.existsSync(scriptPath) || !fs.statSync(scriptPath).isFile()) {
    return { command: "", path: scriptPath, configured: false, error: `${fieldName} 文件不存在` };
  }
  const pythonPart = path.isAbsolute(python) ? quoteCommandPath(python) : python;
  return {
    command: `${pythonPart} ${quoteCommandPath(scriptPath)}`,
    path: scriptPath,
    configured: true,
    error: null,
  };
}

const WEB_CONFIG = loadWebConfig();
const SERVER_CONFIG = WEB_CONFIG.server || {};
const PATH_CONFIG = WEB_CONFIG.paths || {};
const WORKFLOW_CONFIG = WEB_CONFIG.workflow || {};
const RUNTIME_CONFIG = WEB_CONFIG.runtime || {};
const EXECUTOR_CONFIG = WEB_CONFIG.executors || {};
const HOST = typeof SERVER_CONFIG.host === "string" && SERVER_CONFIG.host.trim()
  ? SERVER_CONFIG.host.trim() : "127.0.0.1";
// ATLAS_WEB_PORT 供服务管理器（atlas start）与测试覆盖端口；默认仍读 config/web.json。
const PORT = Number(process.env.ATLAS_WEB_PORT) || Number(SERVER_CONFIG.port) || 8420;
if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) throw new Error("server.port 必须是 1 到 65535 的整数");
const DATA_PATH = resolveProjectPath(PATH_CONFIG.published || "runtime/web/current.json", "paths.published");
const JOB_ROOT = resolveProjectPath(PATH_CONFIG.jobs || "runtime/web-jobs", "paths.jobs");
const INPUT_ROOT = resolveProjectPath(PATH_CONFIG.input || "input", "paths.input");
const PYTHON = resolveConfiguredExecutable(RUNTIME_CONFIG.python);
const ANALYSIS_PARALLELISM = Number(WORKFLOW_CONFIG.analysis_parallelism ?? 5);
if (!Number.isInteger(ANALYSIS_PARALLELISM) || ANALYSIS_PARALLELISM < 1 || ANALYSIS_PARALLELISM > 16) {
  throw new Error("workflow.analysis_parallelism 必须是 1 到 16 的整数");
}
const RECOMMENDATION_PARALLELISM = Number(WORKFLOW_CONFIG.recommendation_parallelism ?? 4);
if (![3, 4].includes(RECOMMENDATION_PARALLELISM)) {
  throw new Error("workflow.recommendation_parallelism 只接受 3 或 4");
}
const ANALYSIS_TIMEOUT_SECONDS = Number(WORKFLOW_CONFIG.analysis_timeout_seconds ?? 600);
if (!Number.isInteger(ANALYSIS_TIMEOUT_SECONDS) || ANALYSIS_TIMEOUT_SECONDS < 1 || ANALYSIS_TIMEOUT_SECONDS > 86400) {
  throw new Error("workflow.analysis_timeout_seconds 必须是 1 到 86400 的整数");
}
const RECOMMENDATION_TIMEOUT_SECONDS = Number(WORKFLOW_CONFIG.recommendation_timeout_seconds ?? 600);
if (!Number.isInteger(RECOMMENDATION_TIMEOUT_SECONDS) || RECOMMENDATION_TIMEOUT_SECONDS < 1 || RECOMMENDATION_TIMEOUT_SECONDS > 86400) {
  throw new Error("workflow.recommendation_timeout_seconds 必须是 1 到 86400 的整数");
}
// 网页端档位：歌单读取完成后才允许选择处理数量，选项与默认值都来自项目配置。
const TRACK_LIMIT_OPTIONS = (() => {
  const raw = WORKFLOW_CONFIG.track_limit_options ?? [30, 100, 200, 500, 1000];
  if (!Array.isArray(raw) || !raw.length) throw new Error("workflow.track_limit_options 必须是非空的整数数组");
  const options = [];
  for (const value of raw) {
    if (!Number.isInteger(value) || value < 1) throw new Error("workflow.track_limit_options 只能包含正整数");
    if (!options.includes(value)) options.push(value);
  }
  return options.sort((left, right) => left - right);
})();
const TRACK_LIMIT_DEFAULT = WORKFLOW_CONFIG.track_limit_default ?? TRACK_LIMIT_OPTIONS[0];
if (!Number.isInteger(TRACK_LIMIT_DEFAULT) || TRACK_LIMIT_DEFAULT < 1) {
  throw new Error("workflow.track_limit_default 必须是正整数");
}
const AWAIT_LIMIT_TIMEOUT_SECONDS = Number(WORKFLOW_CONFIG.await_limit_timeout_seconds ?? 1800);
if (!Number.isInteger(AWAIT_LIMIT_TIMEOUT_SECONDS) || AWAIT_LIMIT_TIMEOUT_SECONDS < 1 || AWAIT_LIMIT_TIMEOUT_SECONDS > 86400) {
  throw new Error("workflow.await_limit_timeout_seconds 必须是 1 到 86400 的整数");
}
// 与 web_workflow.py 的 LIMIT_REQUEST_FILENAME 保持一致：网页写请求，工作流读请求。
const LIMIT_REQUEST_FILENAME = "requested_track_limit.json";
const ANALYSIS_EXECUTOR = configureExecutor(EXECUTOR_CONFIG.analysis, "executors.analysis", PYTHON);
const RECOMMENDATION_EXECUTOR = configureExecutor(EXECUTOR_CONFIG.recommendation, "executors.recommendation", PYTHON);
const ANALYSIS_COMMAND = ANALYSIS_EXECUTOR.command;
const RECOMMENDATION_COMMAND = RECOMMENDATION_EXECUTOR.command;
const jobs = new Map();
const jobSubscribers = new Map();
let activeJobId = null;

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".md": "text/markdown; charset=utf-8",
};

/* ---------------- 封面 / 歌曲链接解析 ---------------- */

const META_TTL = 60 * 60 * 1000; // 成功缓存 1h
const NEG_TTL = 10 * 60 * 1000;  // 失败负缓存 10min（避免反复打外网）
const metaCache = new Map();     // key → { at, data }

function jfetch(url, opts = {}, timeout = 6000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  return fetch(url, { ...opts, signal: ctrl.signal })
    .then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .finally(() => clearTimeout(timer));
}

async function loadAtlasPayload() {
  const raw = await fs.promises.readFile(DATA_PATH, "utf8");
  const payload = JSON.parse(raw);
  if (!payload || payload.payload_type !== "music_atlas_web") {
    throw new Error("invalid web payload");
  }
  return payload;
}

function sendJson(res, status, value, extraHeaders = {}) {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    ...extraHeaders,
  });
  res.end(JSON.stringify(value));
}

function readJsonBody(req, limit = 32 * 1024) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      body += chunk;
      if (Buffer.byteLength(body, "utf8") > limit) {
        reject(new Error("请求体过大"));
        req.destroy();
      }
    });
    req.on("end", () => {
      if (!body.trim()) {
        resolve({});
        return;
      }
      try {
        const value = JSON.parse(body);
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("JSON 对象 required");
        resolve(value);
      } catch (error) {
        reject(new Error("请求体不是合法 JSON"));
      }
    });
    req.on("error", reject);
  });
}

/* 等待选择处理数量时，上限来自工作流已读取到的真实曲目数（awaiting_limit 事件）。 */
function trackLimitMaximum(job) {
  for (let index = job.events.length - 1; index >= 0; index -= 1) {
    const event = job.events[index];
    if (event && event.event === "awaiting_limit" && Number.isInteger(event.track_count)) {
      return event.track_count;
    }
  }
  return null;
}

function publicJob(job) {
  return {
    id: job.id,
    status: job.status,
    stage: job.stage,
    created_at: job.created_at,
    updated_at: job.updated_at,
    runtime_dir: job.runtime_dir,
    events: job.events,
    stderr_tail: job.stderr_tail || "",
    exit_code: job.exit_code === undefined ? null : job.exit_code,
  };
}

function recordJobEvent(job, event) {
  const safeEvent = event && typeof event === "object" ? { ...event } : { event: "message", message: String(event) };
  safeEvent.at = safeEvent.at || new Date().toISOString();
  safeEvent.seq = ++job.event_seq;
  job.events.push(safeEvent);
  if (job.events.length > 1000) job.events.shift();
  if (typeof safeEvent.stage === "string") job.stage = safeEvent.stage;
  if (safeEvent.status === "completed" || safeEvent.event === "failed") job.status = safeEvent.status === "completed" ? "completed" : "failed";
  else if (safeEvent.status === "running") job.status = "running";
  else if (safeEvent.status === "awaiting_limit") job.status = "awaiting_limit";
  job.updated_at = safeEvent.at;
  const subscribers = jobSubscribers.get(job.id) || new Set();
  for (const res of subscribers) {
    try {
      res.write(`data: ${JSON.stringify({ ok: true, job: publicJob(job), event: safeEvent })}\n\n`);
    } catch {}
  }
}

function allowedSource(body) {
  const sourceUrl = String(body.source_url || body.url || "").trim();
  if (!sourceUrl) throw new Error("请提交歌单公开链接");
  let parsed;
  try { parsed = new URL(sourceUrl); } catch { throw new Error("歌单链接必须是 HTTPS 公开链接"); }
  if (parsed.protocol !== "https:") throw new Error("歌单链接必须是 HTTPS 公开链接");
  const hostname = (parsed.hostname || "").toLowerCase().replace(/\.$/, "");
  const sourceByHost = {
    "music.apple.com": { kind: "apple_music", platform: "apple_music" },
    "music.163.com": { kind: "netease_public", platform: "netease" },
    "163cn.tv": { kind: "netease_public", platform: "netease" },
    "www.163cn.tv": { kind: "netease_public", platform: "netease" },
    "y.qq.com": { kind: "qq_public", platform: "qq_music" },
    "i.y.qq.com": { kind: "qq_public", platform: "qq_music" },
  };
  const source = sourceByHost[hostname];
  if (!source) throw new Error("只支持 Apple Music、网易云音乐或 QQ 音乐公开歌单链接");
  return {
    ...source,
    source_url: sourceUrl,
    playlist_id: "",
    playlist_name: "",
    expected_count: null,
  };
}

async function startWorkflowJob(config) {
  if (activeJobId) throw new Error("已有网页工作流正在运行，请等待其完成");
  await fs.promises.mkdir(JOB_ROOT, { recursive: true });
  const id = `${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-${crypto.randomBytes(4).toString("hex")}`;
  const runtimeDir = path.join(JOB_ROOT, id);
  await fs.promises.mkdir(runtimeDir);
  const job = {
    id,
    status: "queued",
    stage: "queued",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    runtime_dir: runtimeDir,
    events: [],
    stderr_tail: "",
    event_seq: 0,
  };
  jobs.set(id, job);
  activeJobId = id;
  recordJobEvent(job, { event: "queued", status: "queued", stage: "queued" });

  const args = [
    WORKFLOW_SCRIPT,
    "--runtime-dir", runtimeDir,
    "--current-data", DATA_PATH,
    "--source-kind", config.kind,
    "--analysis-parallelism", String(ANALYSIS_PARALLELISM),
    "--recommendation-parallelism", String(RECOMMENDATION_PARALLELISM),
    "--analysis-timeout", String(ANALYSIS_TIMEOUT_SECONDS),
    "--recommendation-timeout", String(RECOMMENDATION_TIMEOUT_SECONDS),
  ];
  args.push("--analysis-command", ANALYSIS_COMMAND, "--recommendation-command", RECOMMENDATION_COMMAND);
  // 歌单先读完再选数量：工作流在 snapshot 阶段后暂停，等网页写回处理数量。
  args.push("--await-track-limit", "--await-limit-timeout", String(AWAIT_LIMIT_TIMEOUT_SECONDS));
  if (config.source_url) args.push("--source-url", config.source_url);
  if (config.input) args.push("--input", config.input);
  if (config.playlist_id) args.push("--playlist-id", config.playlist_id);
  if (config.playlist_name) args.push("--playlist-name", config.playlist_name);
  if (config.platform) args.push("--platform", config.platform);
  if (config.expected_count !== null) args.push("--expected-count", String(config.expected_count));
  const childEnvironment = { ...process.env };
  delete childEnvironment.ATLAS_WEB_ANALYSIS_COMMAND;
  delete childEnvironment.ATLAS_WEB_RECOMMENDATION_COMMAND;
  const child = spawn(PYTHON, args, { cwd: PROJECT_ROOT, env: childEnvironment, stdio: ["ignore", "pipe", "pipe"] });
  job.child = child;
  let stdoutBuffer = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdoutBuffer += chunk;
    const lines = stdoutBuffer.split(/\r?\n/);
    stdoutBuffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try { recordJobEvent(job, JSON.parse(line)); }
      catch { recordJobEvent(job, { event: "diagnostic", status: job.status, stage: job.stage, message: "工作流输出无法解析" }); }
    }
  });
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    job.stderr_tail = (job.stderr_tail + chunk).slice(-2000);
  });
  const release = () => {
    if (activeJobId === id) activeJobId = null;
    job.child = undefined;
  };
  child.on("error", (error) => {
    job.exit_code = null;
    recordJobEvent(job, { event: "failed", status: "failed", stage: job.stage, error: `无法启动网页工作流：${error.message}` });
    release();
  });
  child.on("close", (code) => {
    job.exit_code = code;
    if (stdoutBuffer.trim()) {
      try { recordJobEvent(job, JSON.parse(stdoutBuffer)); } catch {}
    }
    if (job.status !== "completed" && job.status !== "failed") {
      if (code === 0) recordJobEvent(job, { event: "completed", status: "completed", stage: "export" });
      else recordJobEvent(job, { event: "failed", status: "failed", stage: job.stage, error: job.stderr_tail.trim() || `工作流退出码 ${code}` });
    }
    release();
  });
  return publicJob(job);
}

/* iTunes Search API：返回具体歌曲页 + 专辑封面 */
async function itunesSong(term) {
  const tries = [
    `https://itunes.apple.com/search?term=${encodeURIComponent(term)}&entity=song&limit=1`,
    `https://itunes.apple.com/search?term=${encodeURIComponent(term)}&entity=song&limit=1&country=CN`,
  ];
  for (const u of tries) {
    try {
      const j = await jfetch(u);
      const hit = j.results && j.results[0];
      if (hit && hit.trackViewUrl) return hit;
    } catch {}
  }
  return null;
}

/* 网易云搜索：歌曲 id（兜底封面 picUrl） */
async function neteaseSong(term) {
  try {
    const j = await jfetch("https://music.163.com/api/search/get", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://music.163.com",
      },
      body: new URLSearchParams({ s: term, type: "1", limit: "1", offset: "0" }).toString(),
    });
    const s = j.result && j.result.songs && j.result.songs[0];
    if (s && s.id) return s;
  } catch {}
  return null;
}

/* QQ 音乐搜索：songmid */
async function qqSong(term) {
  try {
    const j = await jfetch(
      `https://c.y.qq.com/soso/fcgi-bin/client_search_cp?format=json&limit=1&w=${encodeURIComponent(term)}`,
      { headers: { Referer: "https://y.qq.com/" } }
    );
    const s = j.data && j.data.song && j.data.song.list && j.data.song.list[0];
    if (s && s.songmid) return s;
  } catch {}
  return null;
}

async function resolveMeta(artist, track) {
  const key = artist + "|" + track;
  const hit = metaCache.get(key);
  if (hit && Date.now() - hit.at < (hit.data.cover ? META_TTL : NEG_TTL)) return hit.data;

  const term = `${artist} ${track}`;
  const [it, ne, qq] = await Promise.all([itunesSong(term), neteaseSong(term), qqSong(term)]);

  const data = {
    cover:
      (it && it.artworkUrl100 && it.artworkUrl100.replace("100x100", "600x600")) ||
      (ne && ne.album && ne.album.picUrl) ||
      null,
    links: {
      apple:
        (it && it.trackViewUrl) ||
        `https://music.apple.com/us/search?term=${encodeURIComponent(term)}`,
      netease: ne
        ? `https://music.163.com/song?id=${ne.id}`
        : `https://music.163.com/#/search/m/?s=${encodeURIComponent(term)}`,
      qq: qq
        ? `https://y.qq.com/n/ryqq/songDetail/${qq.songmid}`
        : `https://y.qq.com/n/ryqq/search?w=${encodeURIComponent(term)}`,
    },
    matched: { apple: !!it, netease: !!ne, qq: !!qq },
  };
  metaCache.set(key, { at: Date.now(), data });
  return data;
}

/* ---------------- 静态服务 ---------------- */

const server = http.createServer(async (req, res) => {
  let urlPath, query;
  try {
    const u = new URL(req.url, `http://${HOST}:${PORT}`);
    urlPath = u.pathname;
    query = u.searchParams;
  } catch {
    res.writeHead(400).end("Bad Request");
    return;
  }

  if (urlPath === "/api/config" && req.method === "GET") {
    sendJson(res, 200, {
      ok: true,
      config_file: path.relative(PROJECT_ROOT, CONFIG_PATH),
      paths: {
        published: path.relative(PROJECT_ROOT, DATA_PATH),
        jobs: path.relative(PROJECT_ROOT, JOB_ROOT),
        input: path.relative(PROJECT_ROOT, INPUT_ROOT),
      },
      workflow: {
        analysis_executor_configured: ANALYSIS_EXECUTOR.configured,
        recommendation_executor_configured: RECOMMENDATION_EXECUTOR.configured,
        analysis_executor_error: ANALYSIS_EXECUTOR.error,
        recommendation_executor_error: RECOMMENDATION_EXECUTOR.error,
        analysis_parallelism: ANALYSIS_PARALLELISM,
        recommendation_parallelism: RECOMMENDATION_PARALLELISM,
        recommendation_parallelism_options: [3, 4],
        track_limit_options: TRACK_LIMIT_OPTIONS,
        track_limit_default: TRACK_LIMIT_DEFAULT,
        await_limit_timeout_seconds: AWAIT_LIMIT_TIMEOUT_SECONDS,
        analysis_timeout_seconds: ANALYSIS_TIMEOUT_SECONDS,
        recommendation_timeout_seconds: RECOMMENDATION_TIMEOUT_SECONDS,
        apple_requires_expected_count: false,
      },
      active_job_id: activeJobId,
    });
    return;
  }

  if (urlPath === "/api/jobs" && req.method === "POST") {
    try {
      const body = await readJsonBody(req);
      const config = allowedSource(body);
      if (!ANALYSIS_COMMAND || !RECOMMENDATION_COMMAND) {
        sendJson(res, 503, { ok: false, error: "当前暂不可生成推荐" });
        return;
      }
      const job = await startWorkflowJob(config);
      sendJson(res, 202, { ok: true, job });
    } catch (error) {
      const message = error && error.message ? error.message : "无法创建网页工作流";
      sendJson(res, message.includes("已有网页工作流") ? 409 : 400, { ok: false, error: message });
    }
    return;
  }

  const limitMatch = urlPath.match(/^\/api\/jobs\/([^/]+)\/limit$/);
  if (limitMatch && req.method === "POST") {
    const job = jobs.get(decodeURIComponent(limitMatch[1]));
    if (!job) {
      sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" });
      return;
    }
    if (job.status !== "awaiting_limit") {
      sendJson(res, 409, { ok: false, error: "当前任务未在等待选择处理数量" });
      return;
    }
    const maximum = trackLimitMaximum(job);
    try {
      const body = await readJsonBody(req);
      const limit = body.limit;
      if (typeof limit !== "number" || !Number.isInteger(limit) || limit < 1) {
        sendJson(res, 400, { ok: false, error: "处理数量必须是正整数" });
        return;
      }
      if (maximum !== null && limit > maximum) {
        sendJson(res, 400, { ok: false, error: `处理数量不能超过歌单曲目数 ${maximum}` });
        return;
      }
      const requestPath = path.join(job.runtime_dir, LIMIT_REQUEST_FILENAME);
      const temporary = `${requestPath}.${process.pid}.tmp`;
      await fs.promises.writeFile(temporary, JSON.stringify({ limit }), "utf8");
      await fs.promises.rename(temporary, requestPath);
      sendJson(res, 202, { ok: true, job: publicJob(job), limit });
    } catch (error) {
      sendJson(res, 400, { ok: false, error: error && error.message ? error.message : "无法提交处理数量" });
    }
    return;
  }

  const cancelMatch = urlPath.match(/^\/api\/jobs\/([^/]+)\/cancel$/);
  if (cancelMatch && req.method === "POST") {
    const job = jobs.get(decodeURIComponent(cancelMatch[1]));
    if (!job) {
      sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" });
      return;
    }
    if (!job.child) {
      sendJson(res, 409, { ok: false, error: "当前任务已经结束" });
      return;
    }
    job.child.kill();
    recordJobEvent(job, { event: "failed", status: "failed", stage: job.stage, error: "任务已取消" });
    sendJson(res, 200, { ok: true, job: publicJob(job) });
    return;
  }

  const jobMatch = urlPath.match(/^\/api\/jobs\/([^/]+)(\/events)?$/);
  if (jobMatch && req.method === "GET") {
    const job = jobs.get(decodeURIComponent(jobMatch[1]));
    if (!job) {
      sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" });
      return;
    }
    if (jobMatch[2] === "/events") {
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      });
      res.write(`data: ${JSON.stringify({ ok: true, job: publicJob(job) })}\n\n`);
      const subscribers = jobSubscribers.get(job.id) || new Set();
      subscribers.add(res);
      jobSubscribers.set(job.id, subscribers);
      req.on("close", () => {
        subscribers.delete(res);
        if (!subscribers.size) jobSubscribers.delete(job.id);
      });
    } else {
      sendJson(res, 200, { ok: true, job: publicJob(job) });
    }
    return;
  }

  /* 当前 Atlas 数据：只读脱敏视图模型，不直接暴露 runtime */
  if (urlPath === "/api/atlas") {
    try {
      const payload = await loadAtlasPayload();
      res.writeHead(200, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
      });
      res.end(JSON.stringify({ ok: true, ...payload }));
    } catch {
      res.writeHead(503, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: false, error: "Atlas 数据暂不可用" }));
    }
    return;
  }

  if (urlPath === "/api/health") {
    let dataAvailable = false;
    try {
      await loadAtlasPayload();
      dataAvailable = true;
    } catch {}
    res.writeHead(dataAvailable ? 200 : 503, {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    });
    // 身份字段（service/pid/web_root）供 atlas start/stop 防护模型使用：
    // 只有身份匹配的本项目面板才会被复用或停止，不误伤同端口的其他服务。
    res.end(JSON.stringify({
      ok: dataAvailable,
      data_available: dataAvailable,
      service: "music-atlas-web",
      pid: process.pid,
      host: HOST,
      port: PORT,
      web_root: PROJECT_ROOT,
      config_file: path.relative(PROJECT_ROOT, CONFIG_PATH),
    }));
    return;
  }

  /* 元信息接口 */
  if (urlPath === "/api/meta") {
    const artist = (query.get("artist") || "").trim();
    const track = (query.get("track") || "").trim();
    if (!artist || !track) {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: false, error: "artist & track required" }));
      return;
    }
    try {
      const data = await resolveMeta(artist, track);
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache" });
      res.end(JSON.stringify({ ok: true, ...data }));
    } catch (e) {
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: false, cover: null, links: null, error: e.message }));
    }
    return;
  }

  /* 静态文件 */
  if (urlPath === "/data" || urlPath.startsWith("/data/")) {
    res.writeHead(403).end("Forbidden");
    return;
  }
  if (urlPath === "/" || urlPath === "") urlPath = "/" + DEFAULT_FILE;

  const filePath = path.normalize(path.join(ROOT, urlPath));
  if (!filePath.startsWith(ROOT + path.sep) && filePath !== ROOT) {
    res.writeHead(403).end("Forbidden");
    return;
  }

  fs.stat(filePath, (err, st) => {
    if (err || !st.isFile()) {
      res.writeHead(404, { "Content-Type": "text/html; charset=utf-8" });
      res.end(`<!doctype html><meta charset="utf-8"><body style="background:#0f0d0a;color:#a39a88;font-family:'Cormorant Garamond','思源宋体 CN',serif;display:grid;place-items:center;height:100vh;margin:0"><div>404 · 本期内无此页<br><br><a href="/" style="color:#c8622f">← 回到本期</a></div>`);
      return;
    }
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, {
      "Content-Type": MIME[ext] || "application/octet-stream",
      "Content-Length": st.size,
      "Cache-Control": "no-cache",
    });
    fs.createReadStream(filePath).pipe(res);
  });
});

server.on("error", (err) => {
  if (err.code === "EADDRINUSE") {
    console.error(`[music-atlas] 端口 ${PORT} 已被占用，可能服务已在运行：http://${HOST}:${PORT}`);
    process.exit(0);
  }
  console.error("[music-atlas] 启动失败:", err.message);
  process.exit(1);
});

server.listen(PORT, HOST, () => {
  console.log(`[music-atlas] Editorial Atlas 已固化: http://${HOST}:${PORT}`);
});
