import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createAuthStore } from "../auth_store.js";
import { createIsolatedServer } from "./helpers.mjs";

const cookie = (response) => `atlas_session=${(response.headers.get("set-cookie") || "").match(/atlas_session=([^;]+)/)?.[1] || ""}`;

test("已锁定的三组曲目仅所属用户可读，不覆盖正式 Atlas", { concurrency: false }, async (t) => {
  let ownerId;
  let selectionPath;
  const jobId = "selection-job";
  const atlasGroups = Array.from({ length: 3 }, (_, groupIndex) => ({
    id: `atlas-${groupIndex + 1}`, label: `第 ${groupIndex + 1} 组`,
    recommendations: Array.from({ length: 10 }, (_, index) => {
      const number = groupIndex * 10 + index + 1;
      return { canonical_track_id: `netease:${number}`, title: `Song ${number}`,
        artist: `Artist ${number}`, metadata_verified: { url: `https://music.163.com/song?id=${number}`, source: "netease" } };
    }),
  }));
  const selection = { status: "tracks_locked", playlist_name: "测试歌单", source_track_count: 91,
    style_analysis: "公开记录支持的音乐风格分析", atlas_groups: atlasGroups };
  const server = await createIsolatedServer({ label: "selection-private", authRequired: true,
    beforeStart: ({ runtimeDir, authDbPath }) => {
      const store = createAuthStore(authDbPath);
      ownerId = store.createUser("PreviewOwner", "owner-password-2026", "user").id;
      store.createUser("PreviewOther", "other-password-2026", "user");
      store.close();
      const dir = path.join(runtimeDir, "jobs", jobId);
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, "web_job_state.json"), JSON.stringify({
        id: jobId, status: "failed", stage: "analysis", user_id: ownerId,
        created_at: "2026-09-23T00:00:00Z", updated_at: "2026-09-23T00:01:00Z",
        events: [{ event: "tracks_locked", stage: "recommendation", status: "running" }],
      }));
      selectionPath = path.join(dir, "web_selection.json");
      fs.writeFileSync(selectionPath, JSON.stringify(selection));
      fs.writeFileSync(path.join(dir, "web_preview.json"), JSON.stringify({
        status: "preliminary", playlist_name: "测试歌单", source_track_count: 91,
        overall_summary: "公开记录支持的整体描述", recommendations: [{ title: "Song", artist: "Artist", url: "https://music.example/song", platform: "netease" }],
      }));
    },
  });
  t.after(() => server.stop());
  const login = async (username, password) => cookie(await fetch(`${server.baseUrl}/api/auth/login`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ username, password }),
  }));
  const owner = await login("PreviewOwner", "owner-password-2026");
  const other = await login("PreviewOther", "other-password-2026");
  const url = `${server.baseUrl}/api/jobs/${jobId}/selection`;
  assert.equal((await fetch(url)).status, 401);
  assert.equal((await fetch(url, { headers: { cookie: other } })).status, 404);
  assert.equal((await fetch(`${server.baseUrl}/api/jobs/${jobId}/preview`, { headers: { cookie: owner } })).status, 404,
    "旧的临时预览接口不应继续可读");
  const response = await fetch(url, { headers: { cookie: owner } });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  const data = await response.json();
  assert.equal(data.selection.status, "tracks_locked");
  assert.equal(data.selection.atlas_groups.length, 3);
  assert.equal(data.selection.atlas_groups.flatMap((group) => group.recommendations).length, 30);
  assert.equal(data.selection.style_analysis, "公开记录支持的音乐风格分析");
  const atlas = await (await fetch(`${server.baseUrl}/api/atlas`, { headers: { cookie: owner } })).json();
  assert.equal(atlas.empty, true);

  fs.writeFileSync(selectionPath, JSON.stringify({ ...selection, status: "preliminary" }));
  assert.equal((await fetch(url, { headers: { cookie: owner } })).status, 404,
    "不得把尚未锁定的曲目当成正式结果");
  fs.writeFileSync(selectionPath, JSON.stringify({ ...selection, atlas_groups: [atlasGroups[0]] }));
  assert.equal((await fetch(url, { headers: { cookie: owner } })).status, 404,
    "不足三组时不得展示锁定结果");
  const repeated = structuredClone(selection);
  repeated.atlas_groups[2].recommendations[9] = { ...repeated.atlas_groups[0].recommendations[0] };
  fs.writeFileSync(selectionPath, JSON.stringify(repeated));
  assert.equal((await fetch(url, { headers: { cookie: owner } })).status, 404,
    "跨组重复时不得展示锁定结果");
});
