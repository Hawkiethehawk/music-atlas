import { parse } from 'csv-parse/sync';
import fs from 'node:fs';
import path from 'node:path';

export function validateExpectedCount(expectedCount) {
  if (expectedCount !== null && (!Number.isSafeInteger(expectedCount) || expectedCount <= 0)) {
    throw new Error('expected count must be a positive safe integer');
  }
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

export async function downloadAndValidate(page, trigger, outPath, expectedCount = null) {
  validateExpectedCount(expectedCount);
  // Subscribe before either export button can trigger a fast download.
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
