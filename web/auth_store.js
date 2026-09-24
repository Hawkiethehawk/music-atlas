"use strict";

/*
 * Music Atlas authentication and per-user storage.
 *
 * The web service deliberately uses Node's built-in SQLite driver so the
 * deployed service has no extra native dependency.  Passwords are stored as
 * scrypt hashes and browser cookies only contain opaque, hashed sessions.
 */
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { DatabaseSync } = require("node:sqlite");
const scryptAsync = require("node:util").promisify(crypto.scrypt);

async function hashPasswordAsync(password) {
  const salt = crypto.randomBytes(16);
  const derived = await scryptAsync(normalizePassword(password), salt, 64, { N: 16384, r: 8, p: 1 });
  return `scrypt$16384$8$1$${salt.toString("base64url")}$${derived.toString("base64url")}`;
}

async function verifyPasswordAsync(password, encoded) {
  try {
    const [scheme, n, r, p, saltText, digestText] = String(encoded || "").split("$");
    if (scheme !== "scrypt" || n !== "16384" || r !== "8" || p !== "1") return false;
    const expected = Buffer.from(digestText, "base64url");
    if (expected.length !== 64) return false;
    const actual = await scryptAsync(String(password || ""), Buffer.from(saltText, "base64url"), 64, { N: 16384, r: 8, p: 1 });
    return crypto.timingSafeEqual(actual, expected);
  } catch { return false; }
}

const SESSION_TTL_SECONDS = 30 * 24 * 60 * 60;
const ADMIN_SESSION_TTL_SECONDS = 8 * 60 * 60;

function isoNow() {
  return new Date().toISOString();
}

function normalizeUsername(value) {
  const username = String(value || "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$/.test(username)) {
    throw new Error("用户名需为 3 到 64 位字母、数字、下划线、点或短横线");
  }
  return username;
}

function normalizePassword(value) {
  const password = String(value || "");
  if (password.length < 8 || password.length > 200) throw new Error("密码需为 8 到 200 个字符");
  return password;
}

function hashPassword(password, salt = crypto.randomBytes(16)) {
  const derived = crypto.scryptSync(password, salt, 64, { N: 16384, r: 8, p: 1 });
  return `scrypt$16384$8$1$${salt.toString("base64url")}$${derived.toString("base64url")}`;
}

function verifyPassword(password, encoded) {
  try {
    const [scheme, n, r, p, saltText, digestText] = String(encoded || "").split("$");
    if (scheme !== "scrypt" || !n || !r || !p || !saltText || !digestText) return false;
    const salt = Buffer.from(saltText, "base64url");
    const expected = Buffer.from(digestText, "base64url");
    const actual = crypto.scryptSync(password, salt, expected.length, { N: Number(n), r: Number(r), p: Number(p) });
    return actual.length === expected.length && crypto.timingSafeEqual(actual, expected);
  } catch {
    return false;
  }
}

function tokenHash(token) {
  return crypto.createHash("sha256").update(String(token)).digest("hex");
}

function publicUser(row) {
  if (!row) return null;
  return {
    id: Number(row.id),
    username: row.username,
    role: row.role,
    status: row.status,
    created_at: row.created_at,
    last_login_at: row.last_login_at || null,
  };
}

