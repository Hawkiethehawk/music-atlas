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
const { spawn } = require("node:child_process");
const { createAuthStore } = require("./auth_store");
const { spawnWorkflowProcess, terminateProcessTree } = require("./workflow_job");
const { validatePublishedQuality } = require("./publication_quality");
const {
  parseSource,
  canonicalizePlaylistSourceSafe,
} = require("./playlist_identity");

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
const TERMINAL_JOB_STATUSES = new Set(["completed", "failed", "cancelled", "superseded", "interrupted"]);
const DEFAULT_WORKFLOW_SETTINGS = {
  analysis_parallelism: 5,
  recommendation_parallelism: 4,
  workflow_time_budget_seconds: 120,
  analysis_timeout_seconds: 7200,
  recommendation_timeout_seconds: 3600,
  track_percentile_default: 1,
  await_limit_timeout_seconds: 1800,
  max_research_rounds: 2,
  initial_candidate_limit: 60,
  hard_candidate_limit: 200,
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
const SETTINGS_SCHEMA = [
  {
    group: "ai", title: "AI 执行器",
    note: "接口、模型与思考强度；API Key 在下方单独保存，不会写进设置 JSON。",
    fields: [
      { path: "runtime.openai_compat.base_url", label: "接口地址（Base URL）", type: "text", placeholder: "https://api.example.com/v1" },
      { path: "runtime.openai_compat.model", label: "模型", type: "text", placeholder: "deepseek-v4.1-flash" },
      { path: "thinking", label: "思考强度", type: "select", options: [["off", "关闭（最快）"], ["low", "低"], ["medium", "中"], ["high", "高"], ["max", "最大（max）"]], help: "关闭时不生成思维链；低/中/高/最大同时写入 Codex 执行器的推理级别。" },
      { path: "runtime.openai_compat.timeout_seconds", label: "请求超时（秒）", type: "number", min: 1, max: 86400 },
      { path: "runtime.openai_compat.max_tokens", label: "最大输出 Token", type: "number", min: 1, max: 200000 },
      { path: "runtime.openai_compat.temperature", label: "采样温度", type: "number", min: 0, max: 2, step: 0.1 },
    ],
  },
  {
    group: "crawler", title: "歌单读取与爬虫",
    note: "只调整请求预算与分页，不保存 Cookie、登录态或 API Key。",
    fields: [
      { path: "crawler.request_timeout_seconds", label: "接口请求超时（秒）", type: "number", min: 1, max: 86400 },
      { path: "crawler.request_retries", label: "失败重试次数", type: "number", min: 0, max: 3 },
      { path: "crawler.user_agent", label: "请求 User-Agent", type: "text" },
      { path: "crawler.netease_song_detail_batch_size", label: "网易云歌曲详情批量数", type: "number", min: 1, max: 1000 },
      { path: "crawler.qq_page_size", label: "QQ 音乐分页大小", type: "number", min: 1, max: 1000 },
      { path: "crawler.qq_max_pages", label: "QQ 音乐最大页数", type: "number", min: 1, max: 500 },
      { path: "crawler.apple_export_timeout_seconds", label: "歌单读取超时（秒）", type: "number", min: 1, max: 3600 },
      { path: "crawler.netease_detail_url", label: "网易云歌单接口地址（仅 music.163.com）", type: "text" },
      { path: "crawler.netease_song_detail_url", label: "网易云歌曲详情接口地址（仅 music.163.com）", type: "text" },
      { path: "crawler.netease_referer", label: "网易云 Referer（仅 music.163.com）", type: "text" },
      { path: "crawler.qq_musicu_url", label: "QQ 音乐接口地址（仅 u.y.qq.com）", type: "text" },
      { path: "crawler.qq_referer", label: "QQ 音乐 Referer（仅 y.qq.com）", type: "text" },
    ],
  },
  {
    group: "policy", title: "推荐策略",
    note: "安全边界覆盖；排序公式、证据门禁与最终顺序仍由程序确定。",
    fields: [
      { path: "recommendation_policy.max_per_artist", label: "同一艺人最多推荐数", type: "number", min: 1, max: 10 },
      { path: "recommendation_policy.max_per_project", label: "同一项目最多推荐数", type: "number", min: 1, max: 20 },
      { path: "recommendation_policy.min_projects", label: "最少项目数", type: "number", min: 1, max: 50 },
      { path: "recommendation_policy.candidate_pool_min", label: "候选池最低数量", type: "number", min: 1, max: 200 },
    ],
  },
  {
    group: "workflow", title: "工作流与预算",
    note: "分析与公开候选查询的并发、首次完整运行预算及歌单读取后的默认分位；摘要仍是单次 Agent 分析。",
    fields: [
      { path: "workflow.track_percentile_default", label: "默认分析分位", type: "select", options: [["0.25", "25%"], ["0.5", "50%"], ["1", "100%（全部）"]] },
      { path: "workflow.analysis_parallelism", label: "分析阶段并发", type: "number", min: 1, max: 16, help: "逐曲分析与公开资料查询的并发；摘要模式是单次 Agent 请求，不会启动多个摘要 Agent。" },
      { path: "workflow.recommendation_parallelism", label: "候选查询基础并发", type: "number", min: 1, max: 8, help: "公开候选召回至少使用 8 路并发；不代表多个推荐 Agent 并行。" },
      { path: "workflow.workflow_time_budget_seconds", label: "确认范围后处理预算（秒）", type: "number", min: 1, max: 600, help: "确认处理数量后计时，包含音乐风格分析、公开平台选曲、身份来源与配比硬校验、正式发布；首次抓取耗时另计入首次完整运行验收。不执行单列本地复核或独立审计。" },
      { path: "workflow.analysis_timeout_seconds", label: "分析阶段超时（秒）", type: "number", min: 1, max: 86400, help: "摘要 Agent 最多 78 秒；逐曲模式文案分析最多 70 秒，单次请求最多 30 秒并可在剩余预算内重试。首次抓取和其他步骤会收紧上限，并为选曲与正式发布预留 30 秒。此项可调低，不能放宽程序上限。" },
      { path: "workflow.recommendation_timeout_seconds", label: "推荐阶段超时（秒）", type: "number", min: 1, max: 86400 },
      { path: "workflow.await_limit_timeout_seconds", label: "等待选择超时（秒）", type: "number", min: 1, max: 86400 },
      { path: "workflow.max_research_rounds", label: "最大研究轮数", type: "number", min: 1, max: 3 },
      { path: "workflow.initial_candidate_limit", label: "首轮候选上限", type: "number", min: 1, max: 1200 },
      { path: "workflow.hard_candidate_limit", label: "候选绝对上限", type: "number", min: 1, max: 1200 },
    ],
  },
  {
    group: "editorial", title: "页面展示",
    note: "标题与导语会写入下一次任务生成的 Atlas；不改变推荐事实与证据。",
    fields: [
      { path: "editorial.title", label: "页面标题", type: "text", maxlength: 120 },
      { path: "editorial.lede", label: "页面导语", type: "textarea", maxlength: 2000 },
    ],
  },
];

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
    if (result.workflow.max_candidates !== undefined) {
      const legacyLimit = result.workflow.max_candidates;
      if (result.workflow.initial_candidate_limit === undefined) result.workflow.initial_candidate_limit = legacyLimit;
      if (result.workflow.hard_candidate_limit === undefined) result.workflow.hard_candidate_limit = legacyLimit;
    }
    for (const key of [
      "track_limit_options", "track_limit_default", "max_candidates", "candidate_target",
      "analysis_batch_size", "analysis_context_budget", "context_budget",
    ]) {
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
    if (!isObject(value.runtime)) throw new Error("runtime 必须是对象");
    if (Object.keys(value.runtime).some((key) => key !== "openai_compat" && key !== "codex_reasoning_effort")) throw new Error("runtime 只允许修改 openai_compat 与 codex_reasoning_effort");
    output.runtime = {};
    if (value.runtime.codex_reasoning_effort !== undefined) {
      const effort = String(value.runtime.codex_reasoning_effort || "").trim().toLowerCase();
      if (!["", "low", "medium", "high", "max"].includes(effort)) throw new Error("runtime.codex_reasoning_effort 只支持 low / medium / high / max");
      output.runtime.codex_reasoning_effort = effort;
    }
    const settings = isObject(value.runtime.openai_compat) ? value.runtime.openai_compat : {};
    const allowed = new Set(Object.keys(DEFAULT_OPENAI_SETTINGS));
    for (const key of Object.keys(settings)) if (!allowed.has(key)) throw new Error(`runtime.openai_compat 不允许字段：${key}`);
    output.runtime.openai_compat = {};
    if (settings.base_url !== undefined) {
      let parsed;
      try { parsed = new URL(String(settings.base_url || "").trim()); }
      catch { throw new Error("runtime.openai_compat.base_url 必须是有效 HTTPS 地址"); }
      const loopbackHttp = parsed.protocol === "http:" && ["127.0.0.1", "::1", "localhost"].includes(parsed.hostname);
      if (parsed.protocol !== "https:" && !loopbackHttp) throw new Error("runtime.openai_compat.base_url 必须使用 HTTPS（本机回环地址除外）");
      output.runtime.openai_compat.base_url = parsed.toString().replace(/\/$/, "");
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
      workflow_time_budget_seconds: [1, 600],
      analysis_timeout_seconds: [1, 86400], recommendation_timeout_seconds: [1, 86400],
      await_limit_timeout_seconds: [1, 86400],
      max_research_rounds: [1, 3], initial_candidate_limit: [1, 1200], hard_candidate_limit: [1, 1200],
    };
    for (const [key, range] of Object.entries(ints)) if (workflow[key] !== undefined) output.workflow[key] = settingInteger(workflow[key], `workflow.${key}`, range[0], range[1]);
    if (output.workflow.initial_candidate_limit !== undefined && output.workflow.hard_candidate_limit !== undefined
        && output.workflow.initial_candidate_limit > output.workflow.hard_candidate_limit) {
      throw new Error("workflow.initial_candidate_limit 不得超过 workflow.hard_candidate_limit");
    }
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
  const workflowSettings = { ...DEFAULT_WORKFLOW_SETTINGS };
  for (const key of Object.keys(DEFAULT_WORKFLOW_SETTINGS)) {
    if (Object.hasOwn(workflow, key)) workflowSettings[key] = workflow[key];
  }
  return {
    crawler: { ...DEFAULT_CRAWLER_SETTINGS, ...(isObject(config.crawler) ? config.crawler : {}) },
    runtime: { openai_compat: { ...DEFAULT_OPENAI_SETTINGS, ...openai }, codex_reasoning_effort: String(runtime.codex_reasoning_effort || "").trim().toLowerCase() },
    workflow: workflowSettings,
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
  const workflowTimeBudgetSeconds = settingInteger(Number(workflow.workflow_time_budget_seconds ?? DEFAULT_WORKFLOW_SETTINGS.workflow_time_budget_seconds), "workflow.workflow_time_budget_seconds", 1, 600);
  const analysisTimeoutSeconds = settingInteger(Number(workflow.analysis_timeout_seconds ?? 600), "workflow.analysis_timeout_seconds", 1, 86400);
  const recommendationTimeoutSeconds = settingInteger(Number(workflow.recommendation_timeout_seconds ?? 600), "workflow.recommendation_timeout_seconds", 1, 86400);
  const trackPercentileDefault = Number(workflow.track_percentile_default ?? DEFAULT_WORKFLOW_SETTINGS.track_percentile_default);
  if (!TRACK_PERCENTILE_OPTIONS.includes(trackPercentileDefault)) throw new Error("workflow.track_percentile_default 必须是 0.25、0.5 或 1");
  const awaitLimitTimeoutSeconds = settingInteger(Number(workflow.await_limit_timeout_seconds ?? 1800), "workflow.await_limit_timeout_seconds", 1, 86400);
  const maxResearchRounds = settingInteger(Number(workflow.max_research_rounds ?? 2), "workflow.max_research_rounds", 1, 3);
  const initialCandidateLimit = settingInteger(Number(workflow.initial_candidate_limit ?? workflow.max_candidates ?? DEFAULT_WORKFLOW_SETTINGS.initial_candidate_limit), "workflow.initial_candidate_limit", 1, 1200);
  const hardCandidateLimit = settingInteger(Number(workflow.hard_candidate_limit ?? DEFAULT_WORKFLOW_SETTINGS.hard_candidate_limit), "workflow.hard_candidate_limit", 1, 1200);
  if (initialCandidateLimit > hardCandidateLimit) throw new Error("workflow.initial_candidate_limit 不得超过 workflow.hard_candidate_limit");
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
    workflowTimeBudgetSeconds,
    analysisTimeoutSeconds,
    recommendationTimeoutSeconds,
    trackPercentileOptions: [...TRACK_PERCENTILE_OPTIONS],
    trackPercentileDefault,
    awaitLimitTimeoutSeconds,
    maxResearchRounds,
    initialCandidateLimit,
    hardCandidateLimit,
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
const USER_DATA_ROOT = path.join(path.dirname(DATA_PATH), "users");
const JOB_ROOT = resolveProjectPath(PATH_CONFIG.jobs || "runtime/web-jobs", "paths.jobs");
const HISTORY_ROOT = resolveProjectPath(PATH_CONFIG.recommendation_history || "runtime/recommendation-history", "paths.recommendation_history");
const ACCOUNT_RESET_ROOT = path.join(path.dirname(JOB_ROOT), "backups", "account-reset");
const INPUT_ROOT = resolveProjectPath(PATH_CONFIG.input || "input", "paths.input");
const INITIAL_RUNTIME = runtimeConfigSnapshot();
const PYTHON = INITIAL_RUNTIME.python;
// 与 web_workflow.py 的 LIMIT_REQUEST_FILENAME 保持一致：网页写请求，工作流读请求。
const LIMIT_REQUEST_FILENAME = "requested_track_limit.json";
const JOB_STATE_FILENAME = "web_job_state.json";
const COMPLETED_JOB_RETENTION_DAYS = Math.max(1, Number(process.env.ATLAS_COMPLETED_JOB_RETENTION_DAYS || 14));
const OTHER_JOB_RETENTION_DAYS = Math.max(1, Number(process.env.ATLAS_OTHER_JOB_RETENTION_DAYS || 7));
const jobs = new Map();
const jobSubscribers = new Map();
// 多用户并行：按任务记录归属用户。网页面板仍一次只跑一个任务，
// 这里的额度用于自动化/多位使用者同时提交的场景。
const MAX_CONCURRENT_JOBS = Math.max(1, Number(process.env.ATLAS_MAX_JOBS || 10));
const MAX_JOBS_PER_USER = Math.max(1, Number(process.env.ATLAS_MAX_JOBS_PER_USER || 3));
const activeJobs = new Map();   // jobId -> { userId, startedAt }
let latestJobId = null;         // 全局最近任务（无人登录的部署仍可查看）

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
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".md": "text/markdown; charset=utf-8",
};

/* ---------------- 封面 / 歌曲链接解析 ---------------- */

const META_TTL = 60 * 60 * 1000; // 成功缓存 1h
const NEG_TTL = 10 * 60 * 1000;  // 失败负缓存 10min（避免反复打外网）
const META_CACHE_MAX = 500;
const META_QUERY_MAX_LENGTH = 200;
const META_RATE_WINDOW = 60 * 1000;
const META_RATE_LIMIT = 60;
const metaCache = new Map();     // LRU: key → { at, data }
const metaInflight = new Map();
const metaRateBuckets = new Map();
const atlasPayloadCaches = new Map();

function atlasDataPathForUser(userId) {
  if (!AUTH_REQUIRED) return DATA_PATH;
  const numericId = Number(userId);
  if (!Number.isSafeInteger(numericId) || numericId < 1) return null;
  return path.join(USER_DATA_ROOT, String(numericId), path.basename(DATA_PATH));
}

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

async function loadAtlasPayload(dataPath = DATA_PATH) {
  const stat = await fs.promises.stat(dataPath);
  const cached = atlasPayloadCaches.get(dataPath);
  if (cached && cached.payload && cached.mtimeMs === stat.mtimeMs) {
    return cached.payload;
  }
  const raw = await fs.promises.readFile(dataPath, "utf8");
  const payload = JSON.parse(raw);
  if (!payload || payload.payload_type !== "music_atlas_web") {
    throw new Error("invalid web payload");
  }
  atlasPayloadCaches.set(dataPath, {
    mtimeMs: stat.mtimeMs,
    payload,
    response: JSON.stringify({ ok: true, ...payload }),
  });
  return payload;
}

async function loadAtlasResponse(dataPath = DATA_PATH) {
  await loadAtlasPayload(dataPath);
  return atlasPayloadCaches.get(dataPath).response;
}

function isLoopbackAddress(value) {
  return value === "127.0.0.1" || value === "::1" || value === "::ffff:127.0.0.1";
}

function forwardedAddresses(req) {
  return String(req.headers["x-forwarded-for"] || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
}

function clientAddress(req) {
  const remoteAddress = String(req.socket?.remoteAddress || "unknown");
  const isLocalProxy = isLoopbackAddress(remoteAddress);
  if (!isLocalProxy) return remoteAddress;
  const forwarded = forwardedAddresses(req);
  return forwarded.at(-1) || remoteAddress;
}

function allowMetaRequest(req) {
  const now = Date.now();
  const key = clientAddress(req);
  let bucket = metaRateBuckets.get(key);
  if (!bucket || now - bucket.startedAt >= META_RATE_WINDOW) {
    bucket = { startedAt: now, count: 0 };
  }
  bucket.count += 1;
  metaRateBuckets.delete(key);
  metaRateBuckets.set(key, bucket);
  while (metaRateBuckets.size > META_CACHE_MAX) {
    metaRateBuckets.delete(metaRateBuckets.keys().next().value);
  }
  return bucket.count <= META_RATE_LIMIT;
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
    exit_code: job.exit_code === undefined ? null : job.exit_code,
    workflow_mode: workflowMode,
    first_run: Boolean(job.first_run),
    workflow_time_budget_seconds: job.workflow_time_budget_seconds ?? null,
    processing_started_at: job.processing_started_at || null,
    snapshot_elapsed_seconds: job.snapshot_elapsed_seconds ?? null,
    processing_elapsed_seconds: job.processing_elapsed_seconds ?? null,
    first_run_elapsed_seconds: job.first_run_elapsed_seconds ?? null,
    first_run_within_budget: job.first_run_within_budget ?? null,
  };
}

// A missing final event is not proof of a published Atlas, even when Python exits 0.
function lockedTrackSignature(groups, field) {
  if (!Array.isArray(groups) || groups.length !== 3) return null;
  const seen = new Set();
  const ordered = [];
  for (const group of groups) {
    if (!Array.isArray(group.recommendations) || group.recommendations.length !== 10) return null;
    const ids = [];
    for (const track of group.recommendations) {
      const id = String(track && track[field] || "").trim();
      if (!id || seen.has(id)) return null;
      seen.add(id);
      ids.push(id);
    }
    ordered.push(ids);
  }
  return crypto.createHash("sha256").update(JSON.stringify(ordered)).digest("hex");
}

function completedWorkflowArtifacts(job, publishedDataPath) {
  try {
    const payloadPath = path.join(job.runtime_dir, "web_payload.json");
    const report = JSON.parse(fs.readFileSync(path.join(job.runtime_dir, "web_job_report.json"), "utf8"));
    const selection = JSON.parse(fs.readFileSync(path.join(job.runtime_dir, "web_selection.json"), "utf8"));
    const payloadBytes = fs.readFileSync(payloadPath);
    const publishedBytes = fs.readFileSync(publishedDataPath);
    const payload = JSON.parse(payloadBytes.toString("utf8"));
    const lockedSignature = lockedTrackSignature(selection.atlas_groups, "canonical_track_id");
    if (report.status !== "completed" || selection.status !== "tracks_locked"
      || !report.payload_path || path.resolve(report.payload_path) !== path.resolve(payloadPath)
      || !report.current_data_path || path.resolve(report.current_data_path) !== path.resolve(publishedDataPath)
      || !payloadBytes.equals(publishedBytes) || payload.payload_type !== "music_atlas_web"
      || payload.status?.run !== "completed" || payload.status?.publication !== "published"
      || payload.status?.evidence_audit !== "not_performed"
      || payload.status?.review !== "not_performed"
      || payload.audit?.status !== "not_performed" || payload.review?.status !== "not_performed"
      || report.review !== undefined
      || payload.atlas_group_count !== 3
      || report.recommendation_groups?.count !== 3
      || report.recommendation_groups?.total_unique_recommendation_count !== 30
      || !lockedSignature || lockedSignature !== lockedTrackSignature(payload.atlas_groups, "id")
      || !selection.snapshot_id || report.snapshot_id !== selection.snapshot_id
      || payload.source?.snapshotId !== selection.snapshot_id
      || !report.analysis_id || payload.analysis?.analysisId !== report.analysis_id) return false;
    const packet = JSON.parse(fs.readFileSync(path.join(job.runtime_dir, "musician_analysis.json"), "utf8"));
    if (!validatePublishedQuality(packet, payload)
      || packet.analysis_id !== report.analysis_id
      || report.source_track_count !== packet.source_playlist_track_count
      || report.processed_track_count !== packet.source_track_count) {
      job.publication_error = "公开来源风格资料未达到当前分析质量门槛，不得发布为 completed";
      return false;
    }
    const firstGroupIds = selection.atlas_groups[0].recommendations.map((track) => track.canonical_track_id);
    return Array.isArray(payload.recommendations)
      && JSON.stringify(payload.recommendations.map((track) => track && track.id)) === JSON.stringify(firstGroupIds);
  } catch {
    return false;
  }
}

function syncRunFromEvent(job, event) {
  try {
    if (!authStore.getRun(job.id)) {
      authStore.createRun({
        jobId: job.id, userId: job.user_id || null,
        kind: job.workflow_mode === "recommendation_only" ? "recommendation_only" : "full",
        runtimeDir: job.runtime_dir || "",
      });
    }
    const patch = {};
    if (Number.isFinite(Number(event.track_count)) && (event.stage === "snapshot" || event.task_kind === "track_limit")) patch.trackCount = Number(event.track_count);
    if (Number.isFinite(Number(event.source_track_count)) && event.event === "completed" && event.stage === "analysis") patch.analyzedCount = Number(event.source_track_count);
    if (Number.isFinite(Number(event.recommendation_count)) && event.event === "completed") patch.recommendationCount = Number(event.recommendation_count);
    if (TERMINAL_JOB_STATUSES.has(event.status)) {
      patch.status = event.status;
      if (event.status !== "completed") patch.error = String(event.error || event.message || "").slice(0, 500);
    }
    if (Object.keys(patch).length) authStore.updateRun(job.id, patch);
    if (job.user_id && event.stage === "snapshot" && event.event === "completed" && event.source) {
      const source = event.source;
      const playlistId = String(source.playlist_id || "");
      const platform = source.kind === "netease_public" ? "netease"
        : source.kind === "qq_public" ? "qq_music"
          : source.kind === "apple_music" ? "apple_music" : job.platform;
      const canonicalUrl = String(source.resolved_url || source.url || job.source_url || "");
      authStore.upsertPlaylist(job.user_id, {
        source_url: canonicalUrl,
        canonical_url: canonicalUrl,
        canonical_key: playlistId ? `${platform}:${playlistId}` : "",
        name: event.playlist_name || "",
        platform,
      });
    }
  } catch (error) {
    console.error(`运行记录同步失败 ${job.id}：${error.message}`);
  }
}

function summarizeRunFromJob(job) {
  const events = Array.isArray(job.events) ? job.events : [];
  let trackCount = null; let analyzedCount = null; let recommendationCount = null; let error = "";
  let platform = ""; let playlistId = ""; let playlistName = "";
  for (const event of events) {
    if (Number.isFinite(Number(event.track_count))) trackCount = Number(event.track_count);
    if (Number.isFinite(Number(event.source_track_count)) && event.stage === "analysis") analyzedCount = Number(event.source_track_count);
    if (Number.isFinite(Number(event.recommendation_count))) recommendationCount = Number(event.recommendation_count);
    if (TERMINAL_JOB_STATUSES.has(event.status) && event.status !== "completed") {
      error = String(event.error || event.message || "").slice(0, 500);
    }
    if (event.stage === "snapshot" && event.event === "completed") {
      if (event.playlist_name) playlistName = String(event.playlist_name);
      if (event.snapshot_id) playlistId = playlistId || String(event.snapshot_id);
      if (event.source && typeof event.source === "object" && event.source.kind) platform = String(event.source.kind);
    }
  }
  return { trackCount, analyzedCount, recommendationCount, error, platform, playlistId, playlistName };
}

function persistJobState(job) {
  try {
    fs.mkdirSync(job.runtime_dir, { recursive: true });
    fs.writeFileSync(path.join(job.runtime_dir, JOB_STATE_FILENAME), JSON.stringify({ ...publicJob(job), stderr_tail: job.stderr_tail || "", user_id: job.user_id || null }, null, 2) + "\n", "utf8");
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

function readRecommendationHistoryFiles() {
  if (!fs.existsSync(HISTORY_ROOT)) return [];
  const items = [];
  for (const entry of fs.readdirSync(HISTORY_ROOT, { withFileTypes: true })) {
    if (!entry.isFile() || path.extname(entry.name).toLowerCase() !== ".json") continue;
    const filePath = path.join(HISTORY_ROOT, entry.name);
    try {
      const payload = JSON.parse(fs.readFileSync(filePath, "utf8"));
      const entries = Array.isArray(payload.entries) ? payload.entries : [];
      const retentionDays = Number(payload.retention_days) || 7;
      const cutoff = Date.now() - retentionDays * 24 * 60 * 60 * 1000;
      const active = entries.filter((item) => {
        const stamp = Date.parse(item && item.generated_at ? item.generated_at : "");
        return Number.isFinite(stamp) && stamp >= cutoff;
      });
      const ids = new Set();
      const keys = new Set();
      for (const item of active) {
        for (const value of item.canonical_track_ids || []) ids.add(String(value));
        for (const value of item.track_keys || []) keys.add(String(value));
      }
      const stamps = entries.map((item) => String((item && item.generated_at) || "")).filter(Boolean).sort();
      items.push({
        file: entry.name,
        user_id: String(payload.user_id || ""),
        platform: historyPlatform(payload.playlist && payload.playlist.kind),
        playlist: payload.playlist && typeof payload.playlist === "object" ? payload.playlist : {},
        retention_days: retentionDays,
        total_entries: entries.length,
        active_entries: active.length,
        excluded_tracks: ids.size || keys.size,
        latest_at: stamps.length ? stamps[stamps.length - 1] : "",
        entries: entries.map((item) => ({
          generated_at: String((item && item.generated_at) || ""),
          tracks: Math.max(((item && item.canonical_track_ids) || []).length, ((item && item.track_keys) || []).length),
        })),
      });
    } catch (error) {
      items.push({ file: entry.name, user_id: "", platform: "unknown", broken: true, error: error.message });
    }
  }
  items.sort((left, right) => String(right.latest_at || "").localeCompare(String(left.latest_at || "")));
  return items;
}

function historyPlatform(kind) {
  const value = String(kind || "").trim().toLowerCase();
  if (["netease", "netease_public"].includes(value)) return "netease";
  if (["qq", "qq_public"].includes(value)) return "qq";
  if (["apple_music", "apple"].includes(value)) return "apple_music";
  return value || "unknown";
}

function historyMatchesScope(item, scope, value) {
  if (scope === "all") return true;
  if (scope === "account") return (item.user_id || "__unassigned__") === value;
  return item.platform === value;
}

function resolveHistoryFile(rawName) {
  const name = path.basename(decodeURIComponent(String(rawName || "")));
  if (path.extname(name).toLowerCase() !== ".json") return null;
  const target = path.join(HISTORY_ROOT, name);
  if (!target.startsWith(HISTORY_ROOT + path.sep)) return null;
  return { name, target };
}

function userResetPlan(userId) {
  const snapshot = authStore.userResetSnapshot(userId);
  const id = Number(snapshot.user.id);
  const jobIds = new Set(snapshot.runs.map((run) => String(run.job_id)));
  if (fs.existsSync(JOB_ROOT)) {
    for (const entry of fs.readdirSync(JOB_ROOT, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const statePath = path.join(JOB_ROOT, entry.name, JOB_STATE_FILENAME);
      try {
        const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
        if (Number(state.user_id) === id) jobIds.add(entry.name);
      } catch {}
    }
  }
  const targets = [];
  const addTarget = (root, name, category) => {
    if (path.basename(name) !== name) throw new Error("任务或缓存文件名无效，初始化已停止");
    const source = path.join(root, name);
    if (!fs.existsSync(source)) return;
    const stat = fs.lstatSync(source);
    if (stat.isSymbolicLink()) throw new Error("发现符号链接，初始化已停止");
    targets.push({ source, category, name, size: stat.size, mtime_ms: stat.mtimeMs });
  };
  addTarget(USER_DATA_ROOT, String(id), "atlas");
  for (const item of readRecommendationHistoryFiles()) {
    if (String(item.user_id || "") === String(id)) addTarget(HISTORY_ROOT, item.file, "history");
  }
  for (const jobId of jobIds) {
    if (!/^[A-Za-z0-9_-]+$/.test(jobId)) throw new Error("运行记录包含不安全的任务 ID，初始化已停止");
    const statePath = path.join(JOB_ROOT, jobId, JOB_STATE_FILENAME);
    if (fs.existsSync(statePath)) {
      const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
      if (state.user_id != null && Number(state.user_id) !== id) throw new Error("任务归属不一致，初始化已停止");
    }
    addTarget(JOB_ROOT, jobId, "jobs");
    for (const suffix of ["web_config.snapshot.json", "recommendation_policy.snapshot.json", "editorial.snapshot.json"]) {
      addTarget(JOB_ROOT, `${jobId}.${suffix}`, "jobs");
    }
  }
  targets.sort((left, right) => `${left.category}/${left.name}`.localeCompare(`${right.category}/${right.name}`));
  const summary = {
    playlists: snapshot.playlists.length,
    runs: snapshot.runs.length,
    preferences: snapshot.preferences.length,
    sessions: snapshot.session_count,
    job_directories: targets.filter((item) => item.category === "jobs" && !item.name.includes(".snapshot.json")).length,
    history_files: targets.filter((item) => item.category === "history").length,
    atlas_present: targets.some((item) => item.category === "atlas"),
    active_jobs: activeJobCountForUser(id),
  };
  const token = crypto.createHash("sha256").update(JSON.stringify({ snapshot, targets, summary })).digest("hex");
  return { user: snapshot.user, snapshot, targets, summary, token };
}

function initializeUserAccount(userId, actorId, plan) {
  const id = Number(userId);
  const archiveId = `${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-user-${id}-${crypto.randomBytes(4).toString("hex")}`;
  const archiveDir = path.join(ACCOUNT_RESET_ROOT, archiveId);
  fs.mkdirSync(ACCOUNT_RESET_ROOT, { recursive: true, mode: 0o700 });
  fs.mkdirSync(archiveDir, { mode: 0o700 });
  const moved = [];
  let databaseCommitted = false;
  try {
    fs.writeFileSync(path.join(archiveDir, "manifest.json"), JSON.stringify({
      schema_version: "1.0", archived_at: new Date().toISOString(),
      user: plan.user, summary: plan.summary,
      database: { preferences: plan.snapshot.preferences, playlists: plan.snapshot.playlists, runs: plan.snapshot.runs },
      files: plan.targets.map(({ category, name }) => ({ category, name })),
    }, null, 2) + "\n", { mode: 0o600 });
    for (const target of plan.targets) {
      const destination = path.join(archiveDir, target.category, target.name);
      fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
      fs.renameSync(target.source, destination);
      moved.push({ source: target.source, destination });
    }
    const user = authStore.resetUserData(id, actorId, archiveId);
    databaseCommitted = true;
    for (const [jobId, job] of jobs) {
      if (Number(job.user_id) !== id) continue;
      for (const subscriber of jobSubscribers.get(jobId) || []) {
        try { subscriber.end(); } catch {}
      }
      jobSubscribers.delete(jobId);
      jobs.delete(jobId);
    }
    latestJobId = Array.from(jobs.values()).sort((a, b) => Date.parse(b.updated_at || "") - Date.parse(a.updated_at || ""))[0]?.id || null;
    atlasPayloadCaches.delete(atlasDataPathForUser(id));
    return { user, archive_id: archiveId, summary: plan.summary };
  } catch (error) {
    if (databaseCommitted) throw error;
    const rollbackErrors = [];
    for (const item of moved.reverse()) {
      try { fs.renameSync(item.destination, item.source); }
      catch (rollbackError) { rollbackErrors.push(rollbackError.message); }
    }
    if (rollbackErrors.length) throw new Error(`初始化失败，文件回滚未完成；恢复档案 ${archiveId}：${rollbackErrors.join("；")}`);
    throw error;
  }
}

function pruneHistoryEntries(payload) {
  const entries = Array.isArray(payload.entries) ? payload.entries : [];
  const retentionDays = Number(payload.retention_days) || 7;
  const cutoff = Date.now() - retentionDays * 24 * 60 * 60 * 1000;
  const kept = entries.filter((item) => {
    const stamp = Date.parse(item && item.generated_at ? item.generated_at : "");
    return Number.isFinite(stamp) && stamp >= cutoff;
  });
  return { payload: { ...payload, entries: kept }, removed: entries.length - kept.length, kept: kept.length };
}

function listUserRuns(userId, limit = 50) {
  if (!fs.existsSync(JOB_ROOT)) return [];
  const runs = [];
  for (const entry of fs.readdirSync(JOB_ROOT, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const statePath = path.join(JOB_ROOT, entry.name, JOB_STATE_FILENAME);
    try {
      if (!fs.existsSync(statePath)) continue;
      const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
      if (!state || Number(state.user_id) !== Number(userId)) continue;
      runs.push({
        id: state.id || entry.name,
        status: state.status || "unknown",
        stage: state.stage || "",
        created_at: state.created_at || "",
        updated_at: state.updated_at || "",
        exit_code: state.exit_code === undefined ? null : state.exit_code,
        workflow_mode: state.workflow_mode || "full",
        event_count: Array.isArray(state.events) ? state.events.length : 0,
      });
    } catch {}
  }
  runs.sort((left, right) => Date.parse(right.updated_at || "") - Date.parse(left.updated_at || ""));
  return runs.slice(0, Math.min(Math.max(Number(limit) || 50, 1), 200));
}

function withCompletedRunGroupSummary(run) {
  if (run.status !== "completed" || !/^\d{14}-[a-f0-9]{8}$/.test(run.job_id)) return run;
  try {
    const report = JSON.parse(fs.readFileSync(path.join(JOB_ROOT, run.job_id, "web_job_report.json"), "utf8"));
    const groupCount = report.recommendation_groups?.count;
    const totalCount = report.recommendation_groups?.total_unique_recommendation_count;
    if (report.status !== "completed" || !Number.isInteger(groupCount) || groupCount < 1
      || !Number.isInteger(totalCount) || totalCount !== groupCount * run.recommendation_count
      || report.web_export?.recommendation_count !== run.recommendation_count) return run;
    return { ...run, recommendation_group_count: groupCount, total_unique_recommendation_count: totalCount };
  } catch {
    return run;
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
        if (state && state.id === entry.name && Array.isArray(state.events)) {
          const terminal = TERMINAL_JOB_STATUSES.has(state.status);
          const events = [...state.events];
          let status = state.status;
          let updatedAt = state.updated_at;
          if (status === "failed") {
            const last = [...events].reverse().find((event) => event.status === "failed");
            const message = String(last?.error || last?.message || "");
            if (/任务已取消/.test(message)) status = "cancelled";
            else if (/已改用.*已有分析/.test(message)) status = "superseded";
            if (status !== "failed" && last) {
              const index = events.indexOf(last);
              events[index] = { ...last, status, event: status, legacy_status: "failed" };
            }
          }
          if (!terminal) {
            // 服务重启会结束子进程：将未完成任务记为 interrupted，并同步落盘与数据库。
            const at = new Date().toISOString();
            events.push({ event: "interrupted", status: "interrupted", stage: state.stage || "workflow",
              error: "服务重启，任务已中断；请重新运行", at,
              seq: Math.max(0, ...events.map((item) => Number(item.seq) || 0)) + 1 });
            status = "interrupted";
            updatedAt = at;
          }
          job = { ...state, status, updated_at: updatedAt, events, runtime_dir: runtimeDir, user_id: state.user_id || null,
                  event_seq: Math.max(0, ...events.map((event) => Number(event.seq) || 0)) };
        }
      }
    } catch {}
    job = job || restoredLegacyJob(entry.name, runtimeDir);
    if (job) {
      persistJobState(job);
      restored.push(job);
    }
  }
  restored.sort((left, right) => Date.parse(right.updated_at || "") - Date.parse(left.updated_at || ""));
  for (const job of restored.slice(0, 20)) jobs.set(job.id, job);
  if (restored.length) latestJobId = restored[0].id;
  // 回填运行历史（幂等）：把磁盘上的历史任务补进 runs 表，供账号中心与后台查询。
  for (const job of restored.slice(0, 500)) {
    try {
      const summary = summarizeRunFromJob(job);
      authStore.backfillRun({
        jobId: job.id,
        userId: job.user_id || null,
        kind: job.workflow_mode === "recommendation_only" ? "recommendation_only" : "full",
        platform: summary.platform,
        playlistId: summary.playlistId,
        playlistName: summary.playlistName,
        trackCount: summary.trackCount,
        analyzedCount: summary.analyzedCount,
        recommendationCount: summary.recommendationCount,
        startedAt: job.created_at,
        finishedAt: job.updated_at,
        status: TERMINAL_JOB_STATUSES.has(job.status) ? job.status : "interrupted",
        runtimeDir: job.runtime_dir || "",
        error: summary.error,
      });
    } catch (error) {
      console.error(`运行历史回填失败 ${job.id}：${error.message}`);
    }
  }
}

function recordJobEvent(job, event) {
  if (TERMINAL_JOB_STATUSES.has(job.status)) return;
  const safeEvent = event && typeof event === "object" ? { ...event } : { event: "message", message: String(event) };
  safeEvent.at = safeEvent.at || new Date().toISOString();
  safeEvent.seq = ++job.event_seq;
  job.events.push(safeEvent);
  if (job.events.length > 1000) job.events.shift();
  if (typeof safeEvent.stage === "string") job.stage = safeEvent.stage;
  if (safeEvent.event === "awaiting_limit" && job.snapshot_elapsed_seconds == null) {
    job.snapshot_elapsed_seconds = Math.max(0, (Date.now() - Date.parse(job.created_at)) / 1000);
  }
  if (TERMINAL_JOB_STATUSES.has(safeEvent.status)) {
    const finishedAt = Date.now();
    const processingStarted = Date.parse(job.processing_started_at || "");
    if (Number.isFinite(processingStarted)) {
      job.processing_elapsed_seconds = Math.max(0, (finishedAt - processingStarted) / 1000);
    }
    if (job.first_run && Number.isFinite(processingStarted)) {
      job.first_run_elapsed_seconds = Number((Number(job.snapshot_elapsed_seconds || 0) + job.processing_elapsed_seconds).toFixed(3));
      job.first_run_within_budget = safeEvent.status === "completed"
        && job.first_run_elapsed_seconds <= job.workflow_time_budget_seconds;
    }
  }
  if (TERMINAL_JOB_STATUSES.has(safeEvent.status)) job.status = safeEvent.status;
  else if (safeEvent.status === "running") job.status = "running";
  else if (safeEvent.status === "awaiting_limit") job.status = "awaiting_limit";
  job.updated_at = safeEvent.at;
  latestJobId = job.id;
  persistJobState(job);
  syncRunFromEvent(job, safeEvent);
  const subscribers = jobSubscribers.get(job.id) || new Set();
  for (const res of subscribers) {
    try {
      res.write(`data: ${JSON.stringify({ ok: true, job: publicJob(job), event: safeEvent })}\n\n`);
    } catch {}
  }
}

function activeJobCountForUser(userId) {
  let count = 0;
  for (const meta of activeJobs.values()) {
    if (Number(meta.userId) === Number(userId)) count += 1;
  }
  return count;
}

function activeJobIdForUser(userId) {
  for (const [jobId, meta] of activeJobs) {
    if (Number(meta.userId) === Number(userId)) return jobId;
  }
  return null;
}

function latestJobIdForUser(userId) {
  return Array.from(jobs.values())
    .filter((job) => Number(job.user_id) === Number(userId))
    .sort((left, right) => Date.parse(right.updated_at || "") - Date.parse(left.updated_at || ""))[0]?.id || null;
}

// 当前请求者可见的任务：优先自己正在跑的，其次自己最近一次任务。
function visibleJobIdForUser(userId) {
  if (userId == null) return activeJobs.keys().next().value || latestJobId;
  return activeJobIdForUser(userId) || latestJobIdForUser(userId);
}

function latestCompletedJob(userId = null) {
  return Array.from(jobs.values())
    .filter((job) => job && job.status === "completed" && (userId == null ? !AUTH_REQUIRED : Number(job.user_id) === Number(userId)))
    .sort((left, right) => Date.parse(right.updated_at || "") - Date.parse(left.updated_at || ""))[0] || null;
}

// 分析结果只在 24 小时内可直接复用；超期必须重新分析。
const ANALYSIS_REUSE_MAX_AGE_MS = 24 * 60 * 60 * 1000;

function analysisAgeMs(job) {
  const finishedAt = Date.parse(job && job.updated_at || "");
  return Number.isFinite(finishedAt) ? Date.now() - finishedAt : Number.POSITIVE_INFINITY;
}

// 只有带完整分析产物的任务可以作为复用源；复用任务自身借用 source_runtime，不能再次被复用。
function analysisSourceJobs(userId) {
  return Array.from(jobs.values())
    .filter((job) => job && job.status === "completed"
      && (userId == null ? !AUTH_REQUIRED : Number(job.user_id) === Number(userId))
      && job.workflow_mode !== "recommendation_only"
      && analysisAgeMs(job) <= ANALYSIS_REUSE_MAX_AGE_MS)
    .sort((left, right) => Date.parse(right.updated_at || "") - Date.parse(left.updated_at || ""));
}

// 复用需要同链接与同范围双匹配：平台 + 歌单 id 相同，分位与实际处理数量相同。
function reusableAnalysisJob(userId, { platform, playlistId, percentile, limit, trackTotal }) {
  return analysisSourceJobs(userId).find((job) => {
    if (String(job.platform || "") !== String(platform || "")) return false;
    if (String(job.playlist_id || "") !== String(playlistId || "")) return false;
    if (Number(job.track_percentile) !== Number(percentile)) return false;
    if (Number(job.analyzed_limit) !== Number(limit)) return false;
    if (trackTotal !== null && trackTotal !== undefined && Number(job.track_total) !== Number(trackTotal)) return false;
    return true;
  }) || null;
}

function latestAnalysisJob(userId = null) {
  return analysisSourceJobs(userId)[0] || null;
}

async function allowedSource(body) {
  const raw = String(body.source_url || body.url || "").trim();
  const parsed = parseSource(raw);
  const identity = await canonicalizePlaylistSourceSafe(raw);
  return {
    ...parsed,
    source_url: identity.canonical_url || identity.source_url,
    canonical_url: identity.canonical_url || identity.source_url,
    canonical_key: identity.canonical_key,
    playlist_id: identity.playlist_id || "",
    playlist_name: parsed.playlist_name,
    expected_count: null,
  };
}

async function startWorkflowJob(config, options = {}) {
  const workflowStartedAt = Date.now();
  if (activeJobs.size >= MAX_CONCURRENT_JOBS) {
    throw new Error(`当前已有 ${activeJobs.size} 个任务在运行，请稍后再试`);
  }
  if (options.user && activeJobCountForUser(options.user.id) >= MAX_JOBS_PER_USER) {
    throw new Error(`你已有 ${MAX_JOBS_PER_USER} 个任务在运行，请等待其完成`);
  }
  const runtime = runtimeConfigSnapshot();
  await fs.promises.mkdir(JOB_ROOT, { recursive: true });
  const id = options.jobId
    || `${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-${crypto.randomBytes(4).toString("hex")}`;
  // 断点续跑：重试时沿用同一运行目录，Python 侧会复用已完成的阶段产物。
  const runtimeDir = options.reuseRuntimeDir ? path.resolve(options.reuseRuntimeDir) : path.join(JOB_ROOT, id);
  if (!options.reuseRuntimeDir) await fs.promises.mkdir(runtimeDir);
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
    created_at: new Date(workflowStartedAt).toISOString(),
    updated_at: new Date().toISOString(),
    runtime_dir: runtimeDir,
    user_id: options.user && options.user.id ? Number(options.user.id) : null,
    events: [],
    stderr_tail: "",
    event_seq: 0,
    workflow_mode: options.recommendationOnly ? "recommendation_only" : "full",
    first_run: !options.recommendationOnly && !options.reuseRuntimeDir,
    workflow_time_budget_seconds: runtime.workflowTimeBudgetSeconds,
    source_url: String(config.source_url || ""),
    platform: String(config.platform || ""),
    playlist_id: String(config.playlist_id || ""),
    source_runtime_dir: options.sourceRuntimeDir ? String(options.sourceRuntimeDir) : "",
  };
  const publishedDataPath = atlasDataPathForUser(job.user_id);
  if (!publishedDataPath) throw new Error("无法确定当前用户的 Atlas 发布目录");
  jobs.set(id, job);
  activeJobs.set(id, { userId: job.user_id, startedAt: job.created_at });
  latestJobId = id;
  try {
    authStore.createRun({
      jobId: id,
      userId: job.user_id,
      kind: job.workflow_mode === "recommendation_only" ? "recommendation_only" : "full",
      platform: String(config.platform || ""),
      playlistId: String(config.playlist_id || ""),
      playlistName: String(config.playlist_name || ""),
      runtimeDir,
    });
  } catch (error) {
    console.error(`无法创建运行记录 ${id}：${error.message}`);
  }
  recordJobEvent(job, { event: "queued", status: "queued", stage: "queued", workflow_mode: job.workflow_mode });

  const args = [
    WORKFLOW_SCRIPT,
    "--runtime-dir", runtimeDir,
    "--current-data", publishedDataPath,
    "--source-kind", config.kind,
    "--analysis-parallelism", String(runtime.analysisParallelism),
    "--recommendation-parallelism", String(runtime.recommendationParallelism),
    "--workflow-time-budget", String(runtime.workflowTimeBudgetSeconds),
    "--analysis-timeout", String(runtime.analysisTimeoutSeconds),
    "--recommendation-timeout", String(runtime.recommendationTimeoutSeconds),
    "--max-research-rounds", String(runtime.maxResearchRounds),
    "--initial-candidate-limit", String(runtime.initialCandidateLimit),
    "--hard-candidate-limit", String(runtime.hardCandidateLimit),
    "--await-track-limit",
    "--await-limit-timeout", String(runtime.awaitLimitTimeoutSeconds),
  ];
  if (options.recommendationOnly) {
    args.push("--recommendation-only", "--source-runtime-dir", options.sourceRuntimeDir);
  }
  if (policySnapshotPath) args.push("--policy-file", policySnapshotPath);
  if (editorialSnapshotPath) args.push("--editorial", editorialSnapshotPath);
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
  const remainingSnapshotMs = workflowStartedAt + runtime.workflowTimeBudgetSeconds * 1000 - Date.now();
  if (remainingSnapshotMs <= 0) {
    recordJobEvent(job, { event: "failed", status: "failed", stage: "queued",
      error: `歌单读取超过 ${runtime.workflowTimeBudgetSeconds} 秒预算` });
    activeJobs.delete(id);
    return publicJob(job);
  }
  let workflowTimer = null;
  let snapshotTimer = null;
  const failBudget = (label) => {
    if (TERMINAL_JOB_STATUSES.has(job.status)) return;
    recordJobEvent(job, { event: "failed", status: "failed", stage: job.stage,
      error: `${label}超过 ${runtime.workflowTimeBudgetSeconds} 秒预算` });
    terminateWorkflow(job);
    if (!job.child) activeJobs.delete(id);
  };
  const armProcessingBudget = () => {
    if (job.processing_started_at || TERMINAL_JOB_STATUSES.has(job.status)) return;
    clearTimeout(snapshotTimer);
    job.processing_started_at = new Date().toISOString();
    workflowTimer = setTimeout(() => failBudget("确认范围后的完整处理"), runtime.workflowTimeBudgetSeconds * 1000);
    persistJobState(job);
  };
  job.armProcessingBudget = armProcessingBudget;
  if (!options.recommendationOnly && !options.reuseRuntimeDir) {
    snapshotTimer = setTimeout(() => failBudget("歌单读取"), remainingSnapshotMs);
  }
  // windowsHide：避免在 Windows 上为每个工作流子进程弹出 python.exe 控制台窗口。
  const child = spawnWorkflowProcess(runtime.python, args, {
    cwd: PROJECT_ROOT,
    env: childEnvironment,
  });
  job.child = child;
  if (options.recommendationOnly || options.reuseRuntimeDir) armProcessingBudget();
  if (TERMINAL_JOB_STATUSES.has(job.status)) terminateWorkflow(job);
  let stageTimer = null;
  let budgetStage = null;
  const acceptEvent = (event) => {
    if (TERMINAL_JOB_STATUSES.has(job.status)) return;
    // A child may announce completion before its final artifacts are checked.
    // Defer every success-status event until exit 0 and source-quality validation.
    if (event.status === "completed") {
      if (event.event === "completed") job.pending_completion_event = event;
      return;
    }
    if (event.event === "awaiting_limit") clearTimeout(snapshotTimer);
    if (["analysis", "recommendation", "export"].includes(event.stage)) {
      const stage = event.stage === "export" ? "recommendation" : event.stage;
      if (stage !== budgetStage) {
        clearTimeout(stageTimer);
        budgetStage = stage;
        const seconds = stage === "analysis" ? runtime.analysisTimeoutSeconds : runtime.recommendationTimeoutSeconds;
        stageTimer = setTimeout(() => {
          recordJobEvent(job, { event: "failed", status: "failed", stage,
            error: `${stage === "analysis" ? "分析" : "推荐"}阶段超过 ${seconds} 秒总预算` });
          terminateWorkflow(job);
        }, seconds * 1000);
      }
    }
    recordJobEvent(job, event);
  };
  let stdoutBuffer = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdoutBuffer += chunk;
    const lines = stdoutBuffer.split(/\r?\n/);
    stdoutBuffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try { acceptEvent(JSON.parse(line)); }
      catch { recordJobEvent(job, { event: "diagnostic", status: job.status, stage: job.stage, message: "工作流输出无法解析" }); }
    }
  });
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    job.stderr_tail = (job.stderr_tail + chunk).slice(-2000);
  });
  const release = () => {
    clearTimeout(workflowTimer);
    clearTimeout(snapshotTimer);
    clearTimeout(stageTimer);
    activeJobs.delete(id);
    job.child = undefined;
    job.armProcessingBudget = undefined;
  };
  child.on("error", (error) => {
    job.exit_code = null;
    recordJobEvent(job, { event: "failed", status: "failed", stage: job.stage, error: `无法启动网页工作流：${error.message}` });
    release();
  });
  child.on("close", (code) => {
    job.exit_code = code;
    if (stdoutBuffer.trim()) {
      try { acceptEvent(JSON.parse(stdoutBuffer)); } catch {}
    }
    if (!TERMINAL_JOB_STATUSES.has(job.status)) {
      if (code === 0 && completedWorkflowArtifacts(job, publishedDataPath)) {
        recordJobEvent(job, { ...job.pending_completion_event,
          event: "completed", status: "completed", stage: "export" });
      } else {
        const error = code === 0
          ? job.publication_error || "进程已结束，但完整 Atlas 产物或发布校验未通过"
          : `处理失败（退出码 ${code}），请稍后重试`;
        recordJobEvent(job, { event: "failed", status: "failed", stage: job.stage, error });
      }
    }
    release();
    persistJobState(job);
  });
  return publicJob(job);
}

