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
const { createAuthStore } = require("./auth_store");

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

const SETTINGS_PATH = process.env.ATLAS_WEB_SETTINGS
  ? path.resolve(process.env.ATLAS_WEB_SETTINGS)
  : (process.env.ATLAS_WEB_CONFIG
    ? path.join(path.dirname(CONFIG_PATH), "settings.json")
    : path.join(PROJECT_ROOT, "runtime", "web", "settings.json"));
// 测试可用 ATLAS_WEB_SECRET_SCRIPT 指向夹具脚本，避免触碰真实系统密钥库。
const SECRET_STORE_SCRIPT = process.env.ATLAS_WEB_SECRET_SCRIPT
  ? path.resolve(process.env.ATLAS_WEB_SECRET_SCRIPT)
  : path.join(PROJECT_ROOT, "secret_store.py");
const AUTH_DB_PATH = process.env.ATLAS_AUTH_DB
  ? path.resolve(process.env.ATLAS_AUTH_DB)
  : path.join(PROJECT_ROOT, "runtime", "web", "auth.sqlite");
const AUTH_REQUIRED = String(process.env.ATLAS_AUTH_REQUIRED || (process.env.NODE_ENV === "production" ? "1" : "0")) !== "0";
const USER_SESSION_COOKIE = "atlas_session";
const ADMIN_SESSION_COOKIE = "atlas_admin_session";
const authStore = createAuthStore(AUTH_DB_PATH);
const DEFAULT_CRAWLER_SETTINGS = {
  request_timeout_seconds: 30,
  request_retries: 1,
  user_agent: "MusicAtlas/1.0 (+local)",
  netease_detail_url: "https://music.163.com/api/v6/playlist/detail",
  netease_song_detail_url: "https://music.163.com/api/v3/song/detail",
  netease_referer: "https://music.163.com/",
  netease_song_detail_batch_size: 200,
  qq_page_size: 100,
  qq_max_pages: 50,
  qq_musicu_url: "https://u.y.qq.com/cgi-bin/musicu.fcg",
  qq_referer: "https://y.qq.com/",
  apple_export_timeout_seconds: 600,
};
const TRACK_PERCENTILE_OPTIONS = [0.25, 0.5, 1];
const DEFAULT_WORKFLOW_SETTINGS = {
  analysis_parallelism: 5,
  recommendation_parallelism: 4,
  analysis_timeout_seconds: 7200,
  recommendation_timeout_seconds: 3600,
  track_percentile_default: 1,
  await_limit_timeout_seconds: 1800,
  max_research_rounds: 2,
  max_candidates: 60,
  // 默认先收集足够候选再排序，避免 candidate_pool_min=1 时过早结束，
  // 使项目覆盖和多样性只能依赖极少的已核验歌曲。
  candidate_target: 10,
  analysis_batch_size: 10,
  analysis_context_budget: null,
  context_budget: null,
};
const DEFAULT_OPENAI_SETTINGS = {
  base_url: "",
  model: "",
  timeout_seconds: 1800,
  max_tokens: 60000,
  temperature: 0.2,
  disable_thinking: true,
};
const DEFAULT_POLICY_SETTINGS = {
  max_per_artist: 2,
  max_per_project: 3,
  min_projects: 6,
  candidate_pool_min: 1,
};
const DEFAULT_EDITORIAL_SETTINGS = {
  title: "",
  lede: "",
};

function isObject(value) {
  return value && typeof value === "object" && !Array.isArray(value);
}

function deepMerge(base, override) {
  const result = isObject(base) ? { ...base } : {};
  for (const [key, value] of Object.entries(override || {})) {
    result[key] = isObject(result[key]) && isObject(value)
      ? deepMerge(result[key], value)
      : value;
  }
  return result;
}

/* 已删除的历史设置字段：加载时自动剥离，避免旧覆盖文件让服务无法启动。 */
function migrateSettingsOverride(value) {
  if (!isObject(value)) return { value, changed: false };
  const result = JSON.parse(JSON.stringify(value));
  let changed = false;
  if (isObject(result.workflow)) {
    for (const key of ["track_limit_options", "track_limit_default"]) {
      if (key in result.workflow) {
        delete result.workflow[key];
        changed = true;
      }
    }
    if (!Object.keys(result.workflow).length) {
      delete result.workflow;
      changed = true;
    }
  }
  if (isObject(result.runtime) && isObject(result.runtime.openai_compat)) {
    if ("api_key_env" in result.runtime.openai_compat) {
      delete result.runtime.openai_compat.api_key_env;
      changed = true;
    }
    if (!Object.keys(result.runtime.openai_compat).length) {
      delete result.runtime.openai_compat;
      changed = true;
    }
    if (!Object.keys(result.runtime).length) {
      delete result.runtime;
      changed = true;
    }
  }
  return { value: result, changed };
}

function loadSettingsOverride() {
  try {
    if (!fs.existsSync(SETTINGS_PATH)) return {};
    const raw = JSON.parse(fs.readFileSync(SETTINGS_PATH, "utf8"));
    if (!isObject(raw)) throw new Error("设置文件必须是 JSON 对象");
    const { value, changed } = migrateSettingsOverride(raw);
    validateSettingsPatch(value);
    // 旧字段已在内存中剥离，同步清理磁盘文件，避免每次启动都重复迁移。
    if (changed) writeSettingsOverride(value);
    return value;
  } catch (error) {
    throw new Error(`无法读取网页设置 ${SETTINGS_PATH}：${error.message}`);
  }
}

function loadEffectiveWebConfig() {
  return deepMerge(loadWebConfig(), loadSettingsOverride());
}

