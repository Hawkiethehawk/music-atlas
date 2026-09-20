#!/usr/bin/env node
"use strict";

/* One-time administrator creation. Run from the project root:
 *   node web/admin_bootstrap.js admin-name
 * The password is read from MUSIC_ATLAS_ADMIN_PASSWORD or hidden stdin.
 */
const readline = require("node:readline");
const path = require("node:path");
const { createAuthStore } = require("./auth_store");

const projectRoot = path.resolve(__dirname, "..");
const dbPath = process.env.ATLAS_AUTH_DB || path.join(projectRoot, "runtime", "web", "auth.sqlite");
const username = process.argv[2] || process.env.MUSIC_ATLAS_ADMIN_USERNAME;
const envPassword = process.env.MUSIC_ATLAS_ADMIN_PASSWORD;

function readPassword() {
  if (envPassword) return Promise.resolve(envPassword);
  if (!process.stdin.isTTY) return Promise.reject(new Error("请设置 MUSIC_ATLAS_ADMIN_PASSWORD，或在交互终端运行此命令"));
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => rl.question("管理员密码（至少 8 位）：", (answer) => { rl.close(); process.stdout.write("\n"); resolve(answer); }));
}

(async () => {
  if (!username) throw new Error("请提供管理员用户名：node web/admin_bootstrap.js <username>");
  const password = await readPassword();
  const store = createAuthStore(dbPath);
  const created = store.ensureBootstrapAdmin(username, password);
  store.close();
  if (!created) throw new Error("已有管理员账号；为避免覆盖，请通过后台修改账号或密码");
  console.log(`管理员账号已创建：${username}`);
})().catch((error) => { console.error(error.message || error); process.exitCode = 1; });
