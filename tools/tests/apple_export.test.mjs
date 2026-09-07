import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { downloadAndValidate, validateExportCsv } from '../apple_export_helpers.mjs';

const CSV = '\uFEFFTrack name,Artist name,Apple - id\r\n"Song, with comma",Artist,101\r\n"Song\nwith ""quotes""",Artist,102\r\n';

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
