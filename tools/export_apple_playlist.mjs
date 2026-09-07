#!/usr/bin/env node
// Export an Apple Music *shared* playlist to CSV via TuneMyMusic — no login.
//
// Usage:
//   node export_apple_playlist.mjs <playlist-url> <output-csv-path> [expected-count]
//
// TuneMyMusic parses the public share link server-side ("Load from URL"),
// so no TuneMyMusic account and no Apple login is required. Requires the
// `playwright` npm package (see tools/README.md).
//
// Output: one JSON summary line on stdout on success; human-readable errors
// on stderr with exit code 1 on failure.

import { chromium } from 'playwright';
import { downloadAndValidate, validateExpectedCount } from './apple_export_helpers.mjs';

const [url, outPath, expectedCountArg] = process.argv.slice(2);
if (!url || !outPath) {
  console.error('usage: node export_apple_playlist.mjs <playlist-url> <output-csv-path> [expected-count]');
  process.exit(2);
}

const log = (msg) => console.error(`[tmm] ${msg}`);

const expectedCount = expectedCountArg === undefined ? null : Number(expectedCountArg);
let browser;
try {
  validateExpectedCount(expectedCount);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  log('opening tunemymusic.com ...');
  await page.goto('https://www.tunemymusic.com/transfer', { waitUntil: 'domcontentloaded', timeout: 60000 });

  log('selecting Apple Music as source ...');
  await page.locator('button[class*="MusicServiceBlock"]:has(svg[aria-label="Apple logo"])').first()
    .click({ timeout: 20000 });

  log('loading playlist from URL ...');
  const formInput = page.locator('form input[type="text"]').first();
  await formInput.waitFor({ timeout: 15000 });
  await formInput.click();
  await formInput.pressSequentially(url, { delay: 4 });
  const submit = page.locator('form button[name="load_url"]');
  await submit.waitFor({ timeout: 10000 });
  await page.waitForFunction(
    () => {
      const b = document.querySelector('form button[name="load_url"]');
      return b && !b.disabled;
    },
    undefined, { timeout: 20000 },
  );
  await submit.click();

  // 解析完成信号：目标选择按钮出现（事件驱动，不依赖固定等待）
  log('waiting for playlist parse ...');
  await page.locator('button', { hasText: 'Choose Destination' }).first().waitFor({ timeout: 90000 });

  log('choosing destination ...');
  await page.locator('button', { hasText: 'Choose Destination' }).first().click({ timeout: 20000 });

  // 目标选择页存在中/英两种渲染，统一用 aria-label（语言无关）+ 双语文本 fallback
  const langSafe = (primary, en, zh) =>
    page.locator(primary).or(page.locator(`button:has-text("${en}")`)).or(page.locator(`button:has-text("${zh}")`)).first();

  log('choosing "Export to file" destination ...');
  const exportFileBtn = langSafe(
    'button:has(svg[aria-label*="ToFile"])',
    'Export to file',
    '导出到文件',
  );
  await exportFileBtn.waitFor({ timeout: 30000 });
  await exportFileBtn.click({ timeout: 20000 });

  log('selecting CSV format ...');
  await page.locator('label', { hasText: 'Comma separated values' }).locator('input').first()
    .check({ timeout: 15000 });

  log('waiting for CSV download ...');
  const summary = await downloadAndValidate(page, async () => {
    await page.getByRole('button', { name: 'Export', exact: true }).click({ timeout: 15000 });
    await page.getByRole('button', { name: 'Start Transfer', exact: true }).click({ timeout: 20000 });
  }, outPath, expectedCount);
  if (summary.completeness_status === 'unconfirmed') {
    log('completeness unconfirmed: no independent expected track count was supplied');
  }
  log(`saved ${summary.tracks} tracks -> ${summary.output}`);
  console.log(JSON.stringify(summary));
} catch (err) {
  console.error(`[tmm] export failed: ${err.message}`);
  console.error('[tmm] TuneMyMusic 页面结构可能已改版；请核对选择器（MusicServiceBlock/ToFile/Export/Start Transfer）。');
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
}
