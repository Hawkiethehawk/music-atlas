import test from "node:test";
import assert from "node:assert/strict";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

function cookie(response, name) {
  const raw = response.headers.get("set-cookie") || "";
  const match = raw.match(new RegExp(`${name}=([^;]+)`));
  return match ? `${name}=${match[1]}` : "";
}

test("管理员账号：新增、改名、改密、停用、硬删除与保护规则", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "admin-users", authRequired: true });
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  store.ensureBootstrapAdmin("RootAdmin", "root-password-2026");
  store.close();
  const { baseUrl } = server;

  const login = await fetch(`${baseUrl}/api/admin/login`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ username: "rootadmin", password: "root-password-2026" }),
  });
  assert.equal(login.status, 200, "管理员登录应不区分大小写");
  const session = cookie(login, "atlas_admin_session");
  assert.ok(session, "应下发管理员会话");
  const auth = { cookie: session, "content-type": "application/json" };

  const created = await fetch(`${baseUrl}/api/admin/users`, { method: "POST", headers: auth, body: JSON.stringify({ username: "NewUser", password: "new-password-2026", role: "user" }) });
  assert.equal(created.status, 201);
  const createdUser = (await created.json()).user;
  assert.equal(createdUser.username, "NewUser", "新增账号应保留书写大小写");

  const renamed = await fetch(`${baseUrl}/api/admin/users/${createdUser.id}`, { method: "PATCH", headers: auth, body: JSON.stringify({ username: "RenamedUser", password: "changed-password-2026" }) });
  assert.equal(renamed.status, 200);
  assert.equal((await renamed.json()).user.username, "RenamedUser");

  const userLogin = await fetch(`${baseUrl}/api/auth/login`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ username: "renameduser", password: "changed-password-2026" }) });
  assert.equal(userLogin.status, 200, "改密后新密码应可登录");

  const runs = await fetch(`${baseUrl}/api/admin/users/${createdUser.id}/runs`, { headers: { cookie: session } });
  assert.equal(runs.status, 200);
  assert.deepEqual((await runs.json()).runs, []);

  const disabled = await fetch(`${baseUrl}/api/admin/users/${createdUser.id}`, { method: "PATCH", headers: auth, body: JSON.stringify({ status: "disabled" }) });
  assert.equal(disabled.status, 200);
  assert.equal((await disabled.json()).user.status, "disabled");
  const blocked = await fetch(`${baseUrl}/api/auth/login`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ username: "renameduser", password: "changed-password-2026" }) });
  assert.equal(blocked.status, 401, "停用账号不允许登录");

  const me = await fetch(`${baseUrl}/api/admin/me`, { headers: { cookie: session } });
  const meId = (await me.json()).user.id;
  const selfDelete = await fetch(`${baseUrl}/api/admin/users/${meId}`, { method: "DELETE", headers: auth });
  assert.equal(selfDelete.status, 400, "不能删除当前登录账号");
  const selfDemote = await fetch(`${baseUrl}/api/admin/users/${meId}`, { method: "PATCH", headers: auth, body: JSON.stringify({ role: "user" }) });
  assert.equal(selfDemote.status, 400, "不能降级当前登录账号");

  const removed = await fetch(`${baseUrl}/api/admin/users/${createdUser.id}`, { method: "DELETE", headers: auth });
  assert.equal(removed.status, 200);
  const list = await fetch(`${baseUrl}/api/admin/users`, { headers: { cookie: session } });
  const users = (await list.json()).users;
  assert.ok(!users.some((user) => user.id === createdUser.id), "硬删除后用户不应存在");

  const effort = await fetch(`${baseUrl}/api/admin/settings`, { method: "PUT", headers: auth, body: JSON.stringify({ settings: { runtime: { codex_reasoning_effort: "max", openai_compat: { disable_thinking: false } } } }) });
  assert.equal(effort.status, 200);
  const effortSettings = (await effort.json()).settings;
  assert.equal(effortSettings.runtime.codex_reasoning_effort, "max");
  assert.equal(effortSettings.runtime.openai_compat.disable_thinking, false);
  const badEffort = await fetch(`${baseUrl}/api/admin/settings`, { method: "PUT", headers: auth, body: JSON.stringify({ settings: { runtime: { codex_reasoning_effort: "ultra" } } }) });
  assert.equal(badEffort.status, 400, "非法思考强度应被拒绝");
});
