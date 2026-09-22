"use strict";

const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const { pipeline } = require("node:stream/promises");
const { createHash } = require("node:crypto");

const TERMINAL = new Set(["completed", "failed", "cancelled", "superseded", "interrupted"]);
const KEEP = new Set(["web_job_state.json", "web_job_report.json", "snapshot.json",
  "musician_analysis.json", "review_report.json", "evidence_audit.json"]);

async function digest(stream) {
  const hash = createHash("sha256");
  for await (const chunk of stream) hash.update(chunk);
  return hash.digest("hex");
}

async function compressVerified(file) {
  const target = `${file}.gz`;
  const temp = `${target}.${process.pid}.tmp`;
  // Never overwrite an existing archive, or follow a symbolic link.
  if (fs.existsSync(target) || !(await fs.promises.lstat(file)).isFile()) return 0;
  const before = await fs.promises.stat(file);
  try {
    await pipeline(fs.createReadStream(file), zlib.createGzip(), fs.createWriteStream(temp, { flags: "wx", mode: 0o600 }));
    const original = await digest(fs.createReadStream(file));
    const recovered = await digest(fs.createReadStream(temp).pipe(zlib.createGunzip()));
    const after = await fs.promises.stat(file);
    if (original !== recovered || before.mtimeMs !== after.mtimeMs || before.size !== after.size) {
      throw new Error("archive verification failed or source changed");
    }
    await fs.promises.rename(temp, target);
    await fs.promises.unlink(file);
    return Math.max(0, before.size - (await fs.promises.stat(target)).size);
  } finally {
    await fs.promises.unlink(temp).catch((error) => { if (error.code !== "ENOENT") throw error; });
  }
}

async function archiveJobArtifacts(root, { completedDays = 14, otherDays = 7, now = Date.now() } = {}) {
  if (![completedDays, otherDays].every((days) => Number.isFinite(days) && days >= 1)) throw new Error("invalid retention days");
  root = path.resolve(root);
  if (!fs.existsSync(root)) return { archived: 0, savedBytes: 0 };
  let archived = 0, savedBytes = 0;
  for (const entry of await fs.promises.readdir(root, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dir = path.join(root, entry.name);
    const stateFile = path.join(dir, "web_job_state.json");
    if (!fs.existsSync(stateFile) || (await fs.promises.lstat(stateFile)).isSymbolicLink()) continue;
    const state = JSON.parse(await fs.promises.readFile(stateFile, "utf8"));
    const updated = Date.parse(state.updated_at);
    if (state.id !== entry.name || !TERMINAL.has(state.status) || !Number.isFinite(updated)) continue;
    if (now - updated < (state.status === "completed" ? completedDays : otherDays) * 86400000) continue;
    const queue = [dir];
    while (queue.length) {
      const current = queue.pop();
      for (const child of await fs.promises.readdir(current, { withFileTypes: true })) {
        const file = path.join(current, child.name);
        if (child.isDirectory()) queue.push(file);
        else if (child.isFile() && !KEEP.has(child.name) && !child.name.startsWith("web_payload")
          && /\.(json|txt|md|log|ndjson)$/.test(child.name)) {
          const saved = await compressVerified(file);
          if (saved || !fs.existsSync(file)) { archived++; savedBytes += saved; }
        }
      }
    }
  }
  return { archived, savedBytes };
}

function scheduleMaintenance({ authStore, jobRoot, completedDays, otherDays }) {
  let running = false;
  const run = async () => {
    if (running) return;
    running = true;
    try {
      const database = authStore.maintenance();
      const archive = await archiveJobArtifacts(jobRoot, { completedDays, otherDays });
      if (database.sessions || database.audits || archive.archived) console.log("Atlas maintenance", { database, archive });
      const disk = await fs.promises.statfs(fs.existsSync(jobRoot) ? jobRoot : path.dirname(jobRoot));
      const used = Math.round((1 - Number(disk.bavail) / Number(disk.blocks)) * 1000) / 10;
      if (used >= 70) console.error(`磁盘${used >= 85 ? "严重" : "预警"}：使用 ${used}%，阈值 70% / 85%`);
    } catch (error) { console.error(`例行维护失败：${error.message}`); }
    finally { running = false; }
  };
  void run();
  return setInterval(run, 86400000).unref();
}

module.exports = { archiveJobArtifacts, compressVerified, scheduleMaintenance };
