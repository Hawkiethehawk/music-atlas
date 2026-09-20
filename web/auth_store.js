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
    CREATE TABLE IF NOT EXISTS preferences (
      user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      data_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS playlists (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      source_url TEXT NOT NULL,
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
  `);

  function getUserById(id) {
    return db.prepare("SELECT * FROM users WHERE id = ?").get(Number(id)) || null;
  }

  function createUser(usernameValue, passwordValue, role = "user") {
    const username = normalizeUsername(usernameValue);
    const password = normalizePassword(passwordValue);
    if (!["user", "admin"].includes(role)) throw new Error("用户角色无效");
    const now = isoNow();
    try {
      const result = db.prepare(
        "INSERT INTO users (username, password_hash, role, status, created_at, updated_at) VALUES (?, ?, ?, 'enabled', ?, ?)",
      ).run(username, hashPassword(password), role, now, now);
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

  function createSession(userId, kind = "user") {
    if (!["user", "admin"].includes(kind)) throw new Error("会话类型无效");
    const user = getUserById(userId);
    if (!user || user.status !== "enabled") throw new Error("用户不可用");
    if (kind === "admin" && user.role !== "admin") throw new Error("无管理员权限");
    const token = crypto.randomBytes(32).toString("base64url");
    const now = new Date();
    const expires = new Date(now.getTime() + (kind === "admin" ? ADMIN_SESSION_TTL_SECONDS : SESSION_TTL_SECONDS) * 1000);
    db.prepare("INSERT INTO sessions (token_hash, user_id, kind, created_at, expires_at) VALUES (?, ?, ?, ?, ?)")
      .run(tokenHash(token), Number(userId), kind, now.toISOString(), expires.toISOString());
    return { token, user: publicUser(user), expires_at: expires.toISOString() };
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

  function updateUser(id, patch = {}) {
    const user = getUserById(id);
    if (!user) throw new Error("用户不存在");
    const fields = [];
    const values = [];
    if (patch.status !== undefined) {
      if (!["enabled", "disabled"].includes(patch.status)) throw new Error("用户状态无效");
      fields.push("status = ?"); values.push(patch.status);
    }
    if (patch.role !== undefined) {
      if (!["user", "admin"].includes(patch.role)) throw new Error("用户角色无效");
      fields.push("role = ?"); values.push(patch.role);
    }
    if (patch.password !== undefined) {
      fields.push("password_hash = ?"); values.push(hashPassword(normalizePassword(patch.password)));
    }
    if (!fields.length) return publicUser(user);
    fields.push("updated_at = ?"); values.push(isoNow(), Number(id));
    db.prepare(`UPDATE users SET ${fields.join(", ")} WHERE id = ?`).run(...values);
    if (patch.status === "disabled" || patch.role === "user") {
      db.prepare("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND kind = 'admin' AND revoked_at IS NULL").run(isoNow(), Number(id));
    }
    return publicUser(getUserById(id));
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
    return db.prepare("SELECT id, source_url, name, platform, config_json, created_at, updated_at FROM playlists WHERE user_id = ? ORDER BY updated_at DESC")
      .all(Number(userId)).map((row) => ({
        id: Number(row.id), source_url: row.source_url, name: row.name, platform: row.platform,
        config: (() => { try { return JSON.parse(row.config_json); } catch { return {}; } })(),
        created_at: row.created_at, updated_at: row.updated_at,
      }));
  }

  function upsertPlaylist(userId, data = {}) {
    const sourceUrl = String(data.source_url || "").trim();
    if (!sourceUrl) throw new Error("歌单链接不能为空");
    const now = isoNow();
    const config = data.config && typeof data.config === "object" ? data.config : {};
    db.prepare(`
      INSERT INTO playlists (user_id, source_url, name, platform, config_json, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(user_id, source_url) DO UPDATE SET name = excluded.name, platform = excluded.platform,
        config_json = excluded.config_json, updated_at = excluded.updated_at
    `).run(Number(userId), sourceUrl, String(data.name || "").slice(0, 200), String(data.platform || "").slice(0, 50), JSON.stringify(config), now, now);
    return listPlaylists(userId).find((item) => item.source_url === sourceUrl) || null;
  }

  function writeAudit(userId, action, detail = {}) {
    db.prepare("INSERT INTO audit_log (user_id, action, detail_json, created_at) VALUES (?, ?, ?, ?)")
      .run(userId == null ? null : Number(userId), String(action), JSON.stringify(detail || {}), isoNow());
  }

  return {
    dbPath: resolvedPath,
    normalizeUsername,
    normalizePassword,
    createUser,
    ensureBootstrapAdmin,
    authenticate,
    createSession,
    getSessionUser,
    revokeSession,
    listUsers,
    updateUser,
    getPreferences,
    savePreferences,
    listPlaylists,
    upsertPlaylist,
    writeAudit,
    close() { db.close(); },
  };
}

module.exports = { createAuthStore, normalizeUsername, normalizePassword, hashPassword, verifyPassword };
