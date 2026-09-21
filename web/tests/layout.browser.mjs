import test from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

/** 读取页面高度指标。 */
async function metrics(page) {
  return page.evaluate(() => {
    const footer = document.querySelector("footer");
    return {
      scrollHeight: document.documentElement.scrollHeight,
      innerHeight: window.innerHeight,
      footerBottom: footer ? Math.round(footer.getBoundingClientRect().bottom + window.scrollY) : null,
    };
  });
}

/** 页面必须填满视口，footer 之后不允许出现空白。 */
function assertFilled(m, label) {
  assert.ok(m.footerBottom !== null, `${label}：缺少 footer`);
  assert.ok(m.scrollHeight >= m.innerHeight, `${label}：页面高度 ${m.scrollHeight} 应至少填满视口 ${m.innerHeight}`);
  assert.ok(Math.abs(m.footerBottom - m.scrollHeight) <= 1, `${label}：footer 底边 ${m.footerBottom} 与页面底部 ${m.scrollHeight} 之间有空白`);
}

const CASES = [
  { name: "未登录入口页桌面", path: "/", width: 1581, height: 1307 },
  { name: "未登录入口页移动", path: "/", width: 390, height: 844 },
  { name: "管理员登录屏桌面", path: "/admin", width: 1581, height: 1307 },
  { name: "管理员登录屏移动", path: "/admin", width: 390, height: 844 },
];

test("登录界面在桌面与移动视口下填满页面底部", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "layout-gate", authRequired: true });
  t.after(() => server.stop());
  const browser = await chromium.launch();
  t.after(() => browser.close());
  for (const c of CASES) {
    const page = await browser.newPage({ viewport: { width: c.width, height: c.height } });
    await page.goto(server.baseUrl + c.path, { waitUntil: "networkidle" });
    await page.waitForTimeout(300);
    assertFilled(await metrics(page), c.name);
    await page.close();
  }
});

test("管理控制台在桌面与移动视口下填满页面底部", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "layout-admin", authRequired: true });
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  store.ensureBootstrapAdmin("layout-admin", "layout-password-2026");
  store.close();
  const browser = await chromium.launch();
  t.after(() => browser.close());
  for (const c of [{ width: 1581, height: 1307 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport: c });
    await page.goto(`${server.baseUrl}/admin`);
    await page.locator("#admin-user").fill("layout-admin");
    await page.locator("#admin-pass").fill("layout-password-2026");
    await page.locator("form[data-login] button[type=submit]").click();
    await page.getByText("后台控制台").waitFor();
    assertFilled(await metrics(page), `控制台 ${c.width}x${c.height}`);
    await page.close();
  }
});
