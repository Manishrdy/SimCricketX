// Run through test_pages_and_javascript with T10_BROWSER_CHECK=1.
// Rendered authenticated fixture pages and local assets only; all writes are intercepted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
(async () => {
  const directory = process.argv[2];
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
  try {
    const page = await browser.newPage();
    const errors = [], writes = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.hostname !== 't10.test') return route.abort();
      if (request.method() === 'POST') {
        writes.push({ url: url.pathname, data: request.postDataJSON() });
        return route.fulfill({ json: { success: true, message: 'Saved' } });
      }
      let file;
      if (/^\/page\d+\.html$/.test(url.pathname)) file = path.join(directory, path.basename(url.pathname));
      else if (url.pathname.startsWith('/static/')) file = path.join(process.cwd(), url.pathname);
      if (file && fs.existsSync(file)) return route.fulfill({ path: file });
      return route.fulfill({ json: { success: true, data: [], messages: [] } });
    });
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('http://t10.test/page5.html');
      assert.equal(await page.locator('.gc-pitch-card').count(), 5);
      assert.equal(await page.locator('.gc-format-tab.active').innerText(), 'T10');
      await page.locator('#gcT20PhaseSection .gc-phase-toggle').click();
      await page.locator('#pp-boundary').fill('1.25');
      await page.locator('#btnSaveAll').click();
      await page.waitForFunction(() => !document.getElementById('btnSaveAll').disabled);
      const saved = writes.at(-1);
      assert.equal(saved.url, '/ground-conditions/save');
      assert.equal(saved.data.match_format, 'T10');
      assert.equal(saved.data.phase_boosts.powerplay.overs_end, 2);
      assert.equal(saved.data.phase_boosts.death_overs.overs_start, 7);
      assert.equal(saved.data.phase_boosts.powerplay.boundary_multiplier, 1.25);
      assert.equal(Object.keys(saved.data.pitch_profiles).length, 5);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    }
    await page.goto('http://t10.test/page0.html');
    await page.locator('.team-card').nth(0).click();
    await page.locator('.team-card').nth(1).click();
    await page.locator('#next-btn').click();
    await page.locator('label.format-option').filter({ has: page.locator('input[value="T10"]') }).click();
    assert.equal(await page.locator('#match-format-value').inputValue(), 'T10');
    assert.match(await page.locator('#format-help').innerText(), /10 overs/);
    assert.equal(await page.locator('#summary-format').innerText(), 'T10');
    assert.deepEqual(errors, []);
    console.log('T10 browser checks passed: desktop/mobile settings save, format selection and no page errors.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
