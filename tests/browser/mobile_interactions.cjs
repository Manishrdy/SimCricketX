// Browser-level emulation; real iOS/Android keyboard and touch checks remain manual.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const read = file => fs.readFileSync(path.join(__dirname, '../..', file), 'utf8');
async function browserTest(fn) {
    const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
    try { await fn(await browser.newPage()); } finally { await browser.close(); }
}
async function viewportSetup(page, size, visual, html, css) {
    await page.setViewportSize(size);
    await page.setContent(`<style>*{box-sizing:border-box;animation:none!important;transition:none!important}${css}\n${read('static/css/usable_viewport.css')}</style>${html}`);
    await page.evaluate(visual => {
        window.testViewport = Object.assign(new EventTarget(), visual);
        Object.defineProperty(window, 'visualViewport', { value: window.testViewport, configurable: true });
    }, visual);
    await page.addScriptTag({ content: read('static/js/usable_viewport.js') });
}
async function assertInside(page, selector, visual) {
    const box = await page.locator(selector).boundingBox();
    assert(box, selector);
    assert(box.y >= visual.offsetTop - 1, `${selector}: top ${box.y}`);
    assert(box.y + box.height <= visual.offsetTop + visual.height + 1, `${selector}: bottom ${box.y + box.height}`);
}
for (const [name, size, visual] of [
    ['portrait keyboard', { width: 390, height: 844 }, { width: 390, height: 330, offsetTop: 80, offsetLeft: 0 }],
    ['landscape keyboard', { width: 844, height: 390 }, { width: 844, height: 190, offsetTop: 20, offsetLeft: 0 }]
]) test(`${name}: support composer and modal actions stay reachable`, () => browserTest(async page => {
    const support = read('templates/_support_widget.html');
    const section = support.slice(support.indexOf('<section'), support.indexOf('</section>') + 10).replace('class="scx-support-panel"', 'class="scx-support-panel is-open"');
    await viewportSetup(page, size, visual, section, read('static/css/support_widget.css'));
    await page.locator('#scx-support-input').fill('Keyboard test');
    await assertInside(page, '#scx-support-panel', visual);
    await page.locator('#scx-support-send').evaluate(el => el.scrollIntoView({ block: 'nearest' }));
    await assertInside(page, '#scx-support-send', visual);
    await page.evaluate(() => {
        Object.assign(testViewport, { height: innerHeight, offsetTop: 0 });
        testViewport.dispatchEvent(new Event('resize'));
    });
    await page.waitForFunction(() => document.documentElement.style.getPropertyValue('--scx-vv-height') === innerHeight + 'px');
    await assertInside(page, '#scx-support-panel', { offsetTop: 0, height: size.height });

    const auth = read('templates/admin/auth_events.html');
    const notes = auth.slice(auth.indexOf('<div id="notesModal"'), auth.indexOf('\n<style>', auth.indexOf('<div id="notesModal"'))).replace('display:none;', 'display:flex;');
    await viewportSetup(page, size, visual, notes, '');
    // Long/resized textarea previously pushed the footer beyond short viewports.
    await page.locator('#notesText').evaluate(el => el.style.height = '450px');
    await assertInside(page, '#notesModal > div', visual);
    await page.locator('[onclick="saveNotes()"]').evaluate(el => el.scrollIntoView({ block: 'nearest' }));
    await assertInside(page, '[onclick="saveNotes()"]', visual);

    const matches = read('templates/my_matches.html');
    const modal = matches.slice(matches.indexOf('<!-- Delete Modal (Shared) -->'), matches.indexOf('<script>', matches.indexOf('<!-- Delete Modal (Shared) -->'))).replace('class="modal-overlay"', 'class="modal-overlay show"');
    const css = matches.match(/<style>([\s\S]*?)<\/style>/)[1];
    await viewportSetup(page, size, visual, modal, css);
    await assertInside(page, '.modal-box', visual);
    await page.locator('#confirm-delete-btn').evaluate(el => el.scrollIntoView({ block: 'nearest' }));
    await assertInside(page, '#confirm-delete-btn', visual);
}));
test('viewport fallback follows ordinary window resizing without VisualViewport', () => browserTest(async page => {
    await page.setViewportSize({ width: 700, height: 300 });
    await page.setContent('<div></div>');
    await page.evaluate(() => Object.defineProperty(window, 'visualViewport', { value: null }));
    await page.addScriptTag({ content: read('static/js/usable_viewport.js') });
    assert.equal(await page.evaluate(() => document.documentElement.style.getPropertyValue('--scx-vv-height')), '300px');
    await page.setViewportSize({ width: 700, height: 500 });
    await page.waitForFunction(() => document.documentElement.style.getPropertyValue('--scx-vv-height') === '500px');
}));
async function pointer(page, type, target, x, y) {
    await page.evaluate(({ type, target, x, y }) => {
        (target ? document.querySelector(target) : document).dispatchEvent(new PointerEvent(type, {
            bubbles: true, cancelable: true, pointerType: 'touch', pointerId: 1, isPrimary: true, clientX: x, clientY: y
        }));
    }, { type, target, x, y });
}
const dragSetup = `
    document.addEventListener('dragstart', e => e.dataTransfer.setData('text/plain', e.target.id));
    document.querySelectorAll('.list').forEach(list => {
        list.addEventListener('dragover', e => e.preventDefault());
        list.addEventListener('drop', e => list.appendChild(document.getElementById(e.dataTransfer.getData('text/plain'))));
    });`;
test('touch drag reaches a roster initially below the phone viewport', () => browserTest(async page => {
    await page.setViewportSize({ width: 390, height: 640 });
    await page.setContent(`<style>body{margin:0}.list{height:520px;overflow-y:auto;margin:20px;border:1px solid}.card{height:50px}</style>
        <div id="pool" class="list"><div id="player" class="card" draggable="true"><button class="player-touch-handle">Drag</button></div></div>
        <div id="roster" class="list"></div><script>${dragSetup}</script>`);
    await page.addScriptTag({ content: read('static/js/player_touch_drag.js') });
    await pointer(page, 'pointerdown', '.player-touch-handle', 50, 30);
    await pointer(page, 'pointermove', null, 50, 635);
    await page.waitForFunction(() => document.getElementById('roster').getBoundingClientRect().top < 250);
    await pointer(page, 'pointerup', null, 50, 400);
    assert.equal(await page.locator('#player').evaluate(el => el.parentElement.id), 'roster');
    const scroll = await page.evaluate(() => scrollY);
    await page.waitForTimeout(80); assert.equal(await page.evaluate(() => scrollY), scroll);
}));
test('stationary edge drag reaches the last player in a full roster', () => browserTest(async page => {
    await page.setViewportSize({ width: 390, height: 640 });
    const cards = Array.from({ length: 25 }, (_, i) => `<div class="card" id="player-${i}" draggable="true"><button class="player-touch-handle">Player ${i}</button></div>`).join('');
    await page.setContent(`<style>body{margin:0}.list{height:450px;overflow-y:auto;margin:50px 20px}.card{height:50px}</style><div id="roster" class="list">${cards}</div><script>${dragSetup}</script>`);
    await page.addScriptTag({ content: read('static/js/player_touch_drag.js') });
    await pointer(page, 'pointerdown', '.player-touch-handle', 50, 60);
    await pointer(page, 'pointermove', null, 50, 495);
    await page.waitForFunction(() => document.getElementById('roster').scrollTop >= 799);
    await pointer(page, 'pointerup', null, 50, 495);
    assert.equal(await page.locator('#roster > :last-child').getAttribute('id'), 'player-0');
}));
