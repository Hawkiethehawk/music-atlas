"use strict";

// Defensive server-side check. Python's contract gate must run BEFORE its
// atomic current/history commit; this check prevents a child success event or
// a zero exit code from being mistaken for a quality-approved publication.
function validCount(value, total) {
  return Number.isSafeInteger(value) && value >= 0 && value <= total;
}

function validatePublishedQuality(packet, payload) {
  const analysis = packet && packet.style_analysis;
  const view = payload && payload.analysis;
  const coverage = analysis && analysis.source_coverage;
  const mirrored = view && view.sourceCoverage;
  const total = packet && packet.source_track_count;
  const playlistTotal = packet && packet.source_playlist_track_count;
  if (!Number.isSafeInteger(total) || total < 1
      || !Number.isSafeInteger(playlistTotal) || playlistTotal < total
      || analysis?.evidence_model !== "sourced_tags_v1"
      || view?.evidenceModel !== "sourced_tags_v1"
      || view.sourceTrackCount !== total
      || view.sourcePlaylistTrackCount !== playlistTotal
      || !Array.isArray(packet.favorite_tracks) || packet.favorite_tracks.length !== total
      || !Array.isArray(packet.source_tags?.records) || packet.source_tags.records.length !== total
      || !coverage || typeof coverage !== "object" || !mirrored || typeof mirrored !== "object"
      || view.mode !== packet.analysis_mode) return false;

  const artistOnly = playlistTotal >= 1000;
  if ((packet.analysis_mode === "artist_summary") !== artistOnly
      || coverage.mode !== (artistOnly ? "artist_only" : "track_with_context")
      || mirrored.mode !== coverage.mode) return false;
  const fields = ["track_evidence_count", "album_background_count", "artist_background_count",
    "no_style_evidence_count", "weighted_artist_track_count"];
  for (const field of fields) {
    if (!validCount(coverage[field], total) || mirrored[field] !== coverage[field]) return false;
  }
  // A song can credit several artists; the artist count can exceed the song
  // count without inflating source coverage or the weighted-song numerator.
  for (const field of ["artist_count", "sourced_artist_count"]) {
    if (!Number.isSafeInteger(coverage[field]) || coverage[field] < 0
        || mirrored[field] !== coverage[field]) return false;
  }
  if (coverage.sourced_artist_count > coverage.artist_count
      || coverage.track_evidence_count + coverage.album_background_count
        + coverage.artist_background_count + coverage.no_style_evidence_count !== total) return false;
  if (artistOnly && (coverage.track_evidence_count || coverage.album_background_count
      || coverage.artist_background_count || coverage.no_style_evidence_count !== total)) return false;

  const metric = artistOnly ? "min_artist_weight_share" : "min_track_or_album_share";
  const quality = packet.recommendation_policy?.analysis_quality;
  const minimum = quality && quality[metric];
  if (!quality || Object.keys(quality).length !== 1
      || typeof minimum !== "number" || !Number.isFinite(minimum)
      || minimum <= 0 || minimum > 1) return false;
  const supported = artistOnly ? coverage.weighted_artist_track_count
    : coverage.track_evidence_count + coverage.album_background_count;
  return supported > 0 && supported / total >= minimum;
}

module.exports = { validatePublishedQuality };
