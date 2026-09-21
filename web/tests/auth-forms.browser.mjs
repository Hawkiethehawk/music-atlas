import test from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createIsolatedServer } from "./helpers.mjs";

test("注册需二次确认密码，登录支持记住账号", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "auth-forms", authRequired: true });
  t.after(() => server.stop());
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !/status of (401|503)/.test(m.text())) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(server.baseUrl + "/");
  await page.waitForFunction(() => document.body.classList.contains("atlas-ready"));

  await page.locator("[data-auth-open-mode='register']").click();
  await page.getByRole("heading", { name: "创建账号" }).waitFor();
  assert.equal(await page.locator("#auth-password2").count(), 1, "注册应有确认密码字段");

  await page.locator("#auth-username").fill("CaseUser");
  await page.locator("#auth-password").fill("first-password-2026");
  await page.locator("#auth-password2").fill("second-password-2026");
  await page.locator("form[data-auth-form] button[type=submit]").click();
  await page.waitForTimeout(300);
  assert.match(await page.locator("[data-auth-message]").textContent(), /两次输入的密码不一致/, "两次密码不一致应就地提示");
  assert.equal(await page.locator("#auth-password2").count(), 1, "不一致时不应提交");

  await page.locator("#auth-password2").fill("first-password-2026");
  await page.locator("form[data-auth-form] button[type=submit]").click();
  await page.waitForFunction(() => document.querySelector(".auth-user .auth-name")?.textContent === "CaseUser");

  assert.equal(await page.evaluate(() => localStorage.getItem("atlas_remembered_username")), "CaseUser", "注册成功后应记住账号");

  await page.locator("[data-auth-logout]").click();
  await page.waitForTimeout(400);
  await page.locator("[data-auth-open]").first().click();
  await page.locator("#auth-username").waitFor();
  assert.equal(await page.locator("#auth-username").inputValue(), "CaseUser", "登录弹窗应预填记住的账号");
  assert.equal(await page.locator("[data-auth-remember]").isChecked(), true);

  await page.locator("[data-auth-remember]").uncheck();
  await page.locator("#auth-password").fill("first-password-2026");
  await page.locator("form[data-auth-form] button[type=submit]").click();
  await page.waitForFunction(() => document.querySelector(".auth-user .auth-name")?.textContent === "CaseUser");
  assert.equal(await page.evaluate(() => localStorage.getItem("atlas_remembered_username")), null, "取消勾选后应清除记住的账号");

  await page.locator("[data-auth-logout]").click();
  await page.waitForTimeout(400);
  await page.locator("[data-auth-open]").first().click();
  await page.waitForTimeout(300);
  assert.equal(await page.locator("#auth-username").inputValue(), "", "未记住时不应预填账号");
  assert.deepEqual(errors, []);
});
