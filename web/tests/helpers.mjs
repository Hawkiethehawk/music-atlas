/**
 * Music Atlas web E2E 测试共享工具。
 *
 * 隔离原则：
 * - 每个测试套件在 runtime/web-e2e-<时间戳>-<随机>/ 下建立独立运行目录（Git 忽略 runtime/）。
 * - 通过 ATLAS_WEB_CONFIG 让 server.js 读取临时配置，使用随机空闲端口启动独立实例，
 *   不触碰 config/web.json、真实 8420 服务与真实运行数据。
 * - 夹具工作流只使用 tests/fixtures/ 的合成执行器，不联网、不发送消息。
 */

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const WEB_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const PROJECT_ROOT = path.resolve(WEB_DIR, "..");
const PYTHON = process.env.PYTHON || "python";

/** 探测一个空闲 TCP 端口（存在轻微竞态，仅用于测试）。 */
export function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

export function uniqueRuntimeDir(label) {
  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
  return path.join(PROJECT_ROOT, "runtime", `web-e2e-${label}-${stamp}-${randomBytes(3).toString("hex")}`);
}

function projectRelative(absolutePath) {
  return path.relative(PROJECT_ROOT, absolutePath).split(path.sep).join("/");
}

/**
 * 启动隔离的 server.js 实例。
 * options.executors 为 false 时模拟"执行器未配置"（POST /api/jobs 应返回 503）。
 */
export async function createIsolatedServer({ label, executors = true, settings = null } = {}) {
  const runtimeDir = uniqueRuntimeDir(label || "server");
  await mkdir(runtimeDir, { recursive: true });
  const port = await getFreePort();
  const config = {
    server: { host: "127.0.0.1", port },
    paths: {
      published: projectRelative(path.join(runtimeDir, "current.json")),
      jobs: projectRelative(path.join(runtimeDir, "jobs")),
      input: "input",
    },
    runtime: { python: PYTHON },
    workflow: {
      analysis_parallelism: 5,
      recommendation_parallelism: 4,
      analysis_timeout_seconds: 120,
      recommendation_timeout_seconds: 120,
    },
  };
  if (executors) {
    config.executors = {
      analysis: "tests/fixtures/fake_analysis_agent.py",
      recommendation: "tests/fixtures/fake_agent.py",
    };
  } else {
    config.executors = { analysis: "", recommendation: "" };
  }
  const configPath = path.join(runtimeDir, "web.config.json");
  await writeFile(configPath, JSON.stringify(config, null, 2), "utf8");
  // 可选的预置网页设置覆盖（用于验证旧配置迁移）。
  if (settings) {
    await writeFile(path.join(runtimeDir, "settings.json"), JSON.stringify(settings, null, 2), "utf8");
  }

  // 密钥库隔离：用夹具脚本 + 临时状态文件，测试不会触碰真实系统密钥库。
  const secretStatePath = path.join(runtimeDir, "fake-secret.json");
  const secretScript = path.join(PROJECT_ROOT, "tests", "fixtures", "fake_secret_store.py");

  const childEnv = {
    ...process.env,
    ATLAS_WEB_CONFIG: configPath,
    ATLAS_WEB_SECRET_SCRIPT: secretScript,
    ATLAS_SECRET_STATE_FILE: secretStatePath,
  };
  // 测试确定性：不让宿主机的真实 API Key 环境变量影响探测结果。
  delete childEnv.MUSIC_ATLAS_API_KEY;
  // 夹具曲目为合成数据，平台上不存在；关闭平台元数据核验，
  // 否则候选会被当作“未核实”全部丢弃（核验本身由 Python 测试覆盖）。
  childEnv.ATLAS_METADATA_VERIFY = "off";

  const proc = spawn(process.execPath, ["server.js"], {
    cwd: WEB_DIR,
    env: childEnv,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  proc.stderr.on("data", (chunk) => { stderr += chunk; });
  const exited = new Promise((resolve) => proc.on("close", (code) => resolve(code)));
  const baseUrl = `http://127.0.0.1:${port}`;

  // 等待 /api/config 就绪（不依赖 Atlas 数据，health 在无数据时是 503）。
  const deadline = Date.now() + 15000;
  let ready = false;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) throw new Error(`server.js 提前退出（code=${proc.exitCode}）：${stderr}`);
    try {
      const response = await fetch(`${baseUrl}/api/config`);
      if (response.ok) { ready = true; break; }
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (!ready) {
    proc.kill();
    throw new Error(`隔离服务器未在期限内就绪：${stderr}`);
  }

  return {
    proc,
    port,
    baseUrl,
    runtimeDir,
    configPath,
    secretStatePath,
    exited,
    stderr: () => stderr,
    async stop() {
      if (proc.exitCode === null) proc.kill();
      await exited;
    },
  };
}

/** 读取 SSE 响应流，解析每帧 data: JSON，逐条回调；abort 触发后返回已收集的事件。 */
export async function readSseEvents(response, onData, { signal } = {}) {
  const decoder = new TextDecoder();
  let buffer = "";
  const frames = [];
  try {
    for await (const chunk of response.body) {
      buffer += decoder.decode(chunk, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const dataLine = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("");
        if (!dataLine) continue;
        try {
          const payload = JSON.parse(dataLine);
          frames.push(payload);
          onData?.(payload);
        } catch {}
      }
      if (signal?.aborted) break;
    }
  } catch (error) {
    if (!signal?.aborted) throw error;
  }
  return frames;
}

/**
 * 在隔离 runtime 目录运行真实夹具工作流（CLI 入口 web_workflow.py）。
 * 返回退出码、逐行事件与产物路径；事件同时落盘 workflow-events.ndjson 供验收引用。
 */
export async function runFixtureWorkflow({ label, playlistName = "示例歌单" } = {}) {
  const dir = uniqueRuntimeDir(label || "fixture");
  const jobDir = path.join(dir, "job");
  const currentData = path.join(dir, "current.json");
  await mkdir(jobDir, { recursive: true });
  const args = [
    "tests/fixtures/web_workflow_fixture.py",
    "--runtime-dir", jobDir,
    "--current-data", currentData,
    "--source-kind", "local_json",
    "--input", "tests/fixtures/playlist_sample.json",
    "--platform", "apple_music",
    "--playlist-id", "sample",
    "--playlist-name", playlistName,
    "--analysis-command", `${PYTHON} tests/fixtures/fake_analysis_agent.py`,
    "--recommendation-command", `${PYTHON} tests/fixtures/fake_agent.py`,
    "--analysis-batch-size", "2",
  ];
  const proc = spawn(PYTHON, args, {
    cwd: PROJECT_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    // 夹具曲目为合成数据，平台上不存在；关闭平台元数据核验，
    // 否则候选会被当作“未核实”全部丢弃。
    env: { ...process.env, ATLAS_METADATA_VERIFY: "off" },
  });
  const events = [];
  let stdout = "";
  let stderr = "";
  proc.stdout.on("data", (chunk) => { stdout += chunk; });
  proc.stderr.on("data", (chunk) => { stderr += chunk; });
  const code = await new Promise((resolve) => proc.on("close", resolve));
  for (const line of stdout.split(/\r?\n/)) {
    const text = line.trim();
    if (!text) continue;
    try { events.push(JSON.parse(text)); } catch {}
  }
  await writeFile(path.join(dir, "workflow-events.ndjson"), stdout, "utf8");
  return { code, events, stderr, dir, jobDir, currentData };
}

/** 轮询等待函数返回真值。 */
export async function waitFor(fn, { timeout = 10000, interval = 50, message = "条件未满足" } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
  throw new Error(`等待超时：${message}`);
}
