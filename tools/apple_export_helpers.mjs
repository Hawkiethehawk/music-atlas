import { parse } from 'csv-parse/sync';
import fs from 'node:fs';
import path from 'node:path';

export function validateExpectedCount(expectedCount) {
  if (expectedCount !== null && (!Number.isSafeInteger(expectedCount) || expectedCount <= 0)) {
    throw new Error('expected count must be a positive safe integer');
  }
}

export function parseApplePlaylistUrl(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error('Apple Music playlist URL is invalid');
  }
  if (parsed.protocol !== 'https:' || !['music.apple.com', 'embed.music.apple.com'].includes(parsed.hostname)) {
    throw new Error('Apple Music playlist URL must use https://music.apple.com');
  }
  const segments = parsed.pathname.split('/').filter(Boolean);
  const playlistIndex = segments.indexOf('playlist');
  if (playlistIndex < 1 || playlistIndex + 2 >= segments.length) {
    throw new Error('Apple Music playlist URL is missing storefront, slug, or playlist id');
  }
  const storefront = segments[playlistIndex - 1].toLowerCase();
  const playlistId = segments.at(-1);
  if (!/^[a-z]{2}$/.test(storefront) || !/^pl\.[A-Za-z0-9._-]+$/.test(playlistId)) {
    throw new Error('Apple Music playlist URL has an unsupported storefront or playlist id');
  }
  const embed = new URL(parsed.href);
  embed.hostname = 'embed.music.apple.com';
  embed.search = '';
  embed.hash = '';
  return { storefront, playlistId, embedUrl: embed.href };
}

export function playlistFromPayload(payload, expectedPlaylistId) {
  const playlist = payload?.data?.[0];
  if (!playlist || playlist.type !== 'playlists' || playlist.id !== expectedPlaylistId) {
    throw new Error('Apple Music returned no matching public playlist');
  }
  const name = String(playlist.attributes?.name || '').trim();
  const relationship = playlist.relationships?.tracks;
  if (!name || !relationship || !Array.isArray(relationship.data)) {
    throw new Error('Apple Music playlist response is missing name or tracks');
  }
  return { name, tracks: relationship.data, next: relationship.next || null };
}

export function validateAppleTracks(tracks) {
  if (!Array.isArray(tracks) || tracks.length === 0) {
    throw new Error('Apple Music playlist contains no readable tracks');
  }
  return tracks.map((track, index) => {
    const attributes = track?.attributes || {};
    const id = String(track?.id || '').trim();
    const title = String(attributes.name || '').trim();
    const artist = String(attributes.artistName || '').trim();
    if (track?.type !== 'songs' || !id || !title || !artist) {
      throw new Error(`Apple Music track ${index + 1} is missing id, title, or artist`);
    }
    return {
      id,
      title,
      artist,
      album: String(attributes.albumName || '').trim(),
      isrc: String(attributes.isrc || '').trim(),
      releaseDate: String(attributes.releaseDate || '').trim(),
      url: String(attributes.url || '').trim(),
      artworkUrl: String(attributes.artwork?.url || '').trim(),
    };
  });
}

function csvCell(value) {
  const text = String(value ?? '');
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function buildApplePlaylistCsv(playlistName, tracks) {
  const header = ['Track name', 'Artist name', 'Album', 'Playlist name', 'Type', 'ISRC', 'Apple - id', 'Release date', 'Track URL', 'Artwork URL'];
  const rows = validateAppleTracks(tracks).map((track) => [
    track.title,
    track.artist,
    track.album,
    playlistName,
    'Playlist',
    track.isrc,
    track.id,
    track.releaseDate,
    track.url,
    track.artworkUrl,
  ]);
  return `\uFEFF${[header, ...rows].map((row) => row.map(csvCell).join(',')).join('\r\n')}\r\n`;
}

export function validateExportCsv(raw, expectedCount = null) {
  validateExpectedCount(expectedCount);
  const records = parse(raw, { bom: true, skip_empty_lines: true });
  const [header = [], ...tracks] = records;
  const required = ['Track name', 'Artist name', 'Apple - id'];
  if (new Set(header).size !== header.length || !required.every((key) => header.includes(key))) {
    throw new Error('unexpected CSV header: Track name, Artist name and Apple - id are required');
  }
  if (tracks.length === 0) throw new Error('exported CSV contains no data rows');
  const titleIndex = header.indexOf('Track name');
  const artistIndex = header.indexOf('Artist name');
  if (tracks.some((row) => !row[titleIndex].trim() || !row[artistIndex].trim())) {
    throw new Error('exported CSV contains an empty track title or artist');
  }
  if (expectedCount !== null && tracks.length !== expectedCount) {
    throw new Error(`playlist count mismatch: expected ${expectedCount}, exported ${tracks.length}`);
  }
  return {
    tracks: tracks.length,
    expected_track_count: expectedCount,
    expected_count_source: expectedCount === null ? 'unavailable' : 'argument',
    completeness_status: expectedCount === null ? 'unconfirmed' : 'confirmed',
  };
}

export function writeApplePlaylistCsv(outPath, raw, metadata, expectedCount = null) {
  validateExpectedCount(expectedCount);
  const validated = validateExportCsv(raw, expectedCount ?? metadata.tracks);
  const destination = path.resolve(outPath);
  const parent = path.dirname(destination);
  fs.mkdirSync(parent, { recursive: true });
  const stagingDirectory = fs.mkdtempSync(path.join(parent, '.atlas-export-'));
  const stagedFile = path.join(stagingDirectory, 'playlist.csv');
  try {
    fs.writeFileSync(stagedFile, raw, 'utf8');
    fs.renameSync(stagedFile, destination);
    return {
      status: 'exported',
      source: 'apple_music_official_embed',
      output: destination,
      playlist_id: metadata.playlistId,
      playlist_name: metadata.playlistName,
      storefront: metadata.storefront,
      tracks: validated.tracks,
      expected_track_count: expectedCount ?? validated.tracks,
      expected_count_source: expectedCount === null ? 'apple_official_pagination' : 'argument',
      completeness_status: 'confirmed',
    };
  } finally {
    if (fs.existsSync(stagedFile)) fs.unlinkSync(stagedFile);
    fs.rmdirSync(stagingDirectory);
  }
}

export async function downloadAndValidate(page, trigger, outPath, expectedCount = null) {
  validateExpectedCount(expectedCount);
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 180000 }),
    trigger(),
  ]);
  const destination = path.resolve(outPath);
  const parent = path.dirname(destination);
  fs.mkdirSync(parent, { recursive: true });
  const stagingDirectory = fs.mkdtempSync(path.join(parent, '.atlas-export-'));
  const stagedFile = path.join(stagingDirectory, 'playlist.csv');
  try {
    await download.saveAs(stagedFile);
    const raw = new TextDecoder('utf-8', { fatal: true }).decode(fs.readFileSync(stagedFile));
    const result = validateExportCsv(raw, expectedCount);
    const sourceFile = download.suggestedFilename();
    fs.renameSync(stagedFile, destination);
    return { status: 'exported', output: destination, source_file: sourceFile, ...result };
  } finally {
    if (fs.existsSync(stagedFile)) fs.unlinkSync(stagedFile);
    fs.rmdirSync(stagingDirectory);
  }
}
