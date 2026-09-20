import test from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createIsolatedServer } from "./helpers.mjs";

test("未登录时显示默认入口页，不展示上一份 Atlas", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "guest-browser", authRequired: true });
  t.after(() => server.stop());
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage();
  const errors = [];
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.route("**/api/atlas", (route) => route.fulfill({
    json: { ok: true, issue: { title: "测试 Atlas", lede: "测试导语" }, status: { publication: "draft" }, recommendations: [] },
  }));

  await page.goto(server.baseUrl + "/#/atlas");
  await page.waitForFunction(() => document.body.classList.contains("atlas-ready"));
  assert.match(await page.locator("body").textContent(), /把你的歌单/);
  assert.match(await page.locator("body").textContent(), /创建账号/);
  assert.equal(await page.locator(".trow").count(), 0);
  assert.equal(await page.locator("nav a").count(), 1);
  await page.locator("[data-auth-open-mode='register']").click();
  await page.getByRole("heading", { name: "创建账号" }).waitFor();
  assert.deepEqual(errors, []);
});
