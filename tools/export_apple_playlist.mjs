#!/usr/bin/env node
// Read a public Apple Music playlist through Apple's official web embed API.
// Usage: node export_apple_playlist.mjs <playlist-url> <output-csv-path> [expected-count]

import { chromium } from 'playwright';
import {
  buildApplePlaylistCsv,
  parseApplePlaylistUrl,
  playlistFromPayload,
  validateAppleTracks,
  validateExpectedCount,
  writeApplePlaylistCsv,
} from './apple_export_helpers.mjs';

const [url, outPath, expectedCountArg] = process.argv.slice(2);
if (!url || !outPath) {
  console.error('usage: node export_apple_playlist.mjs <playlist-url> <output-csv-path> [expected-count]');
  process.exit(2);
}

const log = (msg) => console.error(`[apple] ${msg}`);
const expectedCount = expectedCountArg === undefined ? null : Number(expectedCountArg);
let browser;

function nextPageUrl(next, storefront, playlistId) {
  const parsed = new URL(next, 'https://amp-api.music.apple.com');
  const prefix = `/v1/catalog/${storefront}/playlists/${playlistId}/tracks`;
  if (parsed.origin !== 'https://amp-api.music.apple.com' || parsed.pathname !== prefix) {
    throw new Error('Apple Music returned an unsafe pagination URL');
  }
  if (!parsed.searchParams.has('limit')) parsed.searchParams.set('limit', '100');
  return parsed;
}

try {
  validateExpectedCount(expectedCount);
  const { storefront, playlistId, embedUrl } = parseApplePlaylistUrl(url);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const initialPath = `/v1/catalog/${storefront}/playlists/${playlistId}`;
  const initialResponsePromise = page.waitForResponse((response) => {
    const parsed = new URL(response.url());
    return parsed.origin === 'https://amp-api.music.apple.com' && parsed.pathname === initialPath;
  }, { timeout: 60000 });

  log('opening Apple Music official embed ...');
  await page.goto(embedUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  log('waiting for official playlist response ...');
  const initialResponse = await initialResponsePromise;
  if (initialResponse.status() !== 200) {
    throw new Error(`Apple Music playlist request returned HTTP ${initialResponse.status()}`);
  }
  const requestHeaders = await initialResponse.request().allHeaders();
  const authorization = requestHeaders.authorization;
  if (!authorization) throw new Error('Apple Music web embed did not provide an API authorization token');
  const first = playlistFromPayload(await initialResponse.json(), playlistId);
  const tracks = [...first.tracks];
  let next = first.next;
  let pages = 1;

  while (next) {
    if (pages >= 100) throw new Error('Apple Music pagination exceeded 100 pages');
    const nextUrl = nextPageUrl(next, storefront, playlistId);
    log(`loading official playlist page ${pages + 1} ...`);
    const response = await page.request.get(nextUrl.href, {
      headers: {
        authorization,
        origin: 'https://embed.music.apple.com',
        referer: 'https://embed.music.apple.com/',
      },
      timeout: 45000,
    });
    if (!response.ok()) throw new Error(`Apple Music track page returned HTTP ${response.status()}`);
    const payload = await response.json();
    if (!Array.isArray(payload?.data)) throw new Error('Apple Music track page has an invalid response');
    tracks.push(...payload.data);
    next = payload.next || null;
    pages += 1;
  }

  validateAppleTracks(tracks);
  const csv = buildApplePlaylistCsv(first.name, tracks);
  const summary = writeApplePlaylistCsv(outPath, csv, {
    playlistId,
    playlistName: first.name,
    storefront,
    tracks: tracks.length,
  }, expectedCount);
  log(`saved ${summary.tracks} tracks from ${pages} official page(s) -> ${summary.output}`);
  console.log(JSON.stringify(summary));
} catch (err) {
  console.error(`[apple] export failed: ${err.message}`);
  console.error('[apple] The public playlist was not returned completely by Apple Music; no output file was replaced.');
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
}
