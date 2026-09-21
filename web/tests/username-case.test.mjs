import test from "node:test";
import assert from "node:assert/strict";
import { createIsolatedServer } from "./helpers.mjs";

async function post(baseUrl, path, body) {
  const response = await fetch(baseUrl + path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: response.status, payload: await response.json() };
}

test("用户名：注册保留原大小写，登录不区分大小写，显示与注册一致", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "username-case", authRequired: true });
  t.after(() => server.stop());
  const { baseUrl } = server;

  const registered = await post(baseUrl, "/api/auth/register", { username: "Hawkie", password: "correct-horse-2026" });
  assert.equal(registered.status, 201);
  assert.equal(registered.payload.user.username, "Hawkie", "注册应保留输入的大小写");

  for (const attempt of ["hawkie", "HAWKIE", "Hawkie"]) {
    const login = await post(baseUrl, "/api/auth/login", { username: attempt, password: "correct-horse-2026" });
    assert.equal(login.status, 200, `登录应不区分大小写：${attempt}`);
    assert.equal(login.payload.user.username, "Hawkie", `登录后返回的用户名应与注册一致：${attempt}`);
  }

  const wrong = await post(baseUrl, "/api/auth/login", { username: "hawkie", password: "wrong-password-2026" });
  assert.equal(wrong.status, 401);

  const duplicate = await post(baseUrl, "/api/auth/register", { username: "HAWKIE", password: "correct-horse-2026" });
  assert.equal(duplicate.status, 400, "大小写不同的同名账号应视为重复");
});
