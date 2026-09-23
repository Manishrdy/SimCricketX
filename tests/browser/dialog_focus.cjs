// Run with Playwright available on NODE_PATH; CHROME_PATH can select a local browser.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const controller = read('static/js/dialog_focus.js');
const extract = (source, name) => {
    const start = source.indexOf(`function ${name}(`);
    return source.slice(start, source.indexOf('\n}', start) + 2);
};
async function browserTest(fn) {
    const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
    try { await fn(await browser.newPage()); } finally { await browser.close(); }
}
const active = page => page.evaluate(() => document.activeElement.id);
const settle = page => page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
const base = `<style>.dialog {display:none}.dialog.show{display:block}</style>
    <main><button id="opener">Open</button><a id="background" href="#">Background</a></main>
    <div id="preexisting" inert><button>Already inert</button></div>
    <section id="dialog" class="dialog" data-focus-dialog data-dialog-open-class="show" data-dialog-close="#close" aria-label="Test">
    <button id="close" onclick="dialog.classList.remove('show')">Close</button><input id="last"></section>`;
async function setup(page, html = base, script = '') {
    await page.setContent(`${html}<script>${controller}</script><script>${script}</script>`);
    await page.evaluate(() => document.getElementById('opener').onclick = () => dialog.classList.add('show'));
}
test('Tab and Shift+Tab wrap; background is inert; Escape and button restore opener', () => browserTest(async page => {
    await setup(page);
    for (const method of ['Escape', 'button']) {
        await page.click('#opener'); assert.equal(await active(page), 'close');
        assert(await page.locator('main').evaluate(el => el.inert));
        await page.keyboard.press('Shift+Tab'); assert.equal(await active(page), 'last');
        await page.keyboard.press('Tab'); assert.equal(await active(page), 'close');
        await page.evaluate(() => document.getElementById('background').focus()); assert.equal(await active(page), 'close');
        if (method === 'Escape') await page.keyboard.press('Escape'); else await page.click('#close');
        assert.equal(await active(page), 'opener');
        assert.equal(await page.locator('main').evaluate(el => el.inert), false);
        assert(await page.locator('#preexisting').evaluate(el => el.inert));
        assert(await page.locator('#dialog').evaluate(el => el.inert));
    }
}));
test('nested dialogs restore the previous dialog before restoring the page', () => browserTest(async page => {
    await setup(page, base + `<section id="nested" class="dialog" data-focus-dialog data-dialog-open-class="show" data-dialog-close="#nested-close"><button id="nested-close" onclick="nested.classList.remove('show')">Close nested</button></section>`);
    await page.click('#opener'); await page.focus('#last');
    await page.evaluate(() => nested.classList.add('show')); await settle(page);
    assert.equal(await active(page), 'nested-close');
    await page.keyboard.press('Escape'); assert.equal(await active(page), 'last');
    assert(await page.locator('main').evaluate(el => el.inert));
    await page.keyboard.press('Escape'); assert.equal(await active(page), 'opener');
    assert(await page.locator('#nested').evaluate(el => el.inert));
}));
test('manual decision Escape preserves pending decision and can reopen through Resume Selection', () => browserTest(async page => {
    const template = read('templates/match_detail.html');
    const modal = template.slice(template.indexOf('<!-- Manual Decision Modal -->'), template.indexOf('<!-- Impact Player Modal -->'));
    const source = read('static/js/match_detail.js');
    const script = `let pendingManualDecision = {decision_type:'next_batter'}, pendingDecisionSelection = 2, decisionModalVisible = false, simulationMode='manual';
        function appendLog() {}
        ${['hideDecisionModal','closeDecisionModalOnly','updateDecisionResumeButton'].map(name => extract(source, name)).join('\n')}
        document.getElementById('decision-close-btn').onclick = closeDecisionModalOnly;
        function reopen() {document.getElementById('decision-overlay').style.display='flex';decisionModalVisible=true;updateDecisionResumeButton();}
        document.getElementById('resume-decision-btn').onclick=reopen;`;
    await page.setContent(`<style>.overlay{display:none}</style><main><button id="start">Start</button><button id="resume-decision-btn" style="display:none">Resume Selection</button></main>${modal}<script>${controller}</script><script>${script}</script>`);
    await page.focus('#start'); await page.evaluate(() => reopen()); await settle(page);
    assert.equal(await active(page), 'decision-close-btn');
    await page.keyboard.press('Escape'); assert.equal(await active(page), 'start');
    assert.equal(await page.evaluate(() => pendingManualDecision.decision_type), 'next_batter');
    assert.equal(await page.evaluate(() => pendingDecisionSelection), 2);
    await page.click('#resume-decision-btn'); assert.equal(await active(page), 'decision-close-btn');
    await page.keyboard.press('Escape'); assert.equal(await active(page), 'resume-decision-btn');
}));
test('required prompt without close action is not discarded by Escape', () => browserTest(async page => {
    await setup(page, base.replace('data-dialog-close="#close"', '').replace('onclick="dialog.classList.remove(\'show\')"', ''));
    await page.click('#opener'); await page.keyboard.press('Escape');
    assert.equal(await active(page), 'close'); assert(await page.locator('#dialog').evaluate(el => el.classList.contains('show')));
}));
test('admin mobile drawer updates expanded state and restores focus on Escape and desktop resize', () => browserTest(async page => {
    const template = read('templates/admin/layout.html');
    const script = extract(template, 'toggleAdminNav') + "\nwindow.matchMedia('(max-width: 900px)').addEventListener('change', () => toggleAdminNav(false));";
    await page.setViewportSize({ width: 600, height: 800 });
    await page.setContent(`<div class="admin-layout"><aside id="admin-sidebar"><button id="nav-close" onclick="toggleAdminNav(false)">Close</button><a href="#" id="nav-link">Link</a></aside><main><button id="menu" aria-controls="admin-sidebar" aria-expanded="false" onclick="toggleAdminNav()">Menu</button></main></div><script>${controller}</script><script>${script}</script>`);
    await page.click('#menu'); assert.equal(await page.getAttribute('#menu', 'aria-expanded'), 'true');
    assert.equal(await active(page), 'nav-close');
    await page.keyboard.press('Shift+Tab'); assert.equal(await active(page), 'nav-link');
    await page.keyboard.press('Escape'); assert.equal(await active(page), 'menu');
    assert.equal(await page.getAttribute('#menu', 'aria-expanded'), 'false');
    await page.click('#menu'); await page.setViewportSize({ width: 1200, height: 800 });
    await page.waitForFunction(() => document.getElementById('menu').getAttribute('aria-expanded') === 'false');
    assert.equal(await page.locator('main').evaluate(el => el.inert), false);
}));
test('reported dialog markup binds to its actual close control', () => browserTest(async page => {
    const cases = [
        ['templates/manage_teams.html', 'confirmModal', '<button onclick="closeModal()">Cancel</button>', 'closeModal'],
        ['templates/my_matches.html', 'delete-modal', '<button onclick="closeDeleteModal()">Cancel</button>', 'closeDeleteModal'],
        ['templates/ground_conditions.html', 'gcPitchEditorModal', '<button class="gc-pitch-modal-close" onclick="closePitchEditor()">Close</button>', 'closePitchEditor'],
        ['templates/ground_conditions.html', 'gcResetModal', '<button onclick="closeResetModal()">Cancel</button>', 'closeResetModal'],
        ['templates/ground_conditions.html', 'gcUnsavedModal', '<button id="gcKeepEditing" onclick="closeTestDialog()">Keep editing</button>', 'closeTestDialog'],
        ['templates/tournaments/dashboard.html', 'delete-tournament-dialog', '<button onclick="closeDeleteTournamentDialog()">Cancel</button>', 'closeDeleteTournamentDialog'],
    ];
    for (const [file, id, button, closeName] of cases) {
        await page.goto('about:blank');
        const opening = read(file).match(new RegExp(`<[^>]+id="${id}"[^>]*>`))[0];
        const html = `<main><button id="open">Open</button></main>${opening}${button}<input id="end"></${opening.startsWith('<section') ? 'section' : 'div'}>`;
        await page.setContent(`${html}<script>${controller}</script><script>
            const modal = document.getElementById('${id}');
            function ${closeName}() {modal.classList.remove(modal.dataset.dialogOpenClass);}
            document.getElementById('open').onclick = () => modal.classList.add(modal.dataset.dialogOpenClass);
            </script>`);
        await page.click('#open');
        assert.equal(await page.evaluate(id => document.getElementById(id).contains(document.activeElement), id), true, id);
        await page.keyboard.press('Shift+Tab'); assert.equal(await active(page), 'end', id);
        await page.keyboard.press('Escape'); assert.equal(await active(page), 'open', id);
        assert.equal(await page.locator('main').evaluate(el => el.inert), false, id);
    }
}));
test('replacing focused content keeps focus inside; deleting opener falls back to page', () => browserTest(async page => {
    await setup(page); await page.click('#opener');
    await page.evaluate(() => { document.getElementById('close').remove(); document.getElementById('opener').remove(); });
    await settle(page); assert.equal(await active(page), 'last');
    await page.evaluate(() => dialog.classList.remove('show')); await settle(page);
    assert.equal(await page.evaluate(() => document.activeElement.tagName), 'MAIN');
}));
