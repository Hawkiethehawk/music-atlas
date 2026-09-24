import test from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { createIsolatedServer } from "./helpers.mjs";

async function register(page, username) {
  await page.locator("[data-auth-open-mode='register']").click();
  await page.locator("#auth-username").fill(username);
  await page.locator("#auth-password").fill("account-isolation-2026");
  await page.locator("#auth-password2").fill("account-isolation-2026");
  await page.locator("form[data-auth-form] button[type=submit]").click();
  await page.waitForFunction((name) => document.querySelector(".auth-user .auth-name")?.textContent === name, username);
}

test("账号切换隔离歌单、最近任务和页面运行态", { concurrency: false }, async (t) => {
  const server = await createIsolatedServer({ label: "account-isolation", authRequired: true });
  t.after(() => server.stop());
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage();
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !/status of (401|404|503)/.test(message.text())) errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.addInitScript(() => {
    localStorage.setItem("music-atlas.playlists.v1", JSON.stringify([{ url: "https:" + "//music.163.com/playlist?id=999999" }]));
    localStorage.setItem("music-atlas.workflow.last-job.v1", "legacy-job");
  });
  await page.goto(server.baseUrl + "/");
  await page.waitForFunction(() => document.body.classList.contains("atlas-ready"));
  assert.deepEqual(await page.evaluate(() => ({
    playlists: localStorage.getItem("music-atlas.playlists.v1"),
    job: localStorage.getItem("music-atlas.workflow.last-job.v1"),
  })), { playlists: null, job: null });

  await register(page, "FirstUser");
  const emptyState = await page.evaluate(() => ({
    text: document.body.innerText,
    atlasStyle: getComputedStyle(document.querySelector(".atlas-name")).fontStyle,
  }));
  assert.doesNotMatch(emptyState.text, /账号数据隔离|本期/);
  assert.match(emptyState.text, /准备开始/);
  assert.equal(emptyState.atlasStyle, "normal");
  const firstUserId = await page.evaluate(() => CURRENT_USER.id);
  await page.evaluate(() => rememberWorkflowJobId("first-latest-job"));
  await page.evaluate(() => rememberPlaylist("https:" + "//music.163.com/playlist?id=123456", "First 私有歌单", "netease"));
  await page.waitForFunction(async () => {
    const payload = await (await fetch("/api/me/playlists")).json();
    return payload.playlists?.length === 1;
  });
  assert.deepEqual(await page.evaluate(() => ({ local: playlistHistory().length, remote: SAVED_PLAYLISTS.length })), { local: 1, remote: 1 });
  await page.evaluate(() => {
    USER_PREFERENCES = { track_percentile: 0.5 };
    ACTIVE_JOB_ID = "old-account-job";
    window.__streamClosed = false;
    workflowEventSource = { close() { window.__streamClosed = true; } };
    const realFetch = window.fetch.bind(window);
    window.fetch = (input, init) => String(input).includes("/api/jobs/old-account-job")
      ? new Promise((resolve) => { window.__releaseOldPoll = () => resolve(new Response(JSON.stringify({ ok: true, job: { id: "old-account-job", status: "running", events: [] } }), { status: 200, headers: { "Content-Type": "application/json" } })); })
      : realFetch(input, init);
    pollWorkflowJob("old-account-job");
    workflowPollTimer = setTimeout(() => {}, 60000);
  });
  await page.locator("[data-auth-logout]").click();
  await page.waitForFunction(() => !document.querySelector("[data-auth-logout]"));
  await page.evaluate(() => window.__releaseOldPoll());
  await page.waitForTimeout(100);
  const resetState = await page.evaluate(() => ({
    streamClosed: window.__streamClosed,
    timer: workflowPollTimer,
    currentUser: CURRENT_USER,
    savedCount: SAVED_PLAYLISTS.length,
    preferenceCount: Object.keys(USER_PREFERENCES).length,
    activeJob: ACTIVE_JOB_ID,
    latestJob: LATEST_JOB_ID,
  }));
  assert.deepEqual(resetState, {
    streamClosed: true, timer: null, currentUser: null,
    savedCount: 0, preferenceCount: 0, activeJob: null, latestJob: null,
  });
  await register(page, "SecondUser");
  const secondUserId = await page.evaluate(() => CURRENT_USER.id);
  assert.notEqual(secondUserId, firstUserId);
  assert.deepEqual(await page.evaluate(() => ({ local: playlistHistory().length, remote: SAVED_PLAYLISTS.length })), { local: 0, remote: 0 });
  const secondPlaylists = await page.evaluate(async () => (
    await (await fetch("/api/me/playlists", { cache: "no-store" })).json()
  ));
  assert.equal(secondPlaylists.playlists.length, 0);
  assert.equal(await page.evaluate(() => LATEST_JOB_ID), null);
  await page.evaluate(() => rememberWorkflowJobId("second-latest-job"));
  const storage = await page.evaluate(([firstId, secondId]) => ({
    first: localStorage.getItem(`music-atlas.user.workflow.last-job.v2.${firstId}`),
    second: localStorage.getItem(`music-atlas.user.workflow.last-job.v2.${secondId}`),
    firstPlaylist: JSON.parse(localStorage.getItem(`music-atlas.user.playlists.v2.${firstId}`) || "[]").length,
    secondPlaylist: JSON.parse(localStorage.getItem(`music-atlas.user.playlists.v2.${secondId}`) || "[]").length,
  }), [firstUserId, secondUserId]);
  assert.deepEqual(storage, {
    first: "first-latest-job", second: "second-latest-job",
    firstPlaylist: 1, secondPlaylist: 0,
  });
  assert.deepEqual(errors, []);
});
