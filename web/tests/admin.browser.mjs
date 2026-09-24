import test from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

test("管理员页面沿用主页面视觉并完成登录后加载控制台", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "admin-browser", authRequired: true });
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  store.ensureBootstrapAdmin("browser-admin", "admin-password-2026");
  store.close();
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1714, height: 1100 } });
  const errors = [];
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(`${server.baseUrl}/admin`);
  assert.match(await page.locator(".remember").textContent(), /保持登录 30 天/);
  assert.equal(await page.locator("[data-admin-remember]").isChecked(), true);
  await page.locator("#admin-user").fill("browser-admin");
  await page.locator("#admin-pass").fill("admin-password-2026");
  await page.getByRole("button", { name: "进入后台" }).click();
  await page.getByText("后台控制台").waitFor();
  assert.equal(await page.locator("header .logo").textContent(), "MUSIC ATLAS");
  assert.equal(await page.locator("[data-panel]").count(), 3);
  assert.equal(await page.locator("[data-panel]:not([hidden])").count(), 1);
  assert.match(await page.locator("body").textContent(), /用户管理/);
  assert.match(await page.locator("body").textContent(), /系统设置/);
  assert.match(await page.locator("body").textContent(), /身份来源与配比硬校验|不执行单列本地复核或独立审计/,
    "管理员设置应说明首次运行的硬约束与不再单列独立审计");
  const desktopLayout = await page.evaluate(() => {
    const nav = document.querySelector(".side-nav").getBoundingClientRect();
    const foot = document.querySelector(".side-foot").getBoundingClientRect();
    const table = document.querySelector("#users .user-table").getBoundingClientRect();
    const panel = document.querySelector("[data-panel=users]").getBoundingClientRect();
    return { navWidth: nav.width, footWidth: foot.width, tableRight: table.right, panelRight: panel.right };
  });
  assert.ok(Math.abs(desktopLayout.navWidth - desktopLayout.footWidth) < 2);
  assert.ok(desktopLayout.tableRight <= desktopLayout.panelRight);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  const session = (await page.context().cookies(server.baseUrl)).find((item) => item.name === "atlas_admin_session");
  assert.ok(session && session.expires - Date.now() / 1000 > 29 * 24 * 60 * 60);
  await page.reload();
  await page.getByText("后台控制台").waitFor();
  await page.locator("[data-logout]").click();
  await page.getByRole("heading", { name: "管理员登录" }).waitFor();
  await page.reload();
  assert.equal(await page.getByRole("heading", { name: "管理员登录" }).isVisible(), true);
  assert.deepEqual(errors, []);
});

