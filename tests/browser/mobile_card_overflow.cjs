// Run with Playwright available on NODE_PATH; CHROME_PATH can select a local browser.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
function firstStyle(file) {
    const source = read(file);
    const start = source.indexOf('<style>');
    return source.slice(start + 7, source.indexOf('</style>', start));
}
const reset = `*{box-sizing:border-box}html,body{margin:0}button{font:inherit}
.main-content,.main-container{padding:1rem}`;
async function browserTest(fn) {
    const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
    try { await fn(await browser.newPage()); } finally { await browser.close(); }
}
async function open(page, width, css, html, extra = '') {
    await page.setViewportSize({ width, height: 900 });
    await page.setContent(`<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
        <style>${reset}${css}${extra}</style></head><body>${html}</body></html>`);
}
async function pageWidth(page) {
    return page.evaluate(() => Math.max(document.documentElement.scrollWidth, document.body.scrollWidth));
}
function assertFits(width, actual, label) {
    assert.ok(actual <= width, `${label}: document is ${actual}px wide`);
}
function teamCard(name) {
    return `<div class="glass-card"><div class="card-header"><div class="team-badge"><div class="team-color-dot"></div>
        <div class="team-name">${name}</div></div><div class="team-code">LONG</div></div>
        <div class="card-body"><div class="info-grid"><div class="info-item"><span class="info-label">Captain</span>
        <span class="info-value">${name}</span></div></div></div>
        <div class="card-footer"><div class="card-meta">Last updated Sep 18, 2026 14:07 UTC</div>
        <div class="card-actions"><button class="action-btn edit">Edit</button><button class="action-btn delete">Del</button></div></div></div>`;
}
// Mirrors renderRoster() in templates/team_create.html: the slim card keeps
// grip/order/name/role/actions on one identity row, FC ratings underneath.
function fcRow(idx, name) {
    return `<div class="tc-player" draggable="true"><div class="tc-player-top">
        <span class="tc-grip player-touch-handle">⠿</span><span class="tc-player-order">${idx}</span>
        <div class="tc-player-main"><div class="tc-player-name">${name}</div>
        <div class="tc-player-meta">Right hand bat · Fast-medium</div></div>
        <span class="tc-role-pill bat">BAT</span>
        <span class="tc-player-actions">
          <button type="button" data-roster-action="up">↑</button>
          <button type="button" data-roster-action="down">↓</button>
          <button type="button" data-roster-action="remove">✕</button>
        </span></div>
        <div class="tc-fc-ratings">
          <div class="tc-fc-rating"><label>TEC</label><input type="number" value="50"></div>
          <div class="tc-fc-rating"><label>TMP</label><input type="number" value="50"></div>
          <div class="tc-fc-rating"><label>STA</label><input type="number" value="50"></div>
        </div></div>`;
}

test('team, story, analytics and match-setup cards stay within 320px, including 200% text', () => browserTest(async page => {
    const long = 'Sri Lanka Board President\'s XI Super Kings United';
    const cases = [
        ['templates/manage_teams.html', `<main class="main-content"><div class="teams-grid">${teamCard(long)}${teamCard(long)}</div>
            <div class="table-scroll" style="overflow-x:auto"><table style="min-width:720px"><tr><td>wide table stays local</td></tr></table></div></main>`],
        ['templates/scenarios.html', `<main class="main-container"><div class="scenario-grid"><article class="scenario-card">
            <h2>${long}</h2><p class="scenario-story">A famous collapse, rewritten with your squad.</p>
            <a class="scenario-play-btn" href="#">Play this story</a></article></div></main>`],
        ['templates/my_analytics.html', `<main class="main-container"><div class="ma-page"><div class="ma-split">
            <div class="ma-card"><div class="ma-row"><div class="ma-name">${long}</div><span>BAT 92</span></div></div>
            <div class="ma-card"><div class="ma-format-row"><span>First-Class</span><div style="flex:1;min-width:0;height:10px;background:#ddd"></div><span>12</span></div></div>
            </div></div></main>`],
        ['templates/match_setup.html', `<main class="main-container"><div class="ms-shell"><div class="ms-main"><div class="teams-grid">
            <div class="team-card"><span class="color-dot">CSK</span><div class="team-info"><h3>${long}</h3>
            <div class="team-meta">Chepauk Super Stadium</div></div></div></div></div></div></main>`]
    ];
    for (const width of [320, 390]) {
        for (const [file, html] of cases) {
            await open(page, width, firstStyle(file), html);
            assertFits(width, await pageWidth(page), `${file} at ${width}px`);
            await open(page, width, firstStyle(file), html, 'html{font-size:200%}');
            assertFits(width, await pageWidth(page), `${file} at ${width}px with 200% text`);
        }
    }
}));

test('25-player FC roster keeps names and actions readable at 320 and 390px', () => browserTest(async page => {
    const css = firstStyle('templates/team_create.html');
    const name = 'Chandrasekharan Venkataraghavan Jr';
    const rows = Array.from({ length: 25 }, (_, i) => fcRow(i + 1, name)).join('');
    const html = `<main class="main-container"><div class="tc-page"><section class="tc-panel">
        <div class="tc-list">${rows}</div>
        <div class="tc-leaders"><div class="tc-field"><label>Captain</label>
        <select class="tc-select"><option>${name} (wicket-keeper batsman)</option></select></div>
        <div class="tc-field"><label>Wicketkeeper</label>
        <select class="tc-select"><option>${name}</option></select></div></div>
        </section></div></main>`;
    for (const width of [320, 390]) {
        await open(page, width, css, html);
        assertFits(width, await pageWidth(page), `roster at ${width}px`);
        const metrics = await page.evaluate(() => {
            const row = document.querySelector('.tc-player');
            const nameBox = document.querySelector('.tc-player-name');
            const remove = document.querySelector('[data-roster-action="remove"]');
            const leaders = getComputedStyle(document.querySelector('.tc-leaders')).gridTemplateColumns;
            return {
                stacked: getComputedStyle(row).flexDirection === 'column',
                nameVisible: nameBox.getClientRects()[0].width > 40 && nameBox.scrollWidth <= nameBox.clientWidth + 1,
                removeVisible: remove.getBoundingClientRect().right <= document.documentElement.clientWidth,
                leaderCols: leaders.split(' ').length,
                count: document.querySelectorAll('.tc-player').length
            };
        });
        assert.equal(metrics.count, 25);
        assert.equal(metrics.stacked, true);
        assert.equal(metrics.nameVisible, true);
        assert.equal(metrics.removeVisible, true);
        assert.equal(metrics.leaderCols, 1);
        await open(page, width, css, html, 'html{font-size:200%}');
        assertFits(width, await pageWidth(page), `roster at ${width}px with 200% text`);
    }
}));