function settingInteger(value, field, minimum, maximum) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${field} 必须是 ${minimum} 到 ${maximum} 的整数`);
  }
  return value;
}

function settingOptionalInteger(value, field, minimum, maximum) {
  if (value === null) return null;
  return settingInteger(value, field, minimum, maximum);
}

function settingText(value, field, maximum = 300) {
  if (typeof value !== "string" || !value.trim() || value.length > maximum || /[\r\n]/.test(value)) throw new Error(`${field} 必须是有效文本`);
  return value.trim();
}

function settingEndpoint(value, field, hostname) {
  const text = settingText(value, field, 500).replace(/\/$/, "");
  let parsed;
  try { parsed = new URL(text); } catch { throw new Error(`${field} 必须是有效 HTTPS 地址`); }
  if (parsed.protocol !== "https:" || parsed.hostname.toLowerCase().replace(/\.$/, "") !== hostname) throw new Error(`${field} 必须使用受支持域名的 HTTPS 地址`);
  return text;
}

function validateSettingsPatch(value) {
  if (!isObject(value)) throw new Error("settings 必须是 JSON 对象");
  const topAllowed = new Set(["crawler", "runtime", "workflow", "recommendation_policy", "editorial"]);
  for (const key of Object.keys(value)) {
    if (!topAllowed.has(key)) throw new Error(`设置不允许修改：${key}`);
  }
  const output = {};
  if (value.crawler !== undefined) {
    if (!isObject(value.crawler)) throw new Error("crawler 必须是对象");
    const allowed = new Set(Object.keys(DEFAULT_CRAWLER_SETTINGS));
    for (const key of Object.keys(value.crawler)) if (!allowed.has(key)) throw new Error(`crawler 不允许字段：${key}`);
    output.crawler = {};
    output.crawler.request_timeout_seconds = value.crawler.request_timeout_seconds === undefined ? undefined : settingInteger(value.crawler.request_timeout_seconds, "crawler.request_timeout_seconds", 1, 86400);
    output.crawler.request_retries = value.crawler.request_retries === undefined ? undefined : settingInteger(value.crawler.request_retries, "crawler.request_retries", 0, 3);
    output.crawler.user_agent = value.crawler.user_agent === undefined ? undefined : settingText(value.crawler.user_agent, "crawler.user_agent", 200);
    output.crawler.netease_detail_url = value.crawler.netease_detail_url === undefined ? undefined : settingEndpoint(value.crawler.netease_detail_url, "crawler.netease_detail_url", "music.163.com");
    output.crawler.netease_song_detail_url = value.crawler.netease_song_detail_url === undefined ? undefined : settingEndpoint(value.crawler.netease_song_detail_url, "crawler.netease_song_detail_url", "music.163.com");
    output.crawler.netease_referer = value.crawler.netease_referer === undefined ? undefined : settingEndpoint(value.crawler.netease_referer, "crawler.netease_referer", "music.163.com");
    output.crawler.netease_song_detail_batch_size = value.crawler.netease_song_detail_batch_size === undefined ? undefined : settingInteger(value.crawler.netease_song_detail_batch_size, "crawler.netease_song_detail_batch_size", 1, 1000);
    output.crawler.qq_page_size = value.crawler.qq_page_size === undefined ? undefined : settingInteger(value.crawler.qq_page_size, "crawler.qq_page_size", 1, 1000);
    output.crawler.qq_max_pages = value.crawler.qq_max_pages === undefined ? undefined : settingInteger(value.crawler.qq_max_pages, "crawler.qq_max_pages", 1, 500);
    output.crawler.qq_musicu_url = value.crawler.qq_musicu_url === undefined ? undefined : settingEndpoint(value.crawler.qq_musicu_url, "crawler.qq_musicu_url", "u.y.qq.com");
    output.crawler.qq_referer = value.crawler.qq_referer === undefined ? undefined : settingEndpoint(value.crawler.qq_referer, "crawler.qq_referer", "y.qq.com");
    output.crawler.apple_export_timeout_seconds = value.crawler.apple_export_timeout_seconds === undefined ? undefined : settingInteger(value.crawler.apple_export_timeout_seconds, "crawler.apple_export_timeout_seconds", 1, 3600);
    for (const key of Object.keys(output.crawler)) if (output.crawler[key] === undefined) delete output.crawler[key];
  }
  if (value.runtime !== undefined) {
    if (!isObject(value.runtime) || !isObject(value.runtime.openai_compat)) throw new Error("runtime.openai_compat 必须是对象");
    if (Object.keys(value.runtime).some((key) => key !== "openai_compat")) throw new Error("runtime 只允许修改 openai_compat");
    const settings = value.runtime.openai_compat;
    const allowed = new Set(Object.keys(DEFAULT_OPENAI_SETTINGS));
    for (const key of Object.keys(settings)) if (!allowed.has(key)) throw new Error(`runtime.openai_compat 不允许字段：${key}`);
    output.runtime = { openai_compat: {} };
    if (settings.base_url !== undefined) {
      if (typeof settings.base_url !== "string" || !/^https?:\/\//i.test(settings.base_url.trim())) throw new Error("runtime.openai_compat.base_url 必须是 HTTP(S) 地址");
      output.runtime.openai_compat.base_url = settings.base_url.trim().replace(/\/$/, "");
    }
    if (settings.model !== undefined) {
      if (typeof settings.model !== "string" || !settings.model.trim() || settings.model.length > 200) throw new Error("runtime.openai_compat.model 无效");
      output.runtime.openai_compat.model = settings.model.trim();
    }
    if (settings.timeout_seconds !== undefined) output.runtime.openai_compat.timeout_seconds = settingInteger(settings.timeout_seconds, "runtime.openai_compat.timeout_seconds", 1, 86400);
    if (settings.max_tokens !== undefined) output.runtime.openai_compat.max_tokens = settingInteger(settings.max_tokens, "runtime.openai_compat.max_tokens", 1, 200000);
    if (settings.temperature !== undefined) {
      if (typeof settings.temperature !== "number" || !Number.isFinite(settings.temperature) || settings.temperature < 0 || settings.temperature > 2) throw new Error("runtime.openai_compat.temperature 必须是 0 到 2 的数字");
      output.runtime.openai_compat.temperature = settings.temperature;
    }
    if (settings.disable_thinking !== undefined) {
      if (typeof settings.disable_thinking !== "boolean") throw new Error("runtime.openai_compat.disable_thinking 必须是布尔值");
      output.runtime.openai_compat.disable_thinking = settings.disable_thinking;
    }
  }
  if (value.recommendation_policy !== undefined) {
    if (!isObject(value.recommendation_policy)) throw new Error("recommendation_policy 必须是对象");
    const allowed = new Set(Object.keys(DEFAULT_POLICY_SETTINGS));
    for (const key of Object.keys(value.recommendation_policy)) if (!allowed.has(key)) throw new Error(`recommendation_policy 不允许字段：${key}`);
    output.recommendation_policy = {};
    const ranges = { max_per_artist: [1, 10], max_per_project: [1, 20], min_projects: [1, 50], candidate_pool_min: [1, 200] };
    for (const [key, range] of Object.entries(ranges)) if (value.recommendation_policy[key] !== undefined) output.recommendation_policy[key] = settingInteger(value.recommendation_policy[key], `recommendation_policy.${key}`, range[0], range[1]);
  }
  if (value.editorial !== undefined) {
    if (!isObject(value.editorial)) throw new Error("editorial 必须是对象");
    const allowed = new Set(Object.keys(DEFAULT_EDITORIAL_SETTINGS));
    for (const key of Object.keys(value.editorial)) if (!allowed.has(key)) throw new Error(`editorial 不允许字段：${key}`);
    output.editorial = {};
    for (const key of Object.keys(DEFAULT_EDITORIAL_SETTINGS)) if (value.editorial[key] !== undefined) {
      const text = value.editorial[key];
      const limit = key === "title" ? 120 : 2000;
      if (typeof text !== "string" || text.length > limit) throw new Error(`editorial.${key} 必须是不超过 ${limit} 个字符的文本`);
      output.editorial[key] = text.trim();
    }
  }
  if (value.workflow !== undefined) {
    if (!isObject(value.workflow)) throw new Error("workflow 必须是对象");
    const allowed = new Set(Object.keys(DEFAULT_WORKFLOW_SETTINGS));
    for (const key of Object.keys(value.workflow)) if (!allowed.has(key)) throw new Error(`workflow 不允许字段：${key}`);
    const workflow = value.workflow;
    output.workflow = {};
    const ints = {
      analysis_parallelism: [1, 16], recommendation_parallelism: [1, 8],
      analysis_timeout_seconds: [1, 86400], recommendation_timeout_seconds: [1, 86400],
      await_limit_timeout_seconds: [1, 86400],
      max_research_rounds: [1, 3], max_candidates: [1, 200], analysis_batch_size: [1, 50],
    };
    for (const [key, range] of Object.entries(ints)) if (workflow[key] !== undefined) output.workflow[key] = settingInteger(workflow[key], `workflow.${key}`, range[0], range[1]);
    if (workflow.candidate_target !== undefined) output.workflow.candidate_target = settingOptionalInteger(workflow.candidate_target, "workflow.candidate_target", 1, 200);
    if (workflow.analysis_context_budget !== undefined) output.workflow.analysis_context_budget = settingOptionalInteger(workflow.analysis_context_budget, "workflow.analysis_context_budget", 1000, 1000000);
    if (workflow.context_budget !== undefined) output.workflow.context_budget = settingOptionalInteger(workflow.context_budget, "workflow.context_budget", 1000, 1000000);
    if (workflow.track_percentile_default !== undefined) {
      const percentile = Number(workflow.track_percentile_default);
      if (!TRACK_PERCENTILE_OPTIONS.includes(percentile)) throw new Error("workflow.track_percentile_default 必须是 0.25、0.5 或 1");
      output.workflow.track_percentile_default = percentile;
    }
  }
  return output;
}

function writeSettingsOverride(value) {
  fs.mkdirSync(path.dirname(SETTINGS_PATH), { recursive: true });
  const temporary = `${SETTINGS_PATH}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2) + "\n", "utf8");
  fs.renameSync(temporary, SETTINGS_PATH);
}

