import test from "node:test";
import assert from "node:assert/strict";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

const INVALID_URL = "https://163cn.tv/auth-e2e-invalid";

function cookie(response, name) {
  const raw = response.headers.get("set-cookie") || "";
  const match = raw.match(new RegExp(`${name}=([^;]+)`));
  return match ? `${name}=${match[1]}` : "";
}

async function register(baseUrl, username) {
  const response = await fetch(`${baseUrl}/api/auth/register`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ username, password: "correct-horse-2026" }),
  });
  assert.equal(response.status, 201);
  const payload = await response.json();
  return { payload, session: cookie(response, "atlas_session") };
}

test("认证、管理员隔离与用户任务隔离", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "auth", authRequired: true });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const anonymous = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_url: INVALID_URL }),
  });
  assert.equal(anonymous.status, 401);

  const first = await register(baseUrl, "atlas-user");
  assert.equal(first.payload.user.role, "user");
  assert.ok(first.session);
  const me = await fetch(`${baseUrl}/api/auth/me`, { headers: { cookie: first.session } });
  assert.equal((await me.json()).user.username, "atlas-user");

  const created = await fetch(`${baseUrl}/api/jobs`, {
    method: "POST", headers: { "content-type": "application/json", cookie: first.session },
    body: JSON.stringify({ source_url: INVALID_URL }),
  });
  assert.equal(created.status, 202);
  const job = (await created.json()).job;
  assert.equal(Object.prototype.hasOwnProperty.call(job, "runtime_dir"), false);

  const second = await register(baseUrl, "atlas-user-two");
  const forbidden = await fetch(`${baseUrl}/api/jobs/${encodeURIComponent(job.id)}`, { headers: { cookie: second.session } });
  assert.equal(forbidden.status, 404);

  const store = createAuthStore(server.authDbPath);
  store.ensureBootstrapAdmin("atlas-admin", "admin-password-2026");
  store.close();
  const adminLogin = await fetch(`${baseUrl}/api/admin/login`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ username: "atlas-admin", password: "admin-password-2026" }),
  });
  assert.equal(adminLogin.status, 200);
  const adminSession = cookie(adminLogin, "atlas_admin_session");
  assert.ok(adminSession);
  const users = await fetch(`${baseUrl}/api/admin/users`, { headers: { cookie: adminSession } });
  assert.equal(users.status, 200);
  assert.equal((await users.json()).users.length, 3);
  const userCannotAdmin = await fetch(`${baseUrl}/api/admin/users`, { headers: { cookie: first.session } });
  assert.equal(userCannotAdmin.status, 404);
  const settings = await fetch(`${baseUrl}/api/admin/settings`, { headers: { cookie: adminSession } });
  assert.equal(settings.status, 200);
});

test("管理员会话不能替代普通用户会话运行任务", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "admin-cookie", authRequired: true });
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  store.ensureBootstrapAdmin("only-admin", "admin-password-2026");
  store.close();
  const login = await fetch(`${server.baseUrl}/api/admin/login`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ username: "only-admin", password: "admin-password-2026" }),
  });
  const adminSession = cookie(login, "atlas_admin_session");
  const response = await fetch(`${server.baseUrl}/api/jobs`, {
    method: "POST", headers: { "content-type": "application/json", cookie: adminSession },
    body: JSON.stringify({ source_url: INVALID_URL }),
  });
  assert.equal(response.status, 401);
});
