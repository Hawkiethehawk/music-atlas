import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import {
  buildApplePlaylistCsv,
  downloadAndValidate,
  parseApplePlaylistUrl,
  playlistFromPayload,
  validateAppleTracks,
  validateExportCsv,
  writeApplePlaylistCsv,
} from '../apple_export_helpers.mjs';

const CSV = '\uFEFFTrack name,Artist name,Apple - id\r\n"Song, with comma",Artist,101\r\n"Song\nwith ""quotes""",Artist,102\r\n';

test('Apple playlist URL parser accepts public share links and strips query state', () => {
  const parsed = parseApplePlaylistUrl('https://music.apple.com/us/playlist/Favorite/pl.u-test_123?l=zh');
  assert.equal(parsed.storefront, 'us');
  assert.equal(parsed.playlistId, 'pl.u-test_123');
  assert.equal(parsed.embedUrl, 'https://embed.music.apple.com/us/playlist/Favorite/pl.u-test_123');
  for (const invalid of [
    'http://music.apple.com/us/playlist/Favorite/pl.u-test',
    'https://example.com/us/playlist/Favorite/pl.u-test',
    'https://music.apple.com/us/album/Favorite/123',
  ]) assert.throws(() => parseApplePlaylistUrl(invalid));
});

test('official Apple payload converts to a validated CSV', () => {
  const payload = {
    data: [{
      id: 'pl.u-test', type: 'playlists', attributes: { name: 'My Playlist' },
      relationships: { tracks: { data: [{
        id: '101', type: 'songs', attributes: {
          name: 'Song, One', artistName: 'Artist One', albumName: 'Album One',
          isrc: 'AA111', releaseDate: '2026-01-02', url: 'https://music.apple.com/song/101',
          artwork: { url: 'https://example.com/{w}x{h}.jpg' },
        },
      }], next: '/v1/catalog/us/playlists/pl.u-test/tracks?offset=100' } },
    }],
  };
  const playlist = playlistFromPayload(payload, 'pl.u-test');
  assert.equal(playlist.name, 'My Playlist');
  assert.match(playlist.next, /offset=100/);
  const csv = buildApplePlaylistCsv(playlist.name, playlist.tracks);
  const parsed = validateExportCsv(csv, 1);
  assert.equal(parsed.tracks, 1);
  assert.match(csv, /"Song, One"/);
  assert.match(csv, /My Playlist/);
});

test('official Apple track validation rejects missing identities and preserves playlist duplicates', () => {
  assert.throws(() => validateAppleTracks([{ id: '1', type: 'songs', attributes: { name: '', artistName: 'A' } }]));
  const tracks = validateAppleTracks([
    { id: '1', type: 'songs', attributes: { name: 'One', artistName: 'A' } },
    { id: '1', type: 'songs', attributes: { name: 'One', artistName: 'A' } },
  ]);
  assert.equal(tracks.length, 2);
});

test('official Apple CSV write confirms completed pagination and preserves metadata', async () => {
  await withDirectory(async (root) => {
    const output = path.join(root, 'playlist.csv');
    const tracks = [{ id: '101', type: 'songs', attributes: { name: 'Song', artistName: 'Artist' } }];
    const raw = buildApplePlaylistCsv('Playlist', tracks);
    const result = writeApplePlaylistCsv(output, raw, {
      playlistId: 'pl.u-test', playlistName: 'Playlist', storefront: 'us', tracks: 1,
    });
    assert.equal(result.completeness_status, 'confirmed');
    assert.equal(result.expected_count_source, 'apple_official_pagination');
    assert.equal(result.tracks, 1);
    assert.equal(fs.readFileSync(output, 'utf8'), raw);
  });
});


async function withDirectory(run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-export-test-'));
  try {
    await run(root);
  } finally {
    for (const entry of fs.readdirSync(root)) fs.unlinkSync(path.join(root, entry));
    fs.rmdirSync(root);
  }
}

function pageWithDownload(raw = CSV, failure = false) {
  const events = new EventEmitter();
  return {
    page: { waitForEvent: (name) => new Promise((resolve) => events.once(name, resolve)) },
    trigger: () => events.emit('download', {
      suggestedFilename: () => 'playlist.csv',
      saveAs: async (destination) => {
        fs.writeFileSync(destination, raw);
        if (failure) throw new Error('download interrupted');
      },
    }),
  };
}

test('CSV parser handles BOM, commas, escaped quotes and multiline fields', () => {
  assert.equal(validateExportCsv(CSV, 2).tracks, 2);
  assert.equal(validateExportCsv(CSV, 2).completeness_status, 'confirmed');
});

test('missing independent total is explicitly unconfirmed', () => {
  const result = validateExportCsv(CSV);
  assert.equal(result.completeness_status, 'unconfirmed');
  assert.equal(result.expected_track_count, null);
  assert.equal(result.expected_count_source, 'unavailable');
});

test('invalid headers, empty rows and malformed CSV are rejected', () => {
  for (const raw of [
    '', 'wrong,header\nvalue,value\n', 'Track name,Artist name,Apple - id\n',
    'Track name,Artist name,Apple - id\n,Artist,1\n',
    'Track name,Artist name,Apple - id\nSong,,1\n',
    'Track name,Artist name,Apple - id\nSong,Artist\n',
    'Track name,Artist name,Apple - id\n"unclosed,Artist,1\n',
    'Track name,Artist name,Apple - id,Apple - id\nSong,Artist,1,1\n',
  ]) assert.throws(() => validateExportCsv(raw));
});

test('independent count mismatches are rejected', () => {
  assert.throws(() => validateExportCsv(CSV, 3), /count mismatch/);
});

test('invalid expected counts are rejected', () => {
  for (const count of [0, -1, 1.5, NaN, Infinity, true, '2', Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => validateExportCsv(CSV, count), /positive safe integer/);
  }
});

test('listener exists before immediate download and valid output replaces old CSV', async () => {
  await withDirectory(async (root) => {
    const output = path.join(root, 'playlist.csv');
    fs.writeFileSync(output, 'old export');
    const { page, trigger } = pageWithDownload();
    const result = await downloadAndValidate(page, trigger, output, 2);
    assert.equal(result.completeness_status, 'confirmed');
    assert.equal(result.tracks, 2);
    assert.equal(fs.readFileSync(output, 'utf8'), CSV);
    assert.deepEqual(fs.readdirSync(root), ['playlist.csv']);
  });
});

test('download without expected count remains unconfirmed', async () => {
  await withDirectory(async (root) => {
    const { page, trigger } = pageWithDownload();
    const result = await downloadAndValidate(page, trigger, path.join(root, 'playlist.csv'));
    assert.equal(result.completeness_status, 'unconfirmed');
  });
});

for (const scenario of ['invalid CSV', 'mismatched count', 'download failure', 'invalid UTF-8']) {
  test(`${scenario} preserves existing export and removes staging files`, async () => {
    await withDirectory(async (root) => {
      const output = path.join(root, 'playlist.csv');
      fs.writeFileSync(output, 'old export');
      const raw = scenario === 'invalid CSV' ? 'wrong header' : scenario === 'invalid UTF-8' ? Buffer.from([0xff]) : CSV;
      const { page, trigger } = pageWithDownload(raw, scenario === 'download failure');
      await assert.rejects(downloadAndValidate(page, trigger, output, scenario === 'mismatched count' ? 3 : 2));
      assert.equal(fs.readFileSync(output, 'utf8'), 'old export');
      assert.deepEqual(fs.readdirSync(root), ['playlist.csv']);
    });
  });
}