function editableSettings(config) {
  const runtime = isObject(config.runtime) ? config.runtime : {};
  const openai = isObject(runtime.openai_compat) ? runtime.openai_compat : {};
  const workflow = isObject(config.workflow) ? config.workflow : {};
  return {
    crawler: { ...DEFAULT_CRAWLER_SETTINGS, ...(isObject(config.crawler) ? config.crawler : {}) },
    runtime: { openai_compat: { ...DEFAULT_OPENAI_SETTINGS, ...openai } },
    workflow: { ...DEFAULT_WORKFLOW_SETTINGS, ...workflow },
    recommendation_policy: { ...DEFAULT_POLICY_SETTINGS, ...(isObject(config.recommendation_policy) ? config.recommendation_policy : {}) },
    editorial: { ...DEFAULT_EDITORIAL_SETTINGS, ...(isObject(config.editorial) ? config.editorial : {}) },
  };
}

function runtimeConfigSnapshot() {
  const config = loadEffectiveWebConfig();
  const workflow = config.workflow || {};
  const runtime = config.runtime || {};
  const python = resolveConfiguredExecutable(runtime.python);
  const analysisParallelism = settingInteger(Number(workflow.analysis_parallelism ?? 5), "workflow.analysis_parallelism", 1, 16);
  const recommendationParallelism = settingInteger(Number(workflow.recommendation_parallelism ?? 4), "workflow.recommendation_parallelism", 1, 8);
  const analysisTimeoutSeconds = settingInteger(Number(workflow.analysis_timeout_seconds ?? 600), "workflow.analysis_timeout_seconds", 1, 86400);
  const recommendationTimeoutSeconds = settingInteger(Number(workflow.recommendation_timeout_seconds ?? 600), "workflow.recommendation_timeout_seconds", 1, 86400);
  const trackPercentileDefault = Number(workflow.track_percentile_default ?? DEFAULT_WORKFLOW_SETTINGS.track_percentile_default);
  if (!TRACK_PERCENTILE_OPTIONS.includes(trackPercentileDefault)) throw new Error("workflow.track_percentile_default 必须是 0.25、0.5 或 1");
  const awaitLimitTimeoutSeconds = settingInteger(Number(workflow.await_limit_timeout_seconds ?? 1800), "workflow.await_limit_timeout_seconds", 1, 86400);
  const maxResearchRounds = settingInteger(Number(workflow.max_research_rounds ?? 2), "workflow.max_research_rounds", 1, 3);
  const maxCandidates = settingInteger(Number(workflow.max_candidates ?? 80), "workflow.max_candidates", 1, 200);
  const candidateTarget = workflow.candidate_target === null || workflow.candidate_target === undefined ? null : settingInteger(Number(workflow.candidate_target), "workflow.candidate_target", 1, 200);
  const executors = config.executors || {};
  const analysisExecutor = configureExecutor(executors.analysis, "executors.analysis", python);
  const recommendationExecutor = configureExecutor(executors.recommendation, "executors.recommendation", python);
  return {
    config,
    python,
    analysisExecutor,
    recommendationExecutor,
    analysisParallelism,
    recommendationParallelism,
    analysisTimeoutSeconds,
    recommendationTimeoutSeconds,
    trackPercentileOptions: [...TRACK_PERCENTILE_OPTIONS],
    trackPercentileDefault,
    awaitLimitTimeoutSeconds,
    maxResearchRounds,
    maxCandidates,
    candidateTarget,
    analysisBatchSize: workflow.analysis_batch_size === null || workflow.analysis_batch_size === undefined ? null : settingInteger(Number(workflow.analysis_batch_size), "workflow.analysis_batch_size", 1, 50),
    analysisContextBudget: workflow.analysis_context_budget === null || workflow.analysis_context_budget === undefined ? null : settingInteger(Number(workflow.analysis_context_budget), "workflow.analysis_context_budget", 1000, 1000000),
    contextBudget: workflow.context_budget === null || workflow.context_budget === undefined ? null : settingInteger(Number(workflow.context_budget), "workflow.context_budget", 1000, 1000000),
    recommendationPolicy: isObject(config.recommendation_policy) ? config.recommendation_policy : null,
    editorial: isObject(config.editorial) ? config.editorial : null,
  };
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
  const configured = typeof value === "string" && value.trim() ? value.trim() : "";
  const executable = configured || (process.platform === "win32" ? "python" : "python3");
  if (path.isAbsolute(executable) || executable.includes("/") || executable.includes("\\")) {
    return resolveProjectPath(executable, "runtime.python");
  }
  // Ubuntu 发行版通常只提供 python3；保留配置中的 python 兼容旧配置，
  // 但在 PATH 中没有 python 时自动切换，避免 systemd 下密钥库和工作流启动失败。
  if (process.platform !== "win32" && executable === "python" && !commandAvailable("python") && commandAvailable("python3")) {
    return "python3";
  }
  return executable;
}

function commandAvailable(command) {
  const pathValue = String(process.env.PATH || "");
  return pathValue.split(path.delimiter).some((directory) => {
    if (!directory) return false;
    try {
      return fs.statSync(path.join(directory, command)).isFile();
    } catch {
      return false;
    }
  });
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

const WEB_CONFIG = loadEffectiveWebConfig();
const SERVER_CONFIG = WEB_CONFIG.server || {};
const PATH_CONFIG = WEB_CONFIG.paths || {};
const HOST = typeof SERVER_CONFIG.host === "string" && SERVER_CONFIG.host.trim()
  ? SERVER_CONFIG.host.trim() : "127.0.0.1";
// ATLAS_WEB_PORT 供服务管理器（atlas start）与测试覆盖端口；默认仍读配置。
const PORT = Number(process.env.ATLAS_WEB_PORT) || Number(SERVER_CONFIG.port) || 8420;
if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) throw new Error("server.port 必须是 1 到 65535 的整数");
const DATA_PATH = resolveProjectPath(PATH_CONFIG.published || "runtime/web/current.json", "paths.published");
const JOB_ROOT = resolveProjectPath(PATH_CONFIG.jobs || "runtime/web-jobs", "paths.jobs");
const INPUT_ROOT = resolveProjectPath(PATH_CONFIG.input || "input", "paths.input");
const INITIAL_RUNTIME = runtimeConfigSnapshot();
const PYTHON = INITIAL_RUNTIME.python;
// 与 web_workflow.py 的 LIMIT_REQUEST_FILENAME 保持一致：网页写请求，工作流读请求。
const LIMIT_REQUEST_FILENAME = "requested_track_limit.json";
const JOB_STATE_FILENAME = "web_job_state.json";
const jobs = new Map();
const jobSubscribers = new Map();
let activeJobId = null;
let latestJobId = null;

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