function terminateWorkflow(job) {
  const child = job.child;
  if (!child) return;
  try { terminateProcessTree(child); }
  catch (error) { console.error(`停止任务失败：${error.message}`); }
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
  if (hit && Date.now() - hit.at < (hit.data.cover ? META_TTL : NEG_TTL)) {
    metaCache.delete(key);
    metaCache.set(key, hit);
    return hit.data;
  }
  if (hit) metaCache.delete(key);
  if (metaInflight.has(key)) return metaInflight.get(key);

  const pending = (async () => {
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
    while (metaCache.size > META_CACHE_MAX) {
      metaCache.delete(metaCache.keys().next().value);
    }
    return data;
  })();
  metaInflight.set(key, pending);
  try {
    return await pending;
  } finally {
    metaInflight.delete(key);
  }
}


function isLocalRequest(req) {
  const address = String(req.socket?.remoteAddress || "");
  if (!isLoopbackAddress(address)) return false;
  // 本机反代会把外部地址放进 X-Forwarded-For。只要链路中存在非回环地址，
  // 请求就不是本机管理请求；这也避免公网客户端伪造一个 127.0.0.1 前缀。
  return forwardedAddresses(req).every(isLoopbackAddress);
}

function sendSettings(req, res, status = 200) {
  const effective = loadEffectiveWebConfig();
  const override = loadSettingsOverride();
  sendJson(res, status, {
    ok: true,
    settings_file: path.relative(PROJECT_ROOT, SETTINGS_PATH),
    overridden: Object.keys(override).length > 0,
    settings: editableSettings(effective),
    active_job_id: visibleJobIdForUser(currentUser(req)?.id ?? null),
    latest_job_id: visibleJobIdForUser(currentUser(req)?.id ?? null),
    applies_to: "next_job",
  });
}

