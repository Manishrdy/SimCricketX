const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const read = file => fs.readFileSync(path.join(__dirname, '../..', file), 'utf8');
const template = read('templates/scorecard_view.html');
const start = template.indexOf('const scorecardFilename');
const handler = template.slice(start, template.indexOf('</script>', start)).replace(/{{[^\n]+}}/, '"Scorecard_Test"');
async function run(fn) {
    const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
    try { await fn(await browser.newPage({ viewport: { width: 390, height: 844 } })); }
    finally { await browser.close(); }
}
const markup = `<div id="scorecard-content"><div class="result-banner"><h1>Test won</h1><div class="save-button-wrapper"><button id="saveScorecard">Save as PNG</button><button id="saveScorecardText">Download TXT</button></div></div><div class="innings-card">Fourth innings: Keeper 100</div><div class="scorecard-footer">SimCricketX</div></div>`;
test('blocked CDN leaves PNG control usable and TXT still downloads full text', () => run(async page => {
    let blocked = false;
    await page.route('**/html2canvas.min.js', route => { blocked = true; return route.abort(); });
    const alerts = [];
    page.on('dialog', async dialog => { alerts.push(dialog.message()); await dialog.accept(); });
    await page.setContent(`${markup}<script>${read('static/js/image_export.js')}</script><script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script><script>${handler}</script>`);
    assert(blocked);
    await page.click('#saveScorecard');
    assert.match(alerts[0], /Reload.*Download TXT/);
    assert(await page.locator('#saveScorecard').isEnabled());
    assert(await page.locator('.save-button-wrapper').isVisible());
    const downloading = page.waitForEvent('download');
    await page.click('#saveScorecardText');
    const download = await downloading;
    assert.match(download.suggestedFilename(), /\.txt$/);
    const text = fs.readFileSync(await download.path(), 'utf8');
    assert.match(text, /Fourth innings: Keeper 100/);
    assert(!text.includes('Save as PNG'));
}));
test('long capture encodes a bounded real canvas as a blob and releases its bitmap', () => run(async page => {
    await page.setContent(`${markup}<style>#scorecard-content{height:16000px;width:1000px}</style><script>${read('static/js/image_export.js')}</script><script>${handler}</script>`);
    await page.evaluate(() => {
        window.html2canvas = async (element, options) => {
            const canvas = document.createElement('canvas');
            canvas.width = Math.floor(options.width * options.scale);
            canvas.height = Math.floor(options.height * options.scale);
            window.allocated = { width: canvas.width, height: canvas.height };
            window.lastCanvas = canvas;
            canvas.getContext('2d').fillRect(0, 0, 100, 100);
            return canvas;
        };
    });
    const downloading = page.waitForEvent('download');
    await page.click('#saveScorecard');
    const download = await downloading;
    assert.match(download.suggestedFilename(), /\.png$/);
    assert(fs.statSync(await download.path()).size > 0);
    const dims = await page.evaluate(() => allocated);
    assert(dims.width * dims.height <= 4_000_000); assert(dims.height <= 8192);
    assert.equal(await page.evaluate(() => lastCanvas.width), 0);
    assert(await page.locator('#saveScorecard').isEnabled());
}));