function parseCookies(req) {
  const header = String(req.headers.cookie || "");
  const cookies = {};
  for (const part of header.split(";")) {
    const index = part.indexOf("=");
    if (index < 0) continue;
    const key = part.slice(0, index).trim();
    if (!key) continue;
    try { cookies[key] = decodeURIComponent(part.slice(index + 1).trim()); } catch { cookies[key] = part.slice(index + 1).trim(); }
  }
  return cookies;
}

function cookieSecure(req) {
  return process.env.ATLAS_COOKIE_SECURE === "1"
    || String(req.headers["x-forwarded-proto"] || "").toLowerCase() === "https";
}

function setSessionCookie(res, req, name, token, maxAge) {
  const parts = [`${name}=${encodeURIComponent(token)}`, "Path=/", "HttpOnly", "SameSite=Lax", `Max-Age=${maxAge}`];
  if (cookieSecure(req)) parts.push("Secure");
  res.setHeader("Set-Cookie", parts.join("; "));
}

function clearSessionCookie(res, req, name) {
  const parts = [`${name}=`, "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"];
  if (cookieSecure(req)) parts.push("Secure");
  res.setHeader("Set-Cookie", parts.join("; "));
}

function currentUser(req, kind = "user") {
  const cookieName = kind === "admin" ? ADMIN_SESSION_COOKIE : USER_SESSION_COOKIE;
  return authStore.getSessionUser(parseCookies(req)[cookieName], kind);
}

function rejectAuth(res, status, error) {
  sendJson(res, status, { ok: false, error });
  return null;
}

function requireUser(req, res, { allowAnonymous = false } = {}) {
  const user = currentUser(req, "user");
  if (user) return user;
  if (allowAnonymous || !AUTH_REQUIRED) return null;
  return rejectAuth(res, 401, "请先登录后再运行工作流");
}

function requireAdmin(req, res) {
  const user = currentUser(req, "admin");
  if (user && user.role === "admin") return user;
  rejectAuth(res, 404, "页面不存在");
  return null;
}

function canAccessJob(job, user, { admin = false } = {}) {
  if (!job) return false;
  if (admin && user && user.role === "admin") return true;
  if (!AUTH_REQUIRED && !job.user_id) return true;
  return Boolean(user && job.user_id && Number(job.user_id) === Number(user.id));
}

/* 跨平台系统密钥库：通过 python secret_store.py 读写（Windows Credential Manager / Keychain / Secret Service）。 */
function runSecretStore(args, { input } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [SECRET_STORE_SCRIPT, ...args], {
      cwd: PROJECT_ROOT,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve({ stdout: stdout.trim(), stderr });
      else reject(new Error((stderr || stdout).trim() || `密钥库命令失败（code=${code}）`));
    });
    child.stdin.end(input === undefined ? "" : input);
  });
}

async function secretStatus() {
  try {
    const result = await runSecretStore(["status"]);
    const value = JSON.parse(result.stdout);
    if (!value || typeof value !== "object") throw new Error("密钥库状态解析失败");
    return { ...value, writable: true };
  } catch (error) {
    // 云服务器可能没有桌面密钥库；环境变量仍是受 systemd 保护的只读回退来源。
    const configured = Boolean(String(process.env.MUSIC_ATLAS_API_KEY || "").trim());
    return {
      configured,
      backend: configured ? "environment" : "unavailable",
      writable: false,
      warning: configured
        ? "系统密钥库不可用，当前使用服务器环境变量；保存或清除需要先配置安全密钥库"
        : (error?.message || "系统密钥库不可用"),
    };
  }
}

/* API Key 来源：系统密钥库优先（网页保存，改动立即生效），其次固定环境变量。 */
async function readApiKey() {
  const stored = await runSecretStore(["get"]).then((result) => result.stdout).catch(() => "");
  if (stored) return { key: stored, source: "keyring" };
  const envValue = String(process.env.MUSIC_ATLAS_API_KEY || "").trim();
  if (envValue) return { key: envValue, source: "environment" };
  return { key: "", source: "none" };
}

/* 连通性探测：用当前有效配置发一个最小 Chat Completions 请求，不写入任何产物。 */
async function probeAiConnection() {
  const config = loadEffectiveWebConfig();
  const compat = editableSettings(config).runtime.openai_compat;
  const baseUrl = String(compat.base_url || "").trim().replace(/\/$/, "");
  const model = String(compat.model || "").trim();
  if (!baseUrl || !model) throw new Error("请先填写并保存 AI 接口地址与模型");
  const { key, source } = await readApiKey();
  if (!key) throw new Error("未配置 API Key：请在设置中输入并保存，或设置 MUSIC_ATLAS_API_KEY 环境变量");
  const configuredTimeout = Number(compat.timeout_seconds);
  const timeoutSeconds = Number.isInteger(configuredTimeout) && configuredTimeout > 0
    ? Math.min(configuredTimeout, 60) : 30;
  const payload = {
    model,
    messages: [{ role: "user", content: "ping" }],
    max_tokens: 16,
    temperature: 0,
  };
  if (compat.disable_thinking !== false) payload.thinking = { type: "disabled" };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutSeconds * 1000);
  const startedAt = Date.now();
  try {
    const response = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const latencyMs = Date.now() - startedAt;
    if (!response.ok) {
      const detail = (await response.text().catch(() => "")).trim().slice(0, 300);
      throw new Error(`上游返回 HTTP ${response.status}${detail ? `：${detail}` : ""}`);
    }
    const body = await response.json().catch(() => null);
    const choices = body && Array.isArray(body.choices) ? body.choices : [];
    const message = choices[0] && choices[0].message;
    const content = message && typeof message.content === "string" ? message.content : "";
    return {
      model,
      base_url: baseUrl,
      key_source: source,
      latency_ms: latencyMs,
      reply_preview: content.trim().slice(0, 80),
    };
  } catch (error) {
    if (error && error.name === "AbortError") throw new Error(`连接超时（${timeoutSeconds} 秒）`);
    if (error && error.message && /^上游返回 HTTP/.test(error.message)) throw error;
    throw new Error(`无法连接 ${baseUrl}：${error && error.message ? error.message : error}`);
  } finally {
    clearTimeout(timer);
  }
}

async function saveApiKey(apiKey) {
  await runSecretStore(["set"], { input: apiKey + "\n" });
}

async function clearApiKey() {
  await runSecretStore(["delete"]);
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
  const workflowMode = job.workflow_mode || (Array.isArray(job.events) && job.events.some((event) => event && event.reused_analysis_id)
    ? "recommendation_only" : "full");
  return {
    id: job.id,
    status: job.status,
    stage: job.stage,
    created_at: job.created_at,
    updated_at: job.updated_at,
    events: job.events,
    stderr_tail: job.stderr_tail || "",
    exit_code: job.exit_code === undefined ? null : job.exit_code,
    workflow_mode: workflowMode,
  };
}

function persistJobState(job) {
  try {
    fs.mkdirSync(job.runtime_dir, { recursive: true });
    fs.writeFileSync(path.join(job.runtime_dir, JOB_STATE_FILENAME), JSON.stringify({ ...publicJob(job), user_id: job.user_id || null }, null, 2) + "\n", "utf8");
  } catch (error) {
    console.error(`无法保存任务状态 ${job.id}：${error.message}`);
  }
}