function createAuthStore(dbPath) {
  const resolvedPath = path.resolve(dbPath);
  fs.mkdirSync(path.dirname(resolvedPath), { recursive: true });
  const db = new DatabaseSync(resolvedPath);
  db.exec(`
    PRAGMA journal_mode = WAL;
    PRAGMA foreign_keys = ON;
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL COLLATE NOCASE UNIQUE,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
      status TEXT NOT NULL DEFAULT 'enabled' CHECK (status IN ('enabled', 'disabled')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      last_login_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      token_hash TEXT NOT NULL UNIQUE,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind TEXT NOT NULL CHECK (kind IN ('user', 'admin')),
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      revoked_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, kind);
    CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at, revoked_at);
    CREATE TABLE IF NOT EXISTS preferences (
      user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      data_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS playlists (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      source_url TEXT NOT NULL,
      canonical_key TEXT NOT NULL DEFAULT '',
      canonical_url TEXT NOT NULL DEFAULT '',
      name TEXT NOT NULL DEFAULT '',
      platform TEXT NOT NULL DEFAULT '',
      config_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(user_id, source_url)
    );
    CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists(user_id, updated_at DESC);
    CREATE TABLE IF NOT EXISTS audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
      action TEXT NOT NULL,
      detail_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
    CREATE TABLE IF NOT EXISTS runs (
      job_id TEXT PRIMARY KEY,
      user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
      kind TEXT NOT NULL DEFAULT 'full',
      platform TEXT NOT NULL DEFAULT '',
      playlist_id TEXT NOT NULL DEFAULT '',
      playlist_name TEXT NOT NULL DEFAULT '',
      track_count INTEGER,
      analyzed_count INTEGER,
      status TEXT NOT NULL DEFAULT 'running',
      started_at TEXT NOT NULL,
      finished_at TEXT,
      duration_ms INTEGER,
      recommendation_count INTEGER,
      runtime_dir TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
  `);

  // 旧生产库原地升级。先增加可空身份列，再使用部分唯一索引；异步短链
  // 回填会在 server 启动监听前运行，并在写入 canonical_key 时合并重复行。
  const playlistColumns = new Set(db.prepare("PRAGMA table_info(playlists)").all().map((row) => row.name));
  if (!playlistColumns.has("canonical_key")) db.exec("ALTER TABLE playlists ADD COLUMN canonical_key TEXT NOT NULL DEFAULT ''");
  if (!playlistColumns.has("canonical_url")) db.exec("ALTER TABLE playlists ADD COLUMN canonical_url TEXT NOT NULL DEFAULT ''");
  db.exec(`
    CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_user_canonical
      ON playlists(user_id, canonical_key) WHERE canonical_key <> '';
  `);

  function getUserById(id) {
    return db.prepare("SELECT * FROM users WHERE id = ?").get(Number(id)) || null;
  }

  function createUser(usernameValue, passwordValue, role = "user", preparedHash = null) {
    const username = normalizeUsername(usernameValue);
    const password = normalizePassword(passwordValue);
    if (!["user", "admin"].includes(role)) throw new Error("用户角色无效");
    const now = isoNow();
    try {
      const result = db.prepare(
        "INSERT INTO users (username, password_hash, role, status, created_at, updated_at) VALUES (?, ?, ?, 'enabled', ?, ?)",
      ).run(username, preparedHash || hashPassword(password), role, now, now);
      return publicUser(getUserById(result.lastInsertRowid));
    } catch (error) {
      if (String(error && error.message).includes("UNIQUE")) throw new Error("用户名已存在");
      throw error;
    }
  }

  function ensureBootstrapAdmin(usernameValue, passwordValue) {
    const count = Number(db.prepare("SELECT COUNT(*) AS count FROM users WHERE role = 'admin'").get().count || 0);
    if (count > 0) return false;
    createUser(usernameValue, passwordValue, "admin");
    return true;
  }

  function authenticate(usernameValue, passwordValue, requiredRole = "user") {
    const username = String(usernameValue || "").trim();
    const password = String(passwordValue || "");
    const row = db.prepare("SELECT * FROM users WHERE username = ? COLLATE NOCASE").get(username);
    if (!row || row.status !== "enabled" || !verifyPassword(password, row.password_hash)) {
      throw new Error("用户名或密码错误");
    }
    if (requiredRole === "admin" && row.role !== "admin") throw new Error("无管理员权限");
    const now = isoNow();
    db.prepare("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?").run(now, now, row.id);
    return publicUser({ ...row, last_login_at: now });
  }

  function verifyUserPassword(id, passwordValue) {
    const user = getUserById(id);
    if (!user) return false;
    return verifyPassword(String(passwordValue || ""), user.password_hash);
  }

  function createSession(userId, kind = "user", options = {}) {
    if (!["user", "admin"].includes(kind)) throw new Error("会话类型无效");
    const user = getUserById(userId);
    if (!user || user.status !== "enabled") throw new Error("用户不可用");
    if (kind === "admin" && user.role !== "admin") throw new Error("无管理员权限");
    const token = crypto.randomBytes(32).toString("base64url");
    const now = new Date();
    const ttlSeconds = kind === "admin" && options.remember !== true ? ADMIN_SESSION_TTL_SECONDS : SESSION_TTL_SECONDS;
    const expires = new Date(now.getTime() + ttlSeconds * 1000);
    db.prepare("INSERT INTO sessions (token_hash, user_id, kind, created_at, expires_at) VALUES (?, ?, ?, ?, ?)")
      .run(tokenHash(token), Number(userId), kind, now.toISOString(), expires.toISOString());
    return { token, user: publicUser(user), expires_at: expires.toISOString(), ttl_seconds: ttlSeconds };
  }

  function getSessionUser(token, kind = "user") {
    if (!token) return null;
    const row = db.prepare(`
      SELECT u.*, s.expires_at, s.revoked_at
      FROM sessions s JOIN users u ON u.id = s.user_id
      WHERE s.token_hash = ? AND s.kind = ?
    `).get(tokenHash(token), kind);
    if (!row || row.revoked_at || row.status !== "enabled" || Date.parse(row.expires_at) <= Date.now()) return null;
    if (kind === "admin" && row.role !== "admin") return null;
    return publicUser(row);
  }

  function revokeSession(token, kind) {
    if (token) db.prepare("UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND kind = ?").run(isoNow(), tokenHash(token), kind);
  }

  function listUsers() {
    return db.prepare("SELECT id, username, role, status, created_at, updated_at, last_login_at FROM users ORDER BY created_at DESC")
      .all().map(publicUser);
  }

  function updateUser(id, patch = {}, preparedHash = null) {
    const user = getUserById(id);
    if (!user) throw new Error("用户不存在");
    const fields = [];
    const values = [];
    if (patch.username !== undefined) {
      const username = normalizeUsername(patch.username);
      const taken = db.prepare("SELECT id FROM users WHERE username = ? COLLATE NOCASE AND id != ?").get(username, Number(id));
      if (taken) throw new Error("用户名已被占用");
      fields.push("username = ?"); values.push(username);
    }
    if (patch.status !== undefined) {
      if (!["enabled", "disabled"].includes(patch.status)) throw new Error("用户状态无效");
      fields.push("status = ?"); values.push(patch.status);
    }
    if (patch.role !== undefined) {
      if (!["user", "admin"].includes(patch.role)) throw new Error("用户角色无效");
      fields.push("role = ?"); values.push(patch.role);
    }
    if (patch.password !== undefined) {
      fields.push("password_hash = ?"); values.push(preparedHash || hashPassword(normalizePassword(patch.password)));
    }
    if (!fields.length) return publicUser(user);
    fields.push("updated_at = ?"); values.push(isoNow(), Number(id));
    db.prepare(`UPDATE users SET ${fields.join(", ")} WHERE id = ?`).run(...values);
    // 停用、降级或改密后，该账号的所有会话立即失效。
    if (patch.status === "disabled" || patch.role === "user" || patch.password !== undefined) {
      db.prepare("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL").run(isoNow(), Number(id));
    }
    return publicUser(getUserById(id));
  }

  function deleteUser(id) {
    const user = getUserById(id);
    if (!user) throw new Error("用户不存在");
    // 外键级联：sessions / preferences / playlists 同步删除；audit_log 与 runs 保留且置空 user_id。
    db.prepare("DELETE FROM users WHERE id = ?").run(Number(id));
    return publicUser(user);
  }

  function userResetSnapshot(id) {
    const user = getUserById(id);
    if (!user) throw new Error("用户不存在");
    const userId = Number(user.id);
    return {
      user: publicUser(user),
      preferences: db.prepare("SELECT * FROM preferences WHERE user_id = ?").all(userId),
      playlists: db.prepare("SELECT * FROM playlists WHERE user_id = ? ORDER BY id").all(userId),
      runs: db.prepare("SELECT * FROM runs WHERE user_id = ? ORDER BY job_id").all(userId),
      session_count: Number(db.prepare("SELECT COUNT(*) AS count FROM sessions WHERE user_id = ?").get(userId).count),
    };
  }

  function resetUserData(id, actorId, archiveId) {
    const snapshot = userResetSnapshot(id);
    const userId = Number(snapshot.user.id);
    db.exec("BEGIN IMMEDIATE");
    try {
      db.prepare("DELETE FROM sessions WHERE user_id = ?").run(userId);
      db.prepare("DELETE FROM preferences WHERE user_id = ?").run(userId);
      db.prepare("DELETE FROM playlists WHERE user_id = ?").run(userId);
      db.prepare("DELETE FROM runs WHERE user_id = ?").run(userId);
      db.prepare("UPDATE users SET last_login_at = NULL, updated_at = ? WHERE id = ?").run(isoNow(), userId);
      writeAudit(actorId, "admin.user.initialize", {
        target_user_id: userId, username: snapshot.user.username, archive_id: archiveId,
        playlists: snapshot.playlists.length, runs: snapshot.runs.length,
        preferences: snapshot.preferences.length, sessions: snapshot.session_count,
      });
      db.exec("COMMIT");
      return publicUser(getUserById(userId));
    } catch (error) {
      db.exec("ROLLBACK");
      throw error;
    }
  }

  function publicRun(row) {
    return {
      job_id: row.job_id, user_id: row.user_id, kind: row.kind,
      platform: row.platform, playlist_id: row.playlist_id, playlist_name: row.playlist_name,
      track_count: row.track_count, analyzed_count: row.analyzed_count,
      status: row.status, started_at: row.started_at, finished_at: row.finished_at,
      duration_ms: row.duration_ms, recommendation_count: row.recommendation_count,
      runtime_dir: row.runtime_dir, error: row.error,
    };
  }

  function getRun(jobId) {
    const row = db.prepare("SELECT * FROM runs WHERE job_id = ?").get(String(jobId));
    return row ? publicRun(row) : null;
  }

  function createRun(payload = {}) {
    const jobId = String(payload.jobId || "").trim();
    if (!jobId) throw new Error("运行记录缺少 jobId");
    db.prepare(`INSERT OR REPLACE INTO runs
      (job_id, user_id, kind, platform, playlist_id, playlist_name, track_count, status, started_at, runtime_dir)
      VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)`).run(
      jobId,
      payload.userId == null ? null : Number(payload.userId),
      payload.kind === "recommendation_only" ? "recommendation_only" : "full",
      String(payload.platform || ""),
      String(payload.playlistId || ""),
      String(payload.playlistName || ""),
      Number.isInteger(payload.trackCount) ? payload.trackCount : null,
      payload.startedAt ? String(payload.startedAt) : isoNow(),
      String(payload.runtimeDir || ""),
    );
    return getRun(jobId);
  }

  function updateRun(jobId, patch = {}) {
    const row = db.prepare("SELECT * FROM runs WHERE job_id = ?").get(String(jobId));
    if (!row) return null;
    const fields = [];
    const values = [];
    if (patch.status !== undefined) { fields.push("status = ?"); values.push(String(patch.status)); }
    if (patch.platform !== undefined) { fields.push("platform = ?"); values.push(String(patch.platform)); }
    if (patch.playlistId !== undefined) { fields.push("playlist_id = ?"); values.push(String(patch.playlistId)); }
    if (patch.playlistName !== undefined) { fields.push("playlist_name = ?"); values.push(String(patch.playlistName)); }
    if (patch.trackCount !== undefined) { fields.push("track_count = ?"); values.push(patch.trackCount == null ? null : Number(patch.trackCount)); }
    if (patch.analyzedCount !== undefined) { fields.push("analyzed_count = ?"); values.push(patch.analyzedCount == null ? null : Number(patch.analyzedCount)); }
    if (patch.recommendationCount !== undefined) { fields.push("recommendation_count = ?"); values.push(patch.recommendationCount == null ? null : Number(patch.recommendationCount)); }
    if (patch.error !== undefined) { fields.push("error = ?"); values.push(String(patch.error).slice(0, 2000)); }
    if (!fields.length) return publicRun(row);
    if (["completed", "failed", "cancelled", "superseded", "interrupted"].includes(String(patch.status))) {
      const finishedAt = patch.finishedAt ? String(patch.finishedAt) : isoNow();
      const started = Date.parse(row.started_at || "") || Date.parse(finishedAt);
      fields.push("finished_at = ?"); values.push(finishedAt);
      fields.push("duration_ms = ?"); values.push(Math.max(0, Date.parse(finishedAt) - started));
    }
    values.push(String(jobId));
    db.prepare(`UPDATE runs SET ${fields.join(", ")} WHERE job_id = ?`).run(...values);
    return getRun(jobId);
  }

  function listRuns({ userId = null, status = null, limit = 20, offset = 0 } = {}) {
    const where = [];
    const values = [];
    if (userId != null) { where.push("user_id = ?"); values.push(Number(userId)); }
    if (status) { where.push("status = ?"); values.push(String(status)); }
    const clause = where.length ? `WHERE ${where.join(" AND ")}` : "";
    const size = Math.min(Math.max(Number(limit) || 20, 1), 100);
    const start = Math.max(Number(offset) || 0, 0);
    const rows = db.prepare(`SELECT * FROM runs ${clause} ORDER BY started_at DESC LIMIT ? OFFSET ?`).all(...values, size, start);
    const total = db.prepare(`SELECT COUNT(*) AS n FROM runs ${clause}`).get(...values).n;
    return { total, limit: size, offset: start, runs: rows.map(publicRun) };
  }

  function runStats(userId = null) {
    const clause = userId == null ? "" : "WHERE user_id = ?";
    const values = userId == null ? [] : [Number(userId)];
    const row = db.prepare(`SELECT COUNT(*) AS total,
      SUM(status = 'completed') AS completed,
      SUM(status = 'failed') AS failed,
      SUM(status = 'cancelled') AS cancelled,
      SUM(status = 'superseded') AS superseded,
      SUM(status = 'interrupted') AS interrupted,
      MAX(started_at) AS last_started FROM runs ${clause}`).get(...values);
    return {
      total: row.total || 0,
      completed: row.completed || 0,
      failed: row.failed || 0,
      cancelled: row.cancelled || 0,
      superseded: row.superseded || 0,
      interrupted: row.interrupted || 0,
      last_started: row.last_started || null,
    };
  }

  function backfillRun(payload = {}) {
    // 幂等回填历史任务：已存在时不覆盖，但会补齐缺失的歌单信息。
    const existing = getRun(payload.jobId);
    if (existing) {
      const patch = {};
      if (payload.status && payload.status !== existing.status) {
        patch.status = payload.status;
        patch.finishedAt = payload.finishedAt;
        patch.error = payload.error || "";
      }
      if (!existing.platform && payload.platform) patch.platform = payload.platform;
      if (!existing.playlist_id && payload.playlistId) patch.playlistId = payload.playlistId;
      if (!existing.playlist_name && payload.playlistName) patch.playlistName = payload.playlistName;
      if (existing.track_count == null && payload.trackCount != null) patch.trackCount = payload.trackCount;
      if (existing.analyzed_count == null && payload.analyzedCount != null) patch.analyzedCount = payload.analyzedCount;
      if (existing.recommendation_count == null && payload.recommendationCount != null) patch.recommendationCount = payload.recommendationCount;
      return Object.keys(patch).length ? updateRun(payload.jobId, patch) : existing;
    }
    createRun(payload);
    return updateRun(payload.jobId, {
      status: payload.status || "completed",
      platform: payload.platform,
      playlistId: payload.playlistId,
      playlistName: payload.playlistName,
      analyzedCount: payload.analyzedCount,
      recommendationCount: payload.recommendationCount,
      error: payload.error,
      finishedAt: payload.finishedAt,
    });
  }

  function getPreferences(userId) {
    const row = db.prepare("SELECT data_json FROM preferences WHERE user_id = ?").get(Number(userId));
    try { return row ? JSON.parse(row.data_json) : {}; } catch { return {}; }
  }

  function savePreferences(userId, data) {
    const json = JSON.stringify(data && typeof data === "object" ? data : {});
    const now = isoNow();
    db.prepare(`
      INSERT INTO preferences (user_id, data_json, updated_at) VALUES (?, ?, ?)
      ON CONFLICT(user_id) DO UPDATE SET data_json = excluded.data_json, updated_at = excluded.updated_at
    `).run(Number(userId), json, now);
    return { ...data, updated_at: now };
  }

  function listPlaylists(userId) {
    return db.prepare("SELECT id, source_url, canonical_key, canonical_url, name, platform, config_json, created_at, updated_at FROM playlists WHERE user_id = ? ORDER BY updated_at DESC")
      .all(Number(userId)).map((row) => ({
        id: Number(row.id), source_url: row.source_url,
        canonical_key: row.canonical_key || "", canonical_url: row.canonical_url || row.source_url,
        name: row.name, platform: row.platform,
        config: (() => { try { return JSON.parse(row.config_json); } catch { return {}; } })(),
        created_at: row.created_at, updated_at: row.updated_at,
      }));
  }

  function upsertPlaylist(userId, data = {}) {
    const sourceUrl = String(data.source_url || "").trim();
    if (!sourceUrl) throw new Error("歌单链接不能为空");
    const canonicalUrl = String(data.canonical_url || sourceUrl).trim() || sourceUrl;
    const canonicalKey = String(data.canonical_key || "").trim()
      || `${String(data.platform || "url").trim() || "url"}:url:${crypto.createHash("sha256").update(canonicalUrl).digest("hex").slice(0, 24)}`;
    const now = isoNow();
    const config = data.config && typeof data.config === "object" ? data.config : {};
    const existing = db.prepare(`SELECT * FROM playlists WHERE user_id = ?
      AND (canonical_key = ? OR source_url = ?) ORDER BY id LIMIT 1`).get(Number(userId), canonicalKey, sourceUrl);
    let id;
    if (existing) {
      const name = String(data.name || existing.name || "").slice(0, 200);
      const platform = String(data.platform || existing.platform || "").slice(0, 50);
      db.prepare(`UPDATE playlists SET source_url = ?, canonical_key = ?, canonical_url = ?,
        name = ?, platform = ?, config_json = ?, updated_at = ? WHERE id = ?`)
        .run(canonicalUrl, canonicalKey, canonicalUrl, name, platform, JSON.stringify(config), now, existing.id);
      id = Number(existing.id);
    } else {
      const result = db.prepare(`INSERT INTO playlists
        (user_id, source_url, canonical_key, canonical_url, name, platform, config_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`)
        .run(Number(userId), canonicalUrl, canonicalKey, canonicalUrl,
          String(data.name || "").slice(0, 200), String(data.platform || "").slice(0, 50), JSON.stringify(config), now, now);
      id = Number(result.lastInsertRowid);
    }
    return listPlaylists(userId).find((item) => item.id === id) || null;
  }

  async function reconcilePlaylists(normalize) {
    if (typeof normalize !== "function") throw new Error("缺少歌单链接规范化器");
    const rows = db.prepare("SELECT * FROM playlists ORDER BY user_id, created_at, id").all();
    let normalized = 0;
    let merged = 0;
    for (const row of rows) {
      // 前一轮可能已将本行作为重复项删除。
      if (!db.prepare("SELECT 1 FROM playlists WHERE id = ?").get(row.id)) continue;
      if (row.canonical_key && row.canonical_url) continue;
      let identity;
      try { identity = await normalize(row.source_url); } catch { continue; }
      if (!identity || !identity.canonical_key) continue;
      const duplicate = db.prepare(`SELECT * FROM playlists WHERE user_id = ? AND id <> ?
        AND canonical_key = ? ORDER BY created_at, id LIMIT 1`)
        .get(row.user_id, row.id, identity.canonical_key);
      if (!duplicate) {
        db.prepare(`UPDATE playlists SET source_url = ?, canonical_key = ?, canonical_url = ?,
          platform = CASE WHEN ? <> '' THEN ? ELSE platform END WHERE id = ?`)
          .run(identity.canonical_url || identity.source_url, identity.canonical_key,
            identity.canonical_url || identity.source_url, identity.platform || "", identity.platform || "", row.id);
        normalized += 1;
        continue;
      }
      const keeper = Number(duplicate.id) < Number(row.id) ? duplicate : row;
      const other = keeper.id === row.id ? duplicate : row;
      const latest = Date.parse(row.updated_at || "") >= Date.parse(duplicate.updated_at || "") ? row : duplicate;
      const createdAt = [row.created_at, duplicate.created_at].filter(Boolean).sort()[0] || isoNow();
      const updatedAt = [row.updated_at, duplicate.updated_at].filter(Boolean).sort().at(-1) || isoNow();
      db.exec("BEGIN IMMEDIATE");
      try {
        db.prepare("DELETE FROM playlists WHERE id = ?").run(other.id);
        db.prepare(`UPDATE playlists SET source_url = ?, canonical_key = ?, canonical_url = ?,
          name = ?, platform = ?, config_json = ?, created_at = ?, updated_at = ? WHERE id = ?`)
          .run(identity.canonical_url || identity.source_url, identity.canonical_key,
            identity.canonical_url || identity.source_url, latest.name || keeper.name || "",
            identity.platform || latest.platform || keeper.platform || "", latest.config_json || "{}",
            createdAt, updatedAt, keeper.id);
        db.exec("COMMIT");
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
      merged += 1;
      normalized += 1;
    }
    return { normalized, merged, total: db.prepare("SELECT COUNT(*) AS count FROM playlists").get().count };
  }

  function writeAudit(userId, action, detail = {}) {
    db.prepare("INSERT INTO audit_log (user_id, action, detail_json, created_at) VALUES (?, ?, ?, ?)")
      .run(userId == null ? null : Number(userId), String(action), JSON.stringify(detail || {}), isoNow());
  }

  function maintenance({ auditRetentionDays = 180, revokedSessionRetentionDays = 30 } = {}) {
    const now = Date.now();
    const sessionCutoff = new Date(now - Math.max(1, Number(revokedSessionRetentionDays) || 30) * 86400000).toISOString();
    const auditCutoff = new Date(now - Math.max(1, Number(auditRetentionDays) || 180) * 86400000).toISOString();
    const sessions = db.prepare(`DELETE FROM sessions
      WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)`).run(isoNow(), sessionCutoff).changes;
    const audits = db.prepare("DELETE FROM audit_log WHERE created_at < ?").run(auditCutoff).changes;
    db.exec("PRAGMA wal_checkpoint(PASSIVE); PRAGMA optimize;");
    return { sessions: Number(sessions), audits: Number(audits) };
  }

  async function authenticateAsync(usernameValue, passwordValue, requiredRole = "user") {
    const row = db.prepare("SELECT * FROM users WHERE username = ? COLLATE NOCASE").get(String(usernameValue || "").trim());
    if (!row || row.status !== "enabled" || !(await verifyPasswordAsync(passwordValue, row.password_hash))) throw new Error("用户名或密码错误");
    const current = getUserById(row.id);
    if (!current || current.status !== "enabled" || current.password_hash !== row.password_hash) throw new Error("用户名或密码错误");
    if (requiredRole === "admin" && current.role !== "admin") throw new Error("无管理员权限");
    const now = isoNow();
    db.prepare("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?").run(now, now, row.id);
    return publicUser({ ...current, last_login_at: now });
  }

  return {
    dbPath: resolvedPath,
    normalizeUsername,
    normalizePassword,
    createUser,
    async createUserAsync(username, password, role = "user") {
      normalizeUsername(username);
      return createUser(username, password, role, await hashPasswordAsync(password));
    },
    authenticateAsync,
    async updateUserAsync(id, patch = {}) {
      return updateUser(id, patch, patch.password === undefined ? null : await hashPasswordAsync(patch.password));
    },
    async verifyUserPasswordAsync(id, password) {
      const user = getUserById(id);
      return Boolean(user && await verifyPasswordAsync(password, user.password_hash));
    },
    ensureBootstrapAdmin,
    verifyUserPassword,
    authenticate,
    createSession,
    getSessionUser,
    revokeSession,
    listUsers,
    updateUser,
    deleteUser,
    userResetSnapshot,
    resetUserData,
    getRun,
    createRun,
    updateRun,
    listRuns,
    runStats,
    backfillRun,
    getPreferences,
    savePreferences,
    listPlaylists,
    upsertPlaylist,
    reconcilePlaylists,
    writeAudit,
    maintenance,
    close() { db.close(); },
  };
}

module.exports = { createAuthStore, normalizeUsername, normalizePassword, hashPassword, verifyPassword };
