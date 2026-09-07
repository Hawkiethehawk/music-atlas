import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { chromium } from 'playwright';
import { downloadAndValidate } from '../apple_export_helpers.mjs';

test('Chromium downloads multiline CSV and preserves it after a count mismatch', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-browser-test-'));
  const output = path.join(root, 'playlist.csv');
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ acceptDownloads: true });
    await page.setContent('<a id="download" download="playlist.csv">Download</a>');
    const csv = 'Track name,Artist name,Apple - id\n"Song\nOne",Artist,101\nSong Two,Artist,102\n';
    await page.locator('#download').evaluate((element, content) => {
      element.href = URL.createObjectURL(new Blob([content], { type: 'text/csv' }));
    }, csv);
    const result = await downloadAndValidate(page, () => page.locator('#download').click(), output, 2);
    assert.equal(result.tracks, 2);
    assert.equal(result.completeness_status, 'confirmed');
    assert.equal(fs.readFileSync(output, 'utf8'), csv);
    await assert.rejects(downloadAndValidate(page, () => page.locator('#download').click(), output, 3), /count mismatch/);
    assert.equal(fs.readFileSync(output, 'utf8'), csv);
  } finally {
    if (browser) await browser.close();
    for (const entry of fs.readdirSync(root)) fs.unlinkSync(path.join(root, entry));
    fs.rmdirSync(root);
  }
});