/* ---------------- 静态服务 ---------------- */

restoreRecentJobs();
require("./maintenance").scheduleMaintenance({ authStore, jobRoot: JOB_ROOT,
  completedDays: COMPLETED_JOB_RETENTION_DAYS, otherDays: OTHER_JOB_RETENTION_DAYS });

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
      const user = await authStore.createUserAsync(body.username, body.password, "user");
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
      const user = await authStore.authenticateAsync(body.username, body.password, "user");
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

  if (urlPath === "/api/me/profile" && req.method === "PATCH") {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    try {
      const body = await readJsonBody(req);
      const updated = await authStore.updateUserAsync(user.id, { username: body.username });
      authStore.writeAudit(user.id, "user.profile.update");
      sendJson(res, 200, { ok: true, user: updated });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "资料更新失败" }); }
    return;
  }

  if (urlPath === "/api/me/password" && req.method === "PUT") {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    try {
      const body = await readJsonBody(req);
      if (!(await authStore.verifyUserPasswordAsync(user.id, body.current_password))) throw new Error("当前密码不正确");
      const updated = await authStore.updateUserAsync(user.id, { password: body.new_password });
      // 改密后全部会话已失效；为当前浏览器重建会话，避免修改密码后被登出。
      const session = authStore.createSession(user.id, "user");
      setSessionCookie(res, req, USER_SESSION_COOKIE, session.token, 30 * 24 * 60 * 60);
      authStore.writeAudit(user.id, "user.password.update");
      sendJson(res, 200, { ok: true, user: updated });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "密码修改失败" }); }
    return;
  }

  if (urlPath === "/api/me/runs" && req.method === "GET") {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    const result = authStore.listRuns({ userId: user.id, status: query.get("status") || null, limit: query.get("limit") || 20, offset: query.get("offset") || 0 });
    sendJson(res, 200, { ok: true, ...result,
      runs: result.runs.map(withCompletedRunGroupSummary), stats: authStore.runStats(user.id) });
    return;
  }

  if (urlPath === "/api/me/recommendation-history" && req.method === "GET") {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    const items = readRecommendationHistoryFiles().filter((item) => String(item.user_id || "") === String(user.id));
    sendJson(res, 200, { ok: true, items });
    return;
  }

  const myHistoryMatch = urlPath.match(/^\/api\/me\/recommendation-history\/([^/]+)$/);
  if (myHistoryMatch && req.method === "DELETE") {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    const resolved = resolveHistoryFile(myHistoryMatch[1]);
    if (!resolved || !fs.existsSync(resolved.target)) { sendJson(res, 404, { ok: false, error: "历史文件不存在" }); return; }
    try {
      const payload = JSON.parse(fs.readFileSync(resolved.target, "utf8"));
      if (String(payload.user_id || "") !== String(user.id)) { sendJson(res, 403, { ok: false, error: "只能管理自己的缓存" }); return; }
      fs.rmSync(resolved.target, { force: true });
      authStore.writeAudit(user.id, "user.recommendation_history.clear", { file: resolved.name });
      sendJson(res, 200, { ok: true });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "清除失败" }); }
    return;
  }

  const myHistoryPruneMatch = urlPath.match(/^\/api\/me\/recommendation-history\/([^/]+)\/prune$/);
  if (myHistoryPruneMatch && req.method === "POST") {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    const resolved = resolveHistoryFile(myHistoryPruneMatch[1]);
    if (!resolved || !fs.existsSync(resolved.target)) { sendJson(res, 404, { ok: false, error: "历史文件不存在" }); return; }
    try {
      const payload = JSON.parse(fs.readFileSync(resolved.target, "utf8"));
      if (String(payload.user_id || "") !== String(user.id)) { sendJson(res, 403, { ok: false, error: "只能管理自己的缓存" }); return; }
      const pruned = pruneHistoryEntries(payload);
      fs.writeFileSync(resolved.target, JSON.stringify(pruned.payload, null, 2) + "\n", "utf8");
      authStore.writeAudit(user.id, "user.recommendation_history.prune", { file: resolved.name, removed: pruned.removed });
      sendJson(res, 200, { ok: true, removed: pruned.removed, kept: pruned.kept });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "清理失败" }); }
    return;
  }

  const myHistoryEntryMatch = urlPath.match(/^\/api\/me\/recommendation-history\/([^/]+)\/entries\/([^/]+)$/);
  if (myHistoryEntryMatch && req.method === "DELETE") {
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!user) { sendJson(res, 401, { ok: false, error: "请先登录" }); return; }
    const resolved = resolveHistoryFile(myHistoryEntryMatch[1]);
    const stamp = decodeURIComponent(myHistoryEntryMatch[2]);
    if (!resolved || !fs.existsSync(resolved.target)) { sendJson(res, 404, { ok: false, error: "历史文件不存在" }); return; }
    try {
      const payload = JSON.parse(fs.readFileSync(resolved.target, "utf8"));
      if (String(payload.user_id || "") !== String(user.id)) { sendJson(res, 403, { ok: false, error: "只能管理自己的缓存" }); return; }
      const entries = Array.isArray(payload.entries) ? payload.entries : [];
      const kept = entries.filter((item) => String((item && item.generated_at) || "") !== stamp);
      if (kept.length === entries.length) { sendJson(res, 404, { ok: false, error: "该次记录不存在" }); return; }
      fs.writeFileSync(resolved.target, JSON.stringify({ ...payload, entries: kept }, null, 2) + "\n", "utf8");
      authStore.writeAudit(user.id, "user.recommendation_history.remove_entry", { file: resolved.name, generated_at: stamp });
      sendJson(res, 200, { ok: true, removed: entries.length - kept.length });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "删除失败" }); }
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
      const identity = await canonicalizePlaylistSourceSafe(body.source_url || body.url || "");
      sendJson(res, 201, { ok: true, playlist: authStore.upsertPlaylist(user.id, {
        ...body,
        source_url: identity.canonical_url || identity.source_url,
        canonical_url: identity.canonical_url || identity.source_url,
        canonical_key: identity.canonical_key,
        platform: identity.platform || body.platform,
      }) });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "歌单保存失败" }); }
    return;
  }

  if (urlPath === "/api/admin/login" && req.method === "POST") {
    try {
      const body = await readJsonBody(req);
      const user = await authStore.authenticateAsync(body.username, body.password, "admin");
      const session = authStore.createSession(user.id, "admin", { remember: body.remember === true });
      authStore.writeAudit(user.id, "admin.login");
      setSessionCookie(res, req, ADMIN_SESSION_COOKIE, session.token, session.ttl_seconds);
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
    const users = authStore.listUsers().map((user) => ({ ...user, run_stats: authStore.runStats(user.id) }));
    sendJson(res, 200, { ok: true, users });
    return;
  }

  if (urlPath === "/api/admin/users" && req.method === "POST") {
    const admin = requireAdmin(req, res); if (!admin) return;
    try {
      const body = await readJsonBody(req);
      const role = body.role === "admin" ? "admin" : "user";
      const created = await authStore.createUserAsync(body.username, body.password, role);
      authStore.writeAudit(admin.id, "admin.user.create", { target_user_id: created.id, username: created.username, role });
      sendJson(res, 201, { ok: true, user: created });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "创建用户失败" }); }
    return;
  }

  const adminUserMatch = urlPath.match(/^\/api\/admin\/users\/(\d+)$/);
  if (adminUserMatch && (req.method === "PATCH" || req.method === "PUT")) {
    const admin = requireAdmin(req, res); if (!admin) return;
    try {
      const body = await readJsonBody(req);
      const targetId = Number(adminUserMatch[1]);
      const target = authStore.listUsers().find((user) => user.id === targetId);
      if (!target) throw new Error("用户不存在");
      const self = targetId === Number(admin.id);
      if (self && (body.status === "disabled" || body.role === "user")) throw new Error("不能停用或降级当前登录账号");
      const demoting = target.role === "admin" && (body.role === "user" || body.status === "disabled");
      if (demoting) {
        const activeAdmins = authStore.listUsers().filter((user) => user.role === "admin" && user.status === "enabled").length;
        if (activeAdmins <= 1) throw new Error("至少保留一名启用状态的管理员");
      }
      const updated = await authStore.updateUserAsync(targetId, body);
      authStore.writeAudit(admin.id, "admin.user.update", { target_user_id: targetId, fields: Object.keys(body) });
      sendJson(res, 200, { ok: true, user: updated });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "用户更新失败" }); }
    return;
  }

  if (adminUserMatch && req.method === "DELETE") {
    const admin = requireAdmin(req, res); if (!admin) return;
    try {
      const targetId = Number(adminUserMatch[1]);
      if (targetId === Number(admin.id)) throw new Error("不能删除当前登录账号");
      const users = authStore.listUsers();
      const target = users.find((user) => user.id === targetId);
      if (!target) throw new Error("用户不存在");
      if (target.role === "admin" && users.filter((user) => user.role === "admin").length <= 1) throw new Error("至少保留一名管理员");
      const removed = authStore.deleteUser(targetId);
      authStore.writeAudit(admin.id, "admin.user.delete", { target_user_id: targetId, username: removed.username });
      sendJson(res, 200, { ok: true, user: removed });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "删除用户失败" }); }
    return;
  }

  const adminInitializeMatch = urlPath.match(/^\/api\/admin\/users\/(\d+)\/initialize$/);
  if (adminInitializeMatch && (req.method === "GET" || req.method === "POST")) {
    const admin = requireAdmin(req, res); if (!admin) return;
    try {
      const targetId = Number(adminInitializeMatch[1]);
      if (req.method === "GET") {
        const plan = userResetPlan(targetId);
        sendJson(res, 200, { ok: true, user: plan.user, summary: plan.summary, confirmation_token: plan.token });
        return;
      }
      const body = await readJsonBody(req);
      const plan = userResetPlan(targetId);
      if (plan.summary.active_jobs) { sendJson(res, 409, { ok: false, error: "该账号有运行中的任务，请先等待结束再初始化" }); return; }
      if (body.confirm_username !== plan.user.username || body.confirmation_token !== plan.token) {
        sendJson(res, 409, { ok: false, error: "账号或数据范围已变化，请重新预览并确认" }); return;
      }
      const result = initializeUserAccount(targetId, admin.id, plan);
      sendJson(res, 200, { ok: true, user: result.user, summary: result.summary, archive_id: result.archive_id });
    } catch (error) {
      sendJson(res, /运行中的任务|已变化/.test(error.message || "") ? 409 : 400,
        { ok: false, error: error.message || "账号初始化失败" });
    }
    return;
  }

  const adminUserRunsMatch = urlPath.match(/^\/api\/admin\/users\/(\d+)\/runs$/);
  if (adminUserRunsMatch && req.method === "GET") {
    const admin = requireAdmin(req, res); if (!admin) return;
    const result = authStore.listRuns({ userId: Number(adminUserRunsMatch[1]), status: query.get("status") || null, limit: query.get("limit") || 20, offset: query.get("offset") || 0 });
    sendJson(res, 200, { ok: true, ...result, stats: authStore.runStats(Number(adminUserRunsMatch[1])) });
    return;
  }

  if (urlPath === "/api/admin/runs" && req.method === "GET") {
    const admin = requireAdmin(req, res); if (!admin) return;
    const rawUserId = query.get("user_id");
    const userId = rawUserId ? Number(rawUserId) : null;
    const result = authStore.listRuns({ userId, status: query.get("status") || null, limit: query.get("limit") || 20, offset: query.get("offset") || 0 });
    sendJson(res, 200, { ok: true, ...result, stats: userId ? authStore.runStats(userId) : authStore.runStats() });
    return;
  }

  if (urlPath === "/api/admin/recommendation-history" && req.method === "GET") {
    const admin = requireAdmin(req, res); if (!admin) return;
    const usernames = new Map(authStore.listUsers().map((user) => [String(user.id), user.username]));
    const items = readRecommendationHistoryFiles().map((item) => ({ ...item, username: item.user_id ? (usernames.get(item.user_id) || "") : "" }));
    sendJson(res, 200, { ok: true, items });
    return;
  }

  if (urlPath === "/api/admin/recommendation-history/bulk" && req.method === "DELETE") {
    const admin = requireAdmin(req, res); if (!admin) return;
    try {
      const body = await readJsonBody(req);
      const scope = body.scope;
      const value = scope === "account" ? body.user_id : body.platform;
      if (!["account", "platform", "all"].includes(scope)
          || (scope !== "all" && (typeof value !== "string" || !value.trim()))
          || (scope === "all" && (body.user_id || body.platform))
          || !Array.isArray(body.expected_files)
          || body.expected_files.some((name) => typeof name !== "string")
          || !Number.isSafeInteger(body.expected_entries) || body.expected_entries < 0) {
        sendJson(res, 400, { ok: false, error: "清空范围或确认清单无效" }); return;
      }
      const matches = readRecommendationHistoryFiles().filter((item) => historyMatchesScope(item, scope, value));
      const files = matches.map((item) => item.file).sort();
      const expected = [...body.expected_files].sort();
      const entries = matches.reduce((sum, item) => sum + (item.total_entries || 0), 0);
      if (!files.length || files.length !== expected.length || entries !== body.expected_entries
          || files.some((name, index) => name !== expected[index])) {
        sendJson(res, 409, { ok: false, error: "去重缓存已变化，请刷新并重新确认清空范围" }); return;
      }
      for (const name of files) fs.rmSync(path.join(HISTORY_ROOT, name));
      authStore.writeAudit(admin.id, "admin.recommendation_history.clear_scope", { scope, value: scope === "all" ? "all" : value, files: files.length, entries });
      sendJson(res, 200, { ok: true, removed_files: files.length, removed_entries: entries });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "清空失败" }); }
    return;
  }

  const adminHistoryFileMatch = urlPath.match(/^\/api\/admin\/recommendation-history\/([^/]+)$/);
  if (adminHistoryFileMatch && req.method === "DELETE") {
    const admin = requireAdmin(req, res); if (!admin) return;
    const resolved = resolveHistoryFile(adminHistoryFileMatch[1]);
    if (!resolved || !fs.existsSync(resolved.target)) { sendJson(res, 404, { ok: false, error: "历史文件不存在" }); return; }
    fs.rmSync(resolved.target, { force: true });
    authStore.writeAudit(admin.id, "admin.recommendation_history.clear", { file: resolved.name });
    sendJson(res, 200, { ok: true });
    return;
  }

  const adminHistoryPruneMatch = urlPath.match(/^\/api\/admin\/recommendation-history\/([^/]+)\/prune$/);
  if (adminHistoryPruneMatch && req.method === "POST") {
    const admin = requireAdmin(req, res); if (!admin) return;
    const resolved = resolveHistoryFile(adminHistoryPruneMatch[1]);
    if (!resolved || !fs.existsSync(resolved.target)) { sendJson(res, 404, { ok: false, error: "历史文件不存在" }); return; }
    try {
      const payload = JSON.parse(fs.readFileSync(resolved.target, "utf8"));
      const pruned = pruneHistoryEntries(payload);
      fs.writeFileSync(resolved.target, JSON.stringify(pruned.payload, null, 2) + "\n", "utf8");
      authStore.writeAudit(admin.id, "admin.recommendation_history.prune", { file: resolved.name, removed: pruned.removed });
      sendJson(res, 200, { ok: true, removed: pruned.removed, kept: pruned.kept });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "清理失败" }); }
    return;
  }

  const adminHistoryEntryMatch = urlPath.match(/^\/api\/admin\/recommendation-history\/([^/]+)\/entries\/([^/]+)$/);
  if (adminHistoryEntryMatch && req.method === "DELETE") {
    const admin = requireAdmin(req, res); if (!admin) return;
    const resolved = resolveHistoryFile(adminHistoryEntryMatch[1]);
    const stamp = decodeURIComponent(adminHistoryEntryMatch[2]);
    if (!resolved || !fs.existsSync(resolved.target)) { sendJson(res, 404, { ok: false, error: "历史文件不存在" }); return; }
    try {
      const payload = JSON.parse(fs.readFileSync(resolved.target, "utf8"));
      const entries = Array.isArray(payload.entries) ? payload.entries : [];
      const kept = entries.filter((item) => String((item && item.generated_at) || "") !== stamp);
      if (kept.length === entries.length) { sendJson(res, 404, { ok: false, error: "该次记录不存在" }); return; }
      fs.writeFileSync(resolved.target, JSON.stringify({ ...payload, entries: kept }, null, 2) + "\n", "utf8");
      authStore.writeAudit(admin.id, "admin.recommendation_history.remove_entry", { file: resolved.name, generated_at: stamp });
      sendJson(res, 200, { ok: true, removed: entries.length - kept.length });
    } catch (error) { sendJson(res, 400, { ok: false, error: error.message || "删除失败" }); }
    return;
  }

  if (urlPath === "/api/admin/settings/schema" && req.method === "GET") {
    const admin = requireAdmin(req, res); if (!admin) return;
    sendJson(res, 200, { ok: true, schema: SETTINGS_SCHEMA });
    return;
  }

  const adminSettings = urlPath === "/api/admin/settings" && ["GET", "PUT", "DELETE"].includes(req.method);
  if (adminSettings) {
    const admin = requireAdmin(req, res); if (!admin) return;
    try {
      if (req.method === "GET") sendSettings(req, res);
      else if (req.method === "DELETE") { if (fs.existsSync(SETTINGS_PATH)) fs.rmSync(SETTINGS_PATH, { force: true }); sendSettings(req, res); }
      else {
        const body = await readJsonBody(req);
        const patch = validateSettingsPatch(body.settings ?? body);
        const merged = deepMerge(loadSettingsOverride(), patch);
        validateSettingsPatch(merged); writeSettingsOverride(merged); sendSettings(req, res);
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
        sendSettings(req, res);
      } else if (req.method === "DELETE") {
        if (fs.existsSync(SETTINGS_PATH)) fs.rmSync(SETTINGS_PATH, { force: true });
        sendSettings(req, res);
      } else {
        const body = await readJsonBody(req);
        const patch = validateSettingsPatch(body.settings ?? body);
        const merged = deepMerge(loadSettingsOverride(), patch);
        validateSettingsPatch(merged);
        writeSettingsOverride(merged);
        sendSettings(req, res);
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
            workflow_time_budget_seconds: runtime.workflowTimeBudgetSeconds,
            max_research_rounds: runtime.maxResearchRounds,
            initial_candidate_limit: runtime.initialCandidateLimit,
            hard_candidate_limit: runtime.hardCandidateLimit,
            recommendation_parallelism_options: [1, 2, 3, 4, 5, 6, 7, 8],
            await_limit_timeout_seconds: runtime.awaitLimitTimeoutSeconds,
            analysis_timeout_seconds: runtime.analysisTimeoutSeconds,
            recommendation_timeout_seconds: runtime.recommendationTimeoutSeconds,
          } : {}),
          track_percentile_options: runtime.trackPercentileOptions,
          track_percentile_default: runtime.trackPercentileDefault,
          apple_requires_expected_count: false,
        },
        active_job_id: visibleJob(visibleJobIdForUser(currentUser(req)?.id ?? null)),
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
      if (user && activeJobCountForUser(user.id) >= MAX_JOBS_PER_USER) throw new Error(`你已有 ${MAX_JOBS_PER_USER} 个任务在运行，请等待其完成`);
      const baseJob = latestAnalysisJob(user && user.id);
      if (!baseJob) throw new Error("没有 24 小时内可复用的分析结果，请回到推荐源重新运行");
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
      sendJson(res, (message.includes("已有网页工作流") || message.includes("任务在运行")) ? 409 : 400, { ok: false, error: message });
    }
    return;
  }

  if (urlPath === "/api/jobs" && req.method === "POST") {
    try {
      const user = requireUser(req, res);
      if (!user && AUTH_REQUIRED) return;
      const body = await readJsonBody(req);
      const config = await allowedSource(body);
      const runtime = runtimeConfigSnapshot();
      if (!runtime.analysisExecutor.command || !runtime.recommendationExecutor.command) {
        sendJson(res, 503, { ok: false, error: "当前暂不可生成推荐" });
        return;
      }
      const job = await startWorkflowJob(config, { user });
      if (user) authStore.upsertPlaylist(user.id, {
        source_url: config.source_url,
        canonical_url: config.canonical_url,
        canonical_key: config.canonical_key,
        name: config.playlist_name,
        platform: config.platform,
        config,
      });
      sendJson(res, 202, { ok: true, job });
    } catch (error) {
      const message = error && error.message ? error.message : "无法创建网页工作流";
      sendJson(res, (message.includes("已有网页工作流") || message.includes("任务在运行")) ? 409 : 400, { ok: false, error: message });
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
      job.track_percentile = percentile;
      job.analyzed_limit = limit;
      job.track_total = maximum !== null ? maximum : null;
      // 同链接（平台 + 歌单 id）与同范围（分位 + 实际数量）双匹配，且分析在
      // 24 小时内完成时，才复用已有分析直接生成推荐；否则按所选范围重新分析。
      const reusable = reusableAnalysisJob(user && user.id, {
        platform: job.platform, playlistId: job.playlist_id,
        percentile, limit, trackTotal: maximum,
      });
      if (reusable && job.child) {
        terminateWorkflow(job);
        recordJobEvent(job, { event: "superseded", status: "superseded", stage: job.stage, error: "已改用同链接、同范围的已有分析" });
        activeJobs.delete(job.id);
        const reused = await startWorkflowJob(
          { kind: "local_json", source_url: job.source_url || "", input: "", playlist_id: job.playlist_id || "",
            playlist_name: "", platform: job.platform || "", expected_count: null },
          { recommendationOnly: true, sourceRuntimeDir: reusable.runtime_dir, user });
        reused.track_percentile = percentile;
        reused.analyzed_limit = limit;
        reused.track_total = maximum !== null ? maximum : null;
        sendJson(res, 202, { ok: true, job: publicJob(reused), reused_from: reusable.id,
                             reused_at: reusable.updated_at, limit, percentile });
        return;
      }
      const requestPath = path.join(job.runtime_dir, LIMIT_REQUEST_FILENAME);
      const temporary = `${requestPath}.${process.pid}.tmp`;
      await fs.promises.writeFile(temporary, JSON.stringify({ limit, percentile }), "utf8");
      await fs.promises.rename(temporary, requestPath);
      job.armProcessingBudget?.();
      sendJson(res, 202, { ok: true, job: publicJob(job), limit, percentile });
    } catch (error) {
      sendJson(res, 400, { ok: false, error: error && error.message ? error.message : "无法提交处理数量" });
    }
    return;
  }

  // 断点续跑：同一任务失败后重新启动，沿用原运行目录与已完成的阶段产物。
  const retryMatch = urlPath.match(/^\/api\/jobs\/([^/]+)\/retry$/);
  if (retryMatch && req.method === "POST") {
    const job = jobs.get(decodeURIComponent(retryMatch[1]));
    if (!job) { sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" }); return; }
    const user = requireUser(req, res);
    if (!canAccessJob(job, user)) { sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" }); return; }
    if (!["failed", "interrupted"].includes(job.status)) { sendJson(res, 409, { ok: false, error: "只有失败或中断的任务可以续跑" }); return; }
    try {
      const config = { kind: "local_json", source_url: job.source_url || "", input: "",
                       playlist_id: job.playlist_id || "", playlist_name: "",
                       platform: job.platform || "", expected_count: null };
      const retried = await startWorkflowJob(config, { user, jobId: job.id, reuseRuntimeDir: job.runtime_dir });
      sendJson(res, 202, { ok: true, job: publicJob(retried), resumed: true });
    } catch (error) {
      const message = error && error.message ? error.message : "无法续跑任务";
      sendJson(res, (message.includes("已有网页工作流") || message.includes("任务在运行")) ? 409 : 400, { ok: false, error: message });
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
    terminateWorkflow(job);
    recordJobEvent(job, { event: "cancelled", status: "cancelled", stage: job.stage, error: "任务已取消" });
    sendJson(res, 200, { ok: true, job: publicJob(job) });
    return;
  }

  // 仅暴露已经锁定的三组曲目；旧的临时推荐绝不作为正式曲目返回。
  const selectionMatch = urlPath.match(/^\/api\/jobs\/([^/]+)\/selection$/);
  if (selectionMatch && req.method === "GET") {
    const job = jobs.get(decodeURIComponent(selectionMatch[1]));
    if (!job) { sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" }); return; }
    const user = requireUser(req, res);
    if (!user && AUTH_REQUIRED) return;
    if (!canAccessJob(job, user)) { sendJson(res, 404, { ok: false, error: "找不到网页工作流任务" }); return; }
    try {
      const selectionPath = path.join(job.runtime_dir, "web_selection.json");
      const selection = JSON.parse(await fs.promises.readFile(selectionPath, "utf8"));
      const groups = selection.atlas_groups;
      if (selection.status !== "tracks_locked" || !Array.isArray(groups) || groups.length !== 3) {
        throw new Error("selection not locked");
      }
      const ids = new Set();
      const trackKeys = new Set();
      const safeGroups = groups.map((group, groupIndex) => {
        if (!Array.isArray(group.recommendations) || group.recommendations.length !== 10) {
          throw new Error("incomplete selection group");
        }
        return {
          id: `atlas-${groupIndex + 1}`,
          label: `第 ${groupIndex + 1} 组`,
          recommendations: group.recommendations.map((item) => {
            const id = String(item.canonical_track_id || item.id || "").trim();
            const title = String(item.title || "").trim();
            const artist = String(item.artist || "").trim();
            const key = `${title.toLocaleLowerCase()}\0${artist.toLocaleLowerCase()}`;
            if (!id || !title || !artist || ids.has(id) || trackKeys.has(key)) {
              throw new Error("invalid or duplicate locked track");
            }
            ids.add(id);
            trackKeys.add(key);
            return {
              id, title, artist,
              url: String(item.url || item.metadata_verified?.url || ""),
              platform: String(item.platform || item.metadata_verified?.source || ""),
            };
          }),
        };
      });
      sendJson(res, 200, { ok: true, selection: {
        status: "tracks_locked", playlist_name: String(selection.playlist_name || ""),
        source_track_count: Number(selection.source_track_count) || 0,
        style_analysis: String(selection.style_analysis || selection.overall_summary || ""),
        atlas_groups: safeGroups, locked_at: String(selection.locked_at || ""),
        details_status: "pending",
      } }, { "Cache-Control": "no-store" });
    } catch { sendJson(res, 404, { ok: false, error: "正式曲目尚未确定" }); }
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
    const user = currentUser(req, "user");
    const dataPath = atlasDataPathForUser(user && user.id);
    if (!dataPath) {
      sendJson(res, 200, { ok: true, empty: true, reason: "no_user_atlas" });
      return;
    }
    try {
      const response = await loadAtlasResponse(dataPath);
      res.writeHead(200, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
      });
      res.end(response);
    } catch (error) {
      if (error && error.code === "ENOENT") {
        sendJson(res, 200, { ok: true, empty: true, reason: "no_user_atlas" });
        return;
      }
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
    if (artist.length > META_QUERY_MAX_LENGTH || track.length > META_QUERY_MAX_LENGTH) {
      sendJson(res, 400, { ok: false, error: `artist & track must be at most ${META_QUERY_MAX_LENGTH} characters` });
      return;
    }
    if (!allowMetaRequest(req)) {
      sendJson(res, 429, { ok: false, error: "too many metadata requests" }, { "Retry-After": "60" });
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
      res.end(`<!doctype html><meta charset="utf-8"><body style="background:#0f0d0a;color:#a39a88;font-family:'Cormorant Garamond','思源宋体 CN',serif;display:grid;place-items:center;height:100vh;margin:0"><div>404 · 页面不存在<br><br><a href="/" style="color:#c8622f">← 回到首页</a></div>`);
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
    process.exit(1);
  }
  console.error("[music-atlas] 启动失败:", err.message);
  process.exit(1);
});

authStore.reconcilePlaylists(canonicalizePlaylistSourceSafe)
  .then((result) => {
    if (result.merged || result.normalized) {
      console.log(`[music-atlas] 最近歌单身份回填：规范化 ${result.normalized}，合并 ${result.merged}`);
    }
    server.listen(PORT, HOST, () => {
      console.log(`[music-atlas] Editorial Atlas 已固化: http://${HOST}:${PORT}`);
    });
  })
  .catch((error) => {
    console.error(`[music-atlas] 最近歌单迁移失败：${error.message}`);
    process.exit(1);
  });