function restoredLegacyJob(id, runtimeDir) {
  const reportPath = path.join(runtimeDir, "web_job_report.json");
  if (!fs.existsSync(reportPath)) return null;
  try {
    const report = JSON.parse(fs.readFileSync(reportPath, "utf8"));
    if (!report || report.status !== "completed") return null;
    const stat = fs.statSync(runtimeDir);
    const createdAt = stat.birthtime.toISOString();
    const updatedAt = report.completed_at || stat.mtime.toISOString();
    const trackCount = Number(report.source_track_count) || 0;
    const recommendationCount = Number(report.web_export && report.web_export.recommendation_count) || 0;
    const candidateCount = Number(report.platform_discovery && report.platform_discovery.candidate_count) || 0;
    const events = [
      { event: "task_completed", status: "running", stage: "snapshot", task_kind: "snapshot_import", task_id: "snapshot-import", task_status: "validated", track_completed: trackCount, track_total: trackCount, at: createdAt, seq: 1 },
      { event: "task_completed", status: "running", stage: "analysis", task_kind: "analysis_aggregate", task_id: "analysis-aggregate", task_status: "validated", completed: 1, total: 1, track_completed: trackCount, track_total: trackCount, source_track_count: trackCount, at: updatedAt, seq: 2 },
      { event: "task_completed", status: "running", stage: "recommendation", task_kind: "platform_discovery", task_id: "platform-discovery", task_status: "validated", candidate_count: candidateCount, at: updatedAt, seq: 3 },
      { event: "completed", status: "completed", stage: "export", recommendation_count: recommendationCount, at: updatedAt, seq: 4 },
    ];
    return { id, status: "completed", stage: "export", created_at: createdAt, updated_at: updatedAt,
      runtime_dir: runtimeDir, user_id: null, events, stderr_tail: "", exit_code: 0, event_seq: events.length,
      workflow_mode: report.reused_analysis_id ? "recommendation_only" : "full" };
  } catch {
    return null;
  }
}