test("推荐去重按账户与平台筛选，确认框说明单维范围且取消不清除", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "admin-history-browser", authRequired: true });
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  const first = store.createUser("FilterFirst", "user-password-2026", "user");
  const second = store.createUser("FilterSecond", "user-password-2026", "user");
  store.createUser("filter-admin", "admin-password-2026", "admin");
  store.close();
  const historyDir = path.join(path.dirname(server.authDbPath), "recommendation-history");
  mkdirSync(historyDir, { recursive: true });
  for (const [name, userId, kind] of [
    ["first-net.json", first.id, "netease_public"],
    ["first-qq.json", first.id, "qq_public"],
    ["second-net.json", second.id, "netease_public"],
  ]) writeFileSync(path.join(historyDir, name), JSON.stringify({
    schema_version: "1.0", user_id: String(userId), playlist: { kind, playlist_id: name },
    retention_days: 7, entries: [{ generated_at: new Date().toISOString(), canonical_track_ids: [name] }],
  }));
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1714, height: 1100 } });
  await page.goto(`${server.baseUrl}/admin`);
  await page.locator("#admin-user").fill("filter-admin");
  await page.locator("#admin-pass").fill("admin-password-2026");
  await page.getByRole("button", { name: "进入后台" }).click();
  await page.locator("[data-pane=history]").click();
  assert.match(await page.locator(".history-summary").textContent(), /3 \/ 3/);
  await page.locator("[data-history-filter=account] .select-btn").click();
  await page.locator("[data-history-filter=account] [data-select-value]").filter({ hasText: "FilterFirst" }).click();
  assert.match(await page.locator(".history-summary").textContent(), /2 \/ 3/);
  await page.locator("[data-history-filter=platform] .select-btn").click();
  await page.locator("[data-history-filter=platform] [data-select-value]").filter({ hasText: "网易云音乐" }).click();
  assert.match(await page.locator(".history-summary").textContent(), /1 \/ 3/);
  await page.screenshot({ path: path.join(path.dirname(server.authDbPath), "history-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: path.join(path.dirname(server.authDbPath), "history-mobile.png"), fullPage: true });
  await page.setViewportSize({ width: 1714, height: 1100 });

  await page.locator("[data-history-bulk=account]").click();
  assert.match(await page.locator("[role=dialog]").textContent(), /FilterFirst.*所有平台.*2 份缓存.*平台筛选不会限制/);
  await page.locator("[data-confirm-cancel]").click();
  assert.match(await page.locator(".history-summary").textContent(), /1 \/ 3/);
  await page.locator("[data-history-bulk=platform]").click();
  assert.match(await page.locator("[role=dialog]").textContent(), /网易云音乐.*所有账户.*2 份缓存.*账户筛选不会限制/);
  await page.locator("[data-confirm-cancel]").click();

  await page.locator("[data-history-bulk=account]").click();
  await page.locator("[data-confirm-ok]").click();
  await page.getByText("当前显示 1 / 1 份缓存", { exact: false }).waitFor();
  assert.match(await page.locator("#history").textContent(), /FilterSecond/);
});

test("管理员初始化账号只需确认，取消不修改，成功后列表归零", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "admin-initialize-browser", authRequired: true });
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  const target = store.createUser("BrowserTarget", "user-password-2026", "user");
  store.createUser("browser-reset-admin", "admin-password-2026", "admin");
  store.savePreferences(target.id, { mood: "quiet" });
  store.upsertPlaylist(target.id, { source_url: "https://music.example/browser", name: "待清空歌单" });
  store.createRun({ jobId: "browser-target-run", userId: target.id });
  store.updateRun("browser-target-run", { status: "completed" });
  store.close();
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1714, height: 1100 } });
  await page.goto(`${server.baseUrl}/admin`);
  await page.locator("#admin-user").fill("browser-reset-admin");
  await page.locator("#admin-pass").fill("admin-password-2026");
  await page.getByRole("button", { name: "进入后台" }).click();
  await page.locator(`[data-user-initialize="${target.id}"]`).click();
  const dialog = page.locator("[role=dialog]");
  assert.match(await dialog.textContent(), /保留账号、当前密码、角色、状态、注册时间与管理员审计记录/);
  assert.match(await dialog.textContent(), /1 个歌单.*1 项偏好.*1 条运行记录/);
  assert.match(await dialog.textContent(), /账号：BrowserTarget/);
  assert.doesNotMatch(await dialog.textContent(), /BROWSERTARGET|输入用户名/);
  assert.equal(await dialog.locator("[data-initialize-name]").count(), 0);
  assert.equal(await dialog.locator("[data-initialize-confirm]").isEnabled(), true);
  assert.equal(await dialog.locator("[data-initialize-confirm]").getAttribute("data-username"), "BrowserTarget");
  await dialog.getByRole("button", { name: "取消" }).click();
  const before = createAuthStore(server.authDbPath);
  assert.equal(before.listPlaylists(target.id).length, 1);
  before.close();
  await page.locator(`[data-user-initialize="${target.id}"]`).click();
  assert.equal(await dialog.locator("[data-initialize-confirm]").isEnabled(), true);
  await dialog.locator("[data-initialize-confirm]").click();
  await page.getByText("账号已初始化", { exact: true }).waitFor();
  const after = createAuthStore(server.authDbPath);
  assert.equal(after.listPlaylists(target.id).length, 0);
  assert.equal(after.listRuns({ userId: target.id }).total, 0);
  after.close();
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
});
