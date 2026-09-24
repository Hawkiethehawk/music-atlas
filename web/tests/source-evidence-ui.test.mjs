import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const page = fs.readFileSync(path.join(import.meta.dirname, "..", "editorial-atlas.html"), "utf8");
const admin = fs.readFileSync(path.join(import.meta.dirname, "..", "admin.html"), "utf8");
const esc = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;");

function execute(start, end, expression, context = {}) {
  const first = page.indexOf(start), last = page.indexOf(end, first + start.length);
  assert.ok(first >= 0 && last > first, `missing UI function ${start}`);
  return vm.runInNewContext(`${page.slice(first, last)}\n${expression}`, { esc, ...context });
}

function analysis(mode, sourceCoverage, total) {
  return { evidenceModel: "sourced_tags_v1", mode, sourceTrackCount: total, sourceCoverage };
}

test("逐曲模式分别显示曲目、专辑、艺人和无资料，不能把 88/91 叫单曲证实", () => {
  const html = execute("function analysisCoverageHTML(){", "function sourceInterestIslands(", "analysisCoverageHTML()", {
    ATLAS: { analysis: analysis("track_with_context", { mode: "track_with_context",
      track_evidence_count: 22, album_background_count: 36, artist_background_count: 30,
      no_style_evidence_count: 3 }, 91) },
  });
  assert.match(html, /单曲标签<\/span>/);
  assert.match(html, /所属专辑背景<\/span>/);
  assert.match(html, /仅有艺人背景<\/span>/);
  assert.match(html, /暂无可归属风格资料<\/span>/);
  assert.match(html, /单曲与专辑资料计入发布覆盖/);
  assert.doesNotMatch(html, /单曲.*88\/91/);
});

test("千首模式只显示歌手及曲目权重，不生成逐曲风格数字", () => {
  const artistAnalysis = analysis("artist_summary", { mode: "artist_only", sourced_artist_count: 31,
    artist_count: 67, weighted_artist_track_count: 1340 }, 1917);
  const card = execute("function analysisCoverageHTML(){", "function sourceInterestIslands(", "analysisCoverageHTML()",
    { ATLAS: { analysis: artistAnalysis } });
  assert.match(card, /1000 首及以上的歌单只按歌手层级分析/);
  assert.match(card, /有资料歌手对应的曲目权重/);
  assert.doesNotMatch(card, /单曲标签<\/span>/);
  const taste = execute("function vTaste(){", "function vInterest(", "vTaste()", {
    ATLAS: { analysis: artistAnalysis }, INTERESTS: [{ id: "island-1", code: "01", name: "歌手组",
      artists: ["Artist"], sourceArtists: [{ artist: "Artist", count: 8 }], genres: [], summary: "资料摘要" }],
    RECS: [], analysisCoverageHTML: () => card, interestHref: () => "#/interest/island-1",
  });
  assert.match(taste, /按歌手出现频次与公开歌手资料/);
  assert.match(taste, /查看 1 位歌手与来源/);
  assert.doesNotMatch(taste, /查看 1 首曲目与来源/);
  const progress = execute("function workflowEventText(event){", "function workflowDisplayEvents(",
    'workflowEventText({stage:"analysis",event:"completed",source_track_count:1917,source_coverage:{mode:"artist_only",artist_count:67,sourced_artist_count:31,weighted_artist_track_count:1340}})');
  assert.match(progress, /31\/67 位歌手；曲目权重 1340\/1917/);
  assert.doesNotMatch(progress, /分析完成 · 0 首/);
});

test("缺口状态不称全覆盖，来源缺失或身份不符不展示风格标签", () => {
  const banner = execute("function statusBanner(){", "function analysisCoverageHTML(){", "statusBanner()", {
    ATLAS: { status: { analysis: "complete_with_gaps", profile_coverage_degraded: true } },
    guestMode: () => false,
  });
  assert.match(banner, /已达到当前模式的发布门槛/);
  assert.doesNotMatch(banner, /状态：已完成/);
  assert.match(page, /complete_with_gaps:"已完成（有资料缺口）"/);
  const rows = execute("function islandSourcesInner(i){", "const COPY_SKIP_KEYS", "islandSourcesInner(i)", {
    SOURCE_PAGE_SIZE: 10, SOURCE_PAGE_INDEX: 1, i: { sourceRecords: [{ title: "Track", artist: "Artist",
      evidence: [{ scope: "track", status: "supported", identity_status: "mismatch", tags: [{ tag: "不可信标签" }],
        url: "https://www.last.fm/music/Test" }] }] },
  });
  assert.match(rows, /身份不匹配/);
  assert.doesNotMatch(rows, /不可信标签/);
});

test("Admin 设置说明分层资料规则，不暴露旧主观听感配置", () => {
  assert.match(admin, /data-group="analysis-contract"/);
  assert.match(admin, /1000 首及以上仅分析歌手/);
  assert.match(admin, /艺人背景不算单曲证实/);
  assert.match(admin, /缺失资料保持未知/);
  assert.doesNotMatch(admin, /不配置八轴/);
  assert.doesNotMatch(admin, /data-field="[^"]*(?:style_axes|axis_fit|arc_weight)/);
});