function restoreRecentJobs() {
  if (!fs.existsSync(JOB_ROOT)) return;
  const restored = [];
  for (const entry of fs.readdirSync(JOB_ROOT, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const runtimeDir = path.join(JOB_ROOT, entry.name);
    let job = null;
    const statePath = path.join(runtimeDir, JOB_STATE_FILENAME);
    try {
      if (fs.existsSync(statePath)) {
        const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
        if (state && state.id === entry.name && ["completed", "failed"].includes(state.status) && Array.isArray(state.events)) {
          job = { ...state, runtime_dir: runtimeDir, user_id: state.user_id || null, event_seq: Math.max(0, ...state.events.map((event) => Number(event.seq) || 0)) };
        }
      }
    } catch {}
    job = job || restoredLegacyJob(entry.name, runtimeDir);
    if (job) restored.push(job);
  }
  restored.sort((left, right) => Date.parse(right.updated_at || "") - Date.parse(left.updated_at || ""));
  for (const job of restored.slice(0, 20)) jobs.set(job.id, job);
  if (restored.length) latestJobId = restored[0].id;
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
  latestJobId = job.id;
  persistJobState(job);
  const subscribers = jobSubscribers.get(job.id) || new Set();
  for (const res of subscribers) {
    try {
      res.write(`data: ${JSON.stringify({ ok: true, job: publicJob(job), event: safeEvent })}\n\n`);
    } catch {}
  }
}

function latestCompletedJob(userId = null) {
  return Array.from(jobs.values())
    .filter((job) => job && job.status === "completed" && (userId == null ? !AUTH_REQUIRED : Number(job.user_id) === Number(userId)))
    .sort((left, right) => Date.parse(right.updated_at || "") - Date.parse(left.updated_at || ""))[0] || null;
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

async function startWorkflowJob(config, options = {}) {
  if (activeJobId) throw new Error("已有网页工作流正在运行，请等待其完成");
  const runtime = runtimeConfigSnapshot();
  await fs.promises.mkdir(JOB_ROOT, { recursive: true });
  const id = `${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-${crypto.randomBytes(4).toString("hex")}`;
  const runtimeDir = path.join(JOB_ROOT, id);
  await fs.promises.mkdir(runtimeDir);
  // 每次任务锁定完整有效配置；任务运行期间修改网页设置不会影响当前子进程。
  // 配置快照放在任务目录旁边；web_workflow 要求 runtime_dir 启动时为空。
  const configSnapshotPath = path.join(JOB_ROOT, `${id}.web_config.snapshot.json`);
  await fs.promises.writeFile(configSnapshotPath, JSON.stringify(runtime.config, null, 2) + "\n", "utf8");
  let policySnapshotPath = null;
  if (isObject(runtime.recommendationPolicy) && Object.keys(runtime.recommendationPolicy).length) {
    policySnapshotPath = path.join(JOB_ROOT, `${id}.recommendation_policy.snapshot.json`);
    await fs.promises.writeFile(policySnapshotPath, JSON.stringify(runtime.recommendationPolicy, null, 2) + "\n", "utf8");
  }
  let editorialSnapshotPath = null;
  if (isObject(runtime.editorial) && Object.keys(runtime.editorial).length) {
    editorialSnapshotPath = path.join(JOB_ROOT, `${id}.editorial.snapshot.json`);
    await fs.promises.writeFile(editorialSnapshotPath, JSON.stringify(runtime.editorial, null, 2) + "\n", "utf8");
  }
  const job = {
    id,
    status: "queued",
    stage: "queued",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    runtime_dir: runtimeDir,
    user_id: options.user && options.user.id ? Number(options.user.id) : null,
    events: [],
    stderr_tail: "",
    event_seq: 0,
    workflow_mode: options.recommendationOnly ? "recommendation_only" : "full",
  };
  jobs.set(id, job);
  activeJobId = id;
  latestJobId = id;
  recordJobEvent(job, { event: "queued", status: "queued", stage: "queued", workflow_mode: job.workflow_mode });

  const args = [
    WORKFLOW_SCRIPT,
    "--runtime-dir", runtimeDir,
    "--current-data", DATA_PATH,
    "--source-kind", config.kind,
    "--analysis-parallelism", String(runtime.analysisParallelism),
    "--recommendation-parallelism", String(runtime.recommendationParallelism),
    "--analysis-timeout", String(runtime.analysisTimeoutSeconds),
    "--recommendation-timeout", String(runtime.recommendationTimeoutSeconds),
    "--max-research-rounds", String(runtime.maxResearchRounds),
    "--max-candidates", String(runtime.maxCandidates),
    "--await-track-limit",
    "--await-limit-timeout", String(runtime.awaitLimitTimeoutSeconds),
  ];
  if (options.recommendationOnly) {
    args.push("--recommendation-only", "--source-runtime-dir", options.sourceRuntimeDir);
  }
  if (runtime.analysisBatchSize !== null && !options.recommendationOnly) args.push("--analysis-batch-size", String(runtime.analysisBatchSize));
  if (!options.recommendationOnly && runtime.analysisContextBudget !== null) args.push("--analysis-context-budget", String(runtime.analysisContextBudget));
  if (!options.recommendationOnly && runtime.contextBudget !== null) args.push("--context-budget", String(runtime.contextBudget));
  if (!options.recommendationOnly && runtime.candidateTarget !== null) args.push("--candidate-target", String(runtime.candidateTarget));
  if (!options.recommendationOnly && policySnapshotPath) args.push("--policy-file", policySnapshotPath);
  if (!options.recommendationOnly && editorialSnapshotPath) args.push("--editorial", editorialSnapshotPath);
  args.push("--analysis-command", runtime.analysisExecutor.command, "--recommendation-command", runtime.recommendationExecutor.command);
  // 歌单解析完成后只允许网页选择 25%、50% 或 100% 分位。
  if (!options.recommendationOnly) {
    if (config.source_url) args.push("--source-url", config.source_url);
    if (config.input) args.push("--input", config.input);
    if (config.playlist_id) args.push("--playlist-id", config.playlist_id);
    if (config.playlist_name) args.push("--playlist-name", config.playlist_name);
    if (config.platform) args.push("--platform", config.platform);
    if (config.expected_count !== null) args.push("--expected-count", String(config.expected_count));
  }
  const childEnvironment = { ...process.env };
  childEnvironment.ATLAS_WEB_CONFIG = configSnapshotPath;
  delete childEnvironment.ATLAS_WEB_SETTINGS;
  delete childEnvironment.ATLAS_WEB_ANALYSIS_COMMAND;
  delete childEnvironment.ATLAS_WEB_RECOMMENDATION_COMMAND;
  if (job.user_id) childEnvironment.MUSIC_ATLAS_USER_ID = String(job.user_id);
  // 从系统密钥库获取 API Key 并注入子进程环境变量，方便执行器直接读取。
  try {
    const apiKeyValue = await runSecretStore(["get"]).then((r) => r.stdout).catch(() => "");
    if (apiKeyValue) childEnvironment.MUSIC_ATLAS_API_KEY = apiKeyValue;
  } catch {} // 密钥库不可用时执行器会自动回退到环境变量或报错。
  // windowsHide：避免在 Windows 上为每个工作流子进程弹出 python.exe 控制台窗口。
  const child = spawn(runtime.python, args, {
    cwd: PROJECT_ROOT,
    env: childEnvironment,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
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

function normalizeMetaText(value) {
  return String(value || "").normalize("NFKC").toLowerCase()
    .replace(/[\(（\[【].*?[\)）\]】]/g, " ")
    .replace(/\b(feat|ft|with|prod)\.?\s+.*$/i, " ")
    .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

function metaSimilarity(left, right) {
  const a = normalizeMetaText(left); const b = normalizeMetaText(right);
  if (!a || !b) return 0;
  if (a === b) return 1;
  if (a.includes(b) || b.includes(a)) return Math.min(a.length, b.length) / Math.max(a.length, b.length) >= 0.6 ? 0.9 : 0.72;
  return 0;
}

function firstMatching(items, artist, track, fields) {
  return (items || []).find((item) => metaSimilarity(track, item[fields.title]) >= 0.8
    && metaSimilarity(artist, fields.artist(item)) >= 0.7) || null;
}

/* iTunes Search API：只返回曲名及主艺人均匹配的具体歌曲页和封面。 */
async function itunesSong(artist, track) {
  const term = `${artist} ${track}`;
  const tries = [
    `https://itunes.apple.com/search?term=${encodeURIComponent(term)}&entity=song&limit=10`,
    `https://itunes.apple.com/search?term=${encodeURIComponent(term)}&entity=song&limit=10&country=CN`,
  ];
  for (const u of tries) {
    try {
      const j = await jfetch(u);
      const hit = firstMatching(j.results, artist, track, { title: "trackName", artist: (item) => item.artistName });
      if (hit && hit.trackViewUrl) return hit;
    } catch {}
  }
  return null;
}

/* 网易云搜索：歌曲 id（兜底封面 picUrl） */
async function neteaseSong(artist, track) {
  const term = `${artist} ${track}`;
  try {
    const j = await jfetch("https://music.163.com/api/search/get", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://music.163.com",
      },
      body: new URLSearchParams({ s: term, type: "1", limit: "10", offset: "0" }).toString(),
    });
    const s = firstMatching(j.result && j.result.songs, artist, track, {
      title: "name", artist: (item) => (item.artists || item.ar || [])[0]?.name,
    });
    if (s && s.id) return s;
  } catch {}
  return null;
}

/* QQ 音乐搜索：songmid */
async function qqSong(artist, track) {
  const term = `${artist} ${track}`;
  try {
    const j = await jfetch(
      `https://c.y.qq.com/soso/fcgi-bin/client_search_cp?format=json&limit=10&w=${encodeURIComponent(term)}`,
      { headers: { Referer: "https://y.qq.com/" } }
    );
    const s = firstMatching(j.data && j.data.song && j.data.song.list, artist, track, {
      title: "songname", artist: (item) => (item.singer || [])[0]?.name,
    });
    if (s && s.songmid) return s;
  } catch {}
  return null;
}

async function resolveMeta(artist, track) {
  const key = artist + "|" + track;
  const hit = metaCache.get(key);
  if (hit && Date.now() - hit.at < (hit.data.cover ? META_TTL : NEG_TTL)) return hit.data;

  const term = `${artist} ${track}`;
  const [it, ne, qq] = await Promise.all([itunesSong(artist, track), neteaseSong(artist, track), qqSong(artist, track)]);

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

function isLocalRequest(req) {
  const address = req.socket && req.socket.remoteAddress;
  return !address || address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
}

function sendSettings(res, status = 200) {
  const effective = loadEffectiveWebConfig();
  const override = loadSettingsOverride();
  sendJson(res, status, {
    ok: true,
    settings_file: path.relative(PROJECT_ROOT, SETTINGS_PATH),
    overridden: Object.keys(override).length > 0,
    settings: editableSettings(effective),
    active_job_id: activeJobId,
    latest_job_id: latestJobId,
    applies_to: "next_job",
  });
}

/* ---------------- 静态服务 ---------------- */

restoreRecentJobs();

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

  /* 认证：普通用户与管理员使用两套互不复用的会话 Cookie。 */
  if (urlPath === "/api/auth/me" && req.method === "GET") {
    sendJson(res, 200, { ok: true, user: currentUser(req, "user"), auth_required: AUTH_REQUIRED });
    return;
  }

  if (urlPath === "/api/auth/register" && req.method === "POST") {
    try {
      const body = await readJsonBody(req);
      const user = authStore.createUser(body.username, body.password, "user");
      const session = authStore.createSession(user.id, "user");
      authStore.writeAudit(user.id, "user.register");
      setSessionCookie(res, req, USER_SESSION_COOKIE, session.token, 30 * 24 * 60 * 60);
      sendJson(res, 201, { ok: true, user: session.user, expires_at: session.expires_at });
    } catch (error) {
      sendJson(res, 400, { ok: false, error: error && error.message ? error.message : "注册失败" });
    }
    return;
  }

  if (urlPath === "/api/auth/login" && req.method === "POST") {
    try {
      const body = await readJsonBody(req);
      const user = authStore.authenticate(body.username, body.password, "user");
      const session = authStore.createSession(user.id, "user");
      authStore.writeAudit(user.id, "user.login");
      setSessionCookie(res, req, USER_SESSION_COOKIE, session.token, 30 * 24 * 60 * 60);
      sendJson(res, 200, { ok: true, user: session.user, expires_at: session.expires_at });
    } catch (error) {
      sendJson(res, 401, { ok: false, error: error && error.message ? error.message : "登录失败" });
    }
    return;
  }

  if (urlPath === "/api/auth/logout" && req.method === "POST") {
    const cookies = parseCookies(req);
    authStore.revokeSession(cookies[USER_SESSION_COOKIE], "user");
    clearSessionCookie(res, req, USER_SESSION_COOKIE);
    sendJson(res, 200, { ok: true });
    return;
  }

  if (urlPath === "/api/me/preferences" && (req.method === "GET" || req.method === "PUT")) {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    try {
      if (req.method === "GET") {
        sendJson(res, 200, { ok: true, preferences: authStore.getPreferences(user.id) });
      } else {
        const body = await readJsonBody(req);
        const current = authStore.getPreferences(user.id);
        const allowed = {};
        if (body.track_percentile_default !== undefined) {
          const percentile = Number(body.track_percentile_default);
          if (!TRACK_PERCENTILE_OPTIONS.includes(percentile)) throw new Error("默认分析分位只能是 25%、50% 或 100%");
          allowed.track_percentile_default = percentile;
        }
        if (body.display_name !== undefined) {
          if (typeof body.display_name !== "string" || body.display_name.length > 80) throw new Error("显示名称不能超过 80 个字符");
          allowed.display_name = body.display_name.trim();
        }
        sendJson(res, 200, { ok: true, preferences: authStore.savePreferences(user.id, { ...current, ...allowed }) });
      }
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "偏好设置保存失败" }); }
    return;
  }

  if (urlPath === "/api/me/playlists" && req.method === "GET") {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    sendJson(res, 200, { ok: true, playlists: authStore.listPlaylists(user.id) });
    return;
  }

  if (urlPath === "/api/me/playlists" && req.method === "POST") {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    try {
      const body = await readJsonBody(req);
      sendJson(res, 201, { ok: true, playlist: authStore.upsertPlaylist(user.id, body) });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "歌单保存失败" }); }
    return;
  }

  if (urlPath === "/api/admin/login" && req.method === "POST") {
    try {
      const body = await readJsonBody(req);
      const user = authStore.authenticate(body.username, body.password, "admin");
      const session = authStore.createSession(user.id, "admin");
      authStore.writeAudit(user.id, "admin.login");
      setSessionCookie(res, req, ADMIN_SESSION_COOKIE, session.token, 8 * 60 * 60);
      sendJson(res, 200, { ok: true, user: session.user, expires_at: session.expires_at });
    } catch (error) {
      sendJson(res, 401, { ok: false, error: error && error.message ? error.message : "管理员登录失败" });
    }
    return;
  }

  if (urlPath === "/api/admin/logout" && req.method === "POST") {
    const cookies = parseCookies(req);
    authStore.revokeSession(cookies[ADMIN_SESSION_COOKIE], "admin");
    clearSessionCookie(res, req, ADMIN_SESSION_COOKIE);
    sendJson(res, 200, { ok: true });
    return;
  }

  if (urlPath === "/api/admin/me" && req.method === "GET") {
    const user = currentUser(req, "admin");
    sendJson(res, 200, { ok: true, user: user && user.role === "admin" ? user : null });
    return;
  }

  if (urlPath === "/api/admin/users" && req.method === "GET") {
    const admin = requireAdmin(req, res); if (!admin) return;
    sendJson(res, 200, { ok: true, users: authStore.listUsers() });
    return;
  }

  const adminUserMatch = urlPath.match(/^\/api\/admin\/users\/(\d+)$/);
  if (adminUserMatch && (req.method === "PATCH" || req.method === "PUT")) {
    const admin = requireAdmin(req, res); if (!admin) return;
    try {
      const body = await readJsonBody(req);
      const targetId = Number(adminUserMatch[1]);
      if (targetId === Number(admin.id) && (body.status === "disabled" || body.role === "user")) {
        throw new Error("不能停用或撤销当前管理员账号");
      }
      const updated = authStore.updateUser(targetId, body);
      authStore.writeAudit(admin.id, "admin.user.update", { target_user_id: targetId, fields: Object.keys(body) });
      sendJson(res, 200, { ok: true, user: updated });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "用户更新失败" }); }
    return;
  }

  const adminSettings = urlPath === "/api/admin/settings" && ["GET", "PUT", "DELETE"].includes(req.method);
  if (adminSettings) {
    const admin = requireAdmin(req, res); if (!admin) return;
    try {
      if (req.method === "GET") sendSettings(res);
      else if (req.method === "DELETE") { if (fs.existsSync(SETTINGS_PATH)) fs.rmSync(SETTINGS_PATH, { force: true }); sendSettings(res); }
      else {
        const body = await readJsonBody(req);
        const patch = validateSettingsPatch(body.settings ?? body);
        const merged = deepMerge(loadSettingsOverride(), patch);
        validateSettingsPatch(merged); writeSettingsOverride(merged); sendSettings(res);
      }
      authStore.writeAudit(admin.id, `admin.settings.${req.method.toLowerCase()}`);
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "设置保存失败" }); }
    return;
  }

  const adminSecrets = urlPath === "/api/admin/secrets" && ["GET", "PUT", "DELETE"].includes(req.method);
  if (adminSecrets) {
    const admin = requireAdmin(req, res); if (!admin) return;
    try {
      if (req.method === "GET") sendJson(res, 200, { ok: true, ...await secretStatus() });
      else if (req.method === "DELETE") { await clearApiKey(); sendJson(res, 200, { ok: true, ...await secretStatus() }); }
      else {
        const body = await readJsonBody(req); const apiKey = String(body.api_key || "").trim();
        if (!apiKey || apiKey.length > 10000) throw new Error("API Key 无效");
        await saveApiKey(apiKey); sendJson(res, 200, { ok: true, ...await secretStatus() });
      }
      authStore.writeAudit(admin.id, `admin.secrets.${req.method.toLowerCase()}`);
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "密钥操作失败" }); }
    return;
  }

  if (urlPath === "/api/admin/ai/test" && req.method === "POST") {
    const admin = requireAdmin(req, res); if (!admin) return;
    try { const result = await probeAiConnection(); authStore.writeAudit(admin.id, "admin.ai.test"); sendJson(res, 200, { ok: true, ...result }); }
    catch (error) { sendJson(res, 502, { ok: false, error: error.message || "连通性测试失败" }); }
    return;
  }

  if ((urlPath === "/admin" || urlPath === "/admin/") && req.method === "GET") {
    const adminPath = path.join(ROOT, "admin.html");
    fs.stat(adminPath, (error, stat) => {
      if (error || !stat.isFile()) { res.writeHead(404).end("Not Found"); return; }
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Content-Length": stat.size, "Cache-Control": "no-cache" });
      fs.createReadStream(adminPath).pipe(res);
    });
    return;
  }

  if (urlPath === "/api/settings" && (req.method === "GET" || req.method === "PUT" || req.method === "DELETE")) {
    if (!isLocalRequest(req)) {
      sendJson(res, 403, { ok: false, error: "设置接口仅允许本机访问" });
      return;
    }
    try {
      if (req.method === "GET") {
        sendSettings(res);
      } else if (req.method === "DELETE") {
        if (fs.existsSync(SETTINGS_PATH)) fs.rmSync(SETTINGS_PATH, { force: true });
        sendSettings(res);
      } else {
        const body = await readJsonBody(req);
        const patch = validateSettingsPatch(body.settings ?? body);
        const merged = deepMerge(loadSettingsOverride(), patch);
        validateSettingsPatch(merged);
        writeSettingsOverride(merged);
        sendSettings(res);
      }
    } catch (error) {
      sendJson(res, 400, { ok: false, error: error && error.message ? error.message : "设置保存失败" });
    }
    return;
  }

  if (urlPath === "/api/secrets" && req.method === "GET") {
    if (!isLocalRequest(req)) {
      sendJson(res, 403, { ok: false, error: "密钥接口仅允许本机访问" });
      return;
    }
    try {
      sendJson(res, 200, { ok: true, ...await secretStatus() });
    } catch (error) {
      sendJson(res, 503, { ok: false, error: error && error.message ? error.message : "密钥库不可用" });
    }
    return;
  }

  if (urlPath === "/api/secrets" && req.method === "PUT") {
    if (!isLocalRequest(req)) {
      sendJson(res, 403, { ok: false, error: "密钥接口仅允许本机访问" });
      return;
    }
    try {
      const body = await readJsonBody(req);
      const apiKey = String(body.api_key || "").trim();
      if (!apiKey) throw new Error("API Key 不能为空");
      if (apiKey.length > 10000) throw new Error("API Key 过长");
      await saveApiKey(apiKey);
      sendJson(res, 200, { ok: true, ...await secretStatus() });
    } catch (error) {
      sendJson(res, 400, { ok: false, error: error && error.message ? error.message : "API Key 保存失败" });
    }
    return;
  }

  if (urlPath === "/api/secrets" && req.method === "DELETE") {
    if (!isLocalRequest(req)) {
      sendJson(res, 403, { ok: false, error: "密钥接口仅允许本机访问" });
      return;
    }
    try {
      await clearApiKey();
      sendJson(res, 200, { ok: true, ...await secretStatus() });
    } catch (error) {
      sendJson(res, 400, { ok: false, error: error && error.message ? error.message : "API Key 清除失败" });
    }
    return;
  }

  if (urlPath === "/api/ai/test" && req.method === "POST") {
    if (!isLocalRequest(req)) {
      sendJson(res, 403, { ok: false, error: "连通性测试仅允许本机访问" });
      return;
    }
    try {
      const result = await probeAiConnection();
      sendJson(res, 200, { ok: true, ...result });
    } catch (error) {
      sendJson(res, 502, { ok: false, error: error && error.message ? error.message : "连通性测试失败" });
    }
    return;
  }

  if (urlPath === "/api/config" && req.method === "GET") {
    try {
      const runtime = runtimeConfigSnapshot();
      const local = isLocalRequest(req);
      const viewer = currentUser(req, "user");
      const visibleJob = (id) => {
        const job = id ? jobs.get(id) : null;
        return !AUTH_REQUIRED || (viewer && job && Number(job.user_id) === Number(viewer.id)) ? id || null : null;
      };
      sendJson(res, 200, {
        ok: true,
        ...(local ? {
          config_file: path.relative(PROJECT_ROOT, CONFIG_PATH),
          paths: { published: path.relative(PROJECT_ROOT, DATA_PATH), jobs: path.relative(PROJECT_ROOT, JOB_ROOT), input: path.relative(PROJECT_ROOT, INPUT_ROOT) },
        } : {}),
        workflow: {
          analysis_executor_configured: runtime.analysisExecutor.configured,
          recommendation_executor_configured: runtime.recommendationExecutor.configured,
          ...(local ? {
            analysis_executor_error: runtime.analysisExecutor.error,
            recommendation_executor_error: runtime.recommendationExecutor.error,
            analysis_parallelism: runtime.analysisParallelism,
            recommendation_parallelism: runtime.recommendationParallelism,
            max_research_rounds: runtime.maxResearchRounds,
            max_candidates: runtime.maxCandidates,
            recommendation_parallelism_options: [1, 2, 3, 4, 5, 6, 7, 8],
            await_limit_timeout_seconds: runtime.awaitLimitTimeoutSeconds,
            analysis_timeout_seconds: runtime.analysisTimeoutSeconds,
            recommendation_timeout_seconds: runtime.recommendationTimeoutSeconds,
          } : {}),
          track_percentile_options: runtime.trackPercentileOptions,
          track_percentile_default: runtime.trackPercentileDefault,
          apple_requires_expected_count: false,
        },
        active_job_id: visibleJob(activeJobId),
        latest_job_id: visibleJob(latestJobId),
      });
    } catch (error) {
      sendJson(res, 500, { ok: false, error: error && error.message ? error.message : "配置读取失败" });
    }
    return;
  }

  if (urlPath === "/api/atlas/new" && req.method === "POST") {
    try {
      const user = requireUser(req, res);
      if (!user && AUTH_REQUIRED) return;
      if (activeJobId) throw new Error("已有网页工作流正在运行，请等待其完成");
      const baseJob = latestCompletedJob(user && user.id);
      if (!baseJob) throw new Error("当前没有可复用的已完成 Step 2 分析");
      const runtime = runtimeConfigSnapshot();
      if (!runtime.analysisExecutor.command || !runtime.recommendationExecutor.command) {
        sendJson(res, 503, { ok: false, error: "当前暂不可生成推荐" });
        return;
      }
      const config = { kind: "local_json", source_url: "", input: "", playlist_id: "", playlist_name: "", platform: "", expected_count: null };
      const job = await startWorkflowJob(config, { recommendationOnly: true, sourceRuntimeDir: baseJob.runtime_dir, user });
      sendJson(res, 202, { ok: true, job, regenerated_from: baseJob.id });
    } catch (error) {
      const message = error && error.message ? error.message : "无法启动新 Atlas";
      sendJson(res, message.includes("已有网页工作流") ? 409 : 400, { ok: false, error: message });
    }
    return;
  }

  if (urlPath === "/api/jobs" && req.method === "POST") {
    try {
      const user = requireUser(req, res);
      if (!user && AUTH_REQUIRED) return;
      const body = await readJsonBody(req);
      const config = allowedSource(body);
      const runtime = runtimeConfigSnapshot();
      if (!runtime.analysisExecutor.command || !runtime.recommendationExecutor.command) {
        sendJson(res, 503, { ok: false, error: "当前暂不可生成推荐" });
        return;
      }
      const job = await startWorkflowJob(config, { user });
      if (user) authStore.upsertPlaylist(user.id, { source_url: config.source_url, name: config.playlist_name, platform: config.platform, config });
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
    const user = requireUser(req, res);
    if (!canAccessJob(job, user)) { sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" }); return; }
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
      const percentile = Number(body.percentile);
      if (!TRACK_PERCENTILE_OPTIONS.includes(percentile)) {
        sendJson(res, 400, { ok: false, error: "只能选择 25%、50% 或 100% 歌单分位" });
        return;
      }
      if (maximum !== null && Math.max(1, Math.ceil(maximum * percentile)) !== limit) {
        sendJson(res, 400, { ok: false, error: `所选歌单分位对应数量应为 ${Math.max(1, Math.ceil(maximum * percentile))} 首` });
        return;
      }
      const requestPath = path.join(job.runtime_dir, LIMIT_REQUEST_FILENAME);
      const temporary = `${requestPath}.${process.pid}.tmp`;
      await fs.promises.writeFile(temporary, JSON.stringify({ limit, percentile }), "utf8");
      await fs.promises.rename(temporary, requestPath);
      sendJson(res, 202, { ok: true, job: publicJob(job), limit, percentile });
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
    const user = requireUser(req, res);
    const admin = currentUser(req, "admin");
    if (!canAccessJob(job, user, { admin: Boolean(admin) })) { sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" }); return; }
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
    const user = requireUser(req, res);
    const admin = currentUser(req, "admin");
    if (!canAccessJob(job, user, { admin: Boolean(admin) })) { sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" }); return; }
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
