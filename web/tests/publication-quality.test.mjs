import test from "node:test";
import assert from "node:assert/strict";
import quality from "../publication_quality.js";

const { validatePublishedQuality } = quality;

function artifacts({ total = 91, playlistTotal = total, supported = 46,
  artistOnly = false, minimum } = {}) {
  const mode = artistOnly ? "artist_summary" : "public_facts_only";
  const coverage = {
    mode: artistOnly ? "artist_only" : "track_with_context",
    track_evidence_count: artistOnly ? 0 : supported,
    album_background_count: 0,
    artist_background_count: 0,
    no_style_evidence_count: artistOnly ? total : total - supported,
    weighted_artist_track_count: artistOnly ? supported : 0,
    artist_count: 2,
    sourced_artist_count: supported ? 1 : 0,
  };
  const packet = {
    source_track_count: total, source_playlist_track_count: playlistTotal, analysis_mode: mode,
    style_analysis: { evidence_model: "sourced_tags_v1", source_coverage: coverage },
    favorite_tracks: Array.from({ length: total }, () => ({})),
    source_tags: { records: Array.from({ length: total }, () => ({})) },
    recommendation_policy: { analysis_quality: { [artistOnly ? "min_artist_weight_share"
      : "min_track_or_album_share"]: minimum ?? (artistOnly ? 0.3 : 0.5) } },
  };
  const payload = { analysis: { sourceTrackCount: total,
    sourcePlaylistTrackCount: playlistTotal, evidenceModel: "sourced_tags_v1",
    mode, sourceCoverage: { ...coverage } } };
  return { packet, payload };
}

test("曲目模式：零来源和只靠艺人背景不得发布；恰好 50% 可以", () => {
  const zero = artifacts({ supported: 0 });
  assert.equal(validatePublishedQuality(zero.packet, zero.payload), false);
  const artistOnlyBackground = artifacts({ supported: 0 });
  artistOnlyBackground.packet.style_analysis.source_coverage.artist_background_count = 91;
  artistOnlyBackground.packet.style_analysis.source_coverage.no_style_evidence_count = 0;
  Object.assign(artistOnlyBackground.payload.analysis.sourceCoverage,
    artistOnlyBackground.packet.style_analysis.source_coverage);
  assert.equal(validatePublishedQuality(artistOnlyBackground.packet, artistOnlyBackground.payload), false);
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ total: 100, supported: 49 }))), false);
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ total: 100, supported: 50 }))), true);
});

test("1000 首起只能按歌手资料的歌单权重验收", () => {
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ total: 1000,
    supported: 299, artistOnly: true }))), false);
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ total: 1000,
    supported: 300, artistOnly: true }))), true);
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ total: 1000,
    supported: 500, artistOnly: false }))), false);
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ total: 999,
    supported: 300, artistOnly: true }))), false);
});

test("分位处理按原歌单数选择模式，按处理数计算来源覆盖", () => {
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ playlistTotal: 1917,
    total: 959, supported: 287, artistOnly: true }))), false);
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ playlistTotal: 1917,
    total: 959, supported: 288, artistOnly: true }))), true);
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ playlistTotal: 1917,
    total: 959, supported: 500, artistOnly: false }))), false);
  assert.equal(validatePublishedQuality(...Object.values(artifacts({ playlistTotal: 182,
    total: 91, supported: 46 }))), true);
});

test("原歌单数与处理数缺失、错位或 payload 不一致均不得发布", () => {
  const { packet, payload } = artifacts({ playlistTotal: 182, total: 91 });
  assert.equal(validatePublishedQuality(packet, payload), true);
  packet.source_playlist_track_count = 90;
  assert.equal(validatePublishedQuality(packet, payload), false);
  packet.source_playlist_track_count = 182.5;
  assert.equal(validatePublishedQuality(packet, payload), false);
  packet.source_playlist_track_count = 182;
  payload.analysis.sourcePlaylistTrackCount = 183;
  assert.equal(validatePublishedQuality(packet, payload), false);
  payload.analysis.sourcePlaylistTrackCount = 182;
  payload.analysis.sourceTrackCount = 92;
  assert.equal(validatePublishedQuality(packet, payload), false);
  delete packet.source_playlist_track_count;
  assert.equal(validatePublishedQuality(packet, payload), false);
});

test("来源统计、模式、质量下限与 payload 不一致时拒绝成功", () => {
  const { packet, payload } = artifacts();
  payload.analysis.sourceCoverage.track_evidence_count = 91;
  assert.equal(validatePublishedQuality(packet, payload), false);
  payload.analysis.sourceCoverage.track_evidence_count = 46;
  packet.recommendation_policy.analysis_quality.min_track_or_album_share = 0;
  assert.equal(validatePublishedQuality(packet, payload), false);
  packet.recommendation_policy.analysis_quality.min_track_or_album_share = 0.5;
  packet.source_tags.records.pop();
  assert.equal(validatePublishedQuality(packet, payload), false);
});

test("合唱或客串使艺人数超过曲目数时，仍按曲目来源覆盖判定", () => {
  const { packet, payload } = artifacts({ total: 3, supported: 2 });
  packet.style_analysis.source_coverage.artist_count = 4;
  packet.style_analysis.source_coverage.sourced_artist_count = 4;
  payload.analysis.sourceCoverage.artist_count = 4;
  payload.analysis.sourceCoverage.sourced_artist_count = 4;
  assert.equal(validatePublishedQuality(packet, payload), true);
  payload.analysis.sourceCoverage.artist_count = 3;
  assert.equal(validatePublishedQuality(packet, payload), false, "网页镜像仍须与分析包一致");
});
