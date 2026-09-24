import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { gunzipSync } from "node:zlib";
import { archiveJobArtifacts } from "../maintenance.js";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

test("过期任务只压缩中间工件：可解压恢复，运行任务和最终结果保留", async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "atlas-archive-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const original = "真实原始响应\n".repeat(2000);
  for (const status of ["completed", "running"]) {
    const dir = path.join(root, status);
    await fs.mkdir(dir);
    await fs.writeFile(path.join(dir, "web_job_state.json"), JSON.stringify({ id: status, status, updated_at: "2020-01-01T00:00:00Z" }));
    await fs.writeFile(path.join(dir, "response.json"), original);
    await fs.writeFile(path.join(dir, "web_payload.json"), "keep");
  }
  const result = await archiveJobArtifacts(root);
  assert.equal(result.archived, 1);
  assert.ok(result.savedBytes > 0);
  assert.equal(gunzipSync(await fs.readFile(path.join(root, "completed/response.json.gz"))).toString(), original);
  assert.equal(await fs.readFile(path.join(root, "completed/web_payload.json"), "utf8"), "keep");
  assert.equal(await fs.readFile(path.join(root, "running/response.json"), "utf8"), original);
  assert.equal((await archiveJobArtifacts(root)).archived, 0);
});

test("重启将 stale running 同步为 interrupted；取消和复用独立统计并持久化", async (t) => {
  const cases = [ ["stale", "running", "", "interrupted"],
    ["cancelled", "failed", "任务已取消", "cancelled"],
    ["reuse", "failed", "已改用同链接、同范围的已有分析", "superseded"] ];
  const server = await createIsolatedServer({ label: "restore", beforeStart: async ({ runtimeDir, authDbPath }) => {
    const store = createAuthStore(authDbPath);
    for (const [id, status, error] of cases) {
      const dir = path.join(runtimeDir, "jobs", id);
      await fs.mkdir(dir, { recursive: true });
      await fs.writeFile(path.join(dir, "web_job_state.json"), JSON.stringify({ id, status, stage: "analysis",
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(), events: [{ event: status, status, error, seq: 1 }] }));
      store.createRun({ jobId: id, runtimeDir: dir });
      if (status === "failed") store.updateRun(id, { status, error });
    }
    store.close();
  }});
  t.after(() => server.stop());
  const store = createAuthStore(server.authDbPath);
  t.after(() => store.close());
  for (const [id, , , expected] of cases) {
    const api = await (await fetch(`${server.baseUrl}/api/jobs/${id}`)).json();
    assert.equal(api.job.status, expected);
    assert.equal(store.getRun(id).status, expected);
    const state = JSON.parse(await fs.readFile(path.join(server.runtimeDir, "jobs", id, "web_job_state.json")));
    assert.equal(state.status, expected);
  }
  assert.equal(store.runStats().failed, 0);
});

test("Atlas 缓存随发布更新失效；元数据拒绝超长参数", async (t) => {
  const server = await createIsolatedServer({ label: "cache" });
  t.after(() => server.stop());
  const file = path.join(server.runtimeDir, "current.json");
  for (const revision of [1, 2]) {
    await fs.writeFile(file, JSON.stringify({ payload_type: "music_atlas_web", revision }));
    await fs.utimes(file, new Date(), new Date(Date.now() + revision * 1000));
    const payload = await (await fetch(`${server.baseUrl}/api/atlas`)).json();
    assert.equal(payload.revision, revision);
  }
  assert.equal((await fetch(`${server.baseUrl}/api/meta?artist=${"a".repeat(201)}&track=x`)).status, 400);
});

test("登录用户只读取自己的 Atlas；新账号不会回退到全站旧结果", async (t) => {
  const server = await createIsolatedServer({ label: "user-atlas", authRequired: true });
  t.after(() => server.stop());

  const register = async (username) => {
    const response = await fetch(`${server.baseUrl}/api/auth/register`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username, password: "Passw0rd!123" }),
    });
    assert.equal(response.status, 201);
    const payload = await response.json();
    const cookie = String(response.headers.get("set-cookie") || "").split(";")[0];
    assert.ok(cookie.startsWith("atlas_session="));
    return { user: payload.user, cookie };
  };

  const hawkie = await register("hawkie-test");
  const test1 = await register("test1-test");
  await fs.writeFile(path.join(server.runtimeDir, "current.json"), JSON.stringify({ payload_type: "music_atlas_web", owner: "legacy-global" }));
  const hawkieDir = path.join(server.runtimeDir, "users", String(hawkie.user.id));
  await fs.mkdir(hawkieDir, { recursive: true });
  await fs.writeFile(path.join(hawkieDir, "current.json"), JSON.stringify({ payload_type: "music_atlas_web", owner: "hawkie" }));

  const hawkiePayload = await (await fetch(`${server.baseUrl}/api/atlas`, { headers: { cookie: hawkie.cookie } })).json();
  assert.equal(hawkiePayload.owner, "hawkie");

  const test1Payload = await (await fetch(`${server.baseUrl}/api/atlas`, { headers: { cookie: test1.cookie } })).json();
  assert.deepEqual(test1Payload, { ok: true, empty: true, reason: "no_user_atlas" });

  const guestPayload = await (await fetch(`${server.baseUrl}/api/atlas`)).json();
  assert.deepEqual(guestPayload, { ok: true, empty: true, reason: "no_user_atlas" });
});
