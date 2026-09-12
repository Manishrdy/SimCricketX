// Run with: node --test tests/scorecard_flow.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/js/match_detail.js'), 'utf8');
function functionSource(name) {
    const start = source.indexOf(`async function ${name}(`);
    const end = source.indexOf('\n}', start) + 2;
    return source.slice(start, end);
}

test('FC Close is installed before export and resumes exactly once after failure', async () => {
    const button = { textContent: 'Close' };
    const overlay = { style: { display: 'flex' } };
    let rejectCapture;
    let resumed = 0;
    const context = vm.createContext({
        document: { getElementById: () => overlay, querySelector: () => button },
        captureCurrentScorecardImage: () => {
            assert.equal(typeof button.onclick, 'function');
            return new Promise((_, reject) => { rejectCapture = reject; });
        },
        scheduleNextBall: () => resumed++, delay: 0, traceMatch: () => {},
    });
    vm.runInContext(functionSource('fcHoldScorecard'), context);
    const hold = context.fcHoldScorecard('day_01_lunch');
    const failure = assert.rejects(hold, /export failed/);
    const close = button.onclick();
    await button.onclick();
    rejectCapture(new Error('export failed'));
    await Promise.all([close, failure]);
    assert.equal(resumed, 1);
    assert.equal(overlay.style.display, 'none');
    assert.equal(button.disabled, false);
});

test('stalled optional export times out', async () => {
    const context = vm.createContext({ setTimeout, clearTimeout });
    vm.runInContext(functionSource('withScorecardTimeout'), context);
    await assert.rejects(context.withScorecardTimeout(new Promise(() => {}), 'Rendering', 5), /Rendering timed out/);
    assert.equal(await context.withScorecardTimeout(Promise.resolve(true), 'Rendering', 5), true);
});

test('capture excludes commentary, keeps panel ancestry, and never restyles live panel', async () => {
    const panel = { style: { cssText: 'original' }, contains: e => e.inside === true };
    const title = { textContent: '1st INNINGS', style: { color: 'original' } };
    const context = vm.createContext({
        document: { querySelector: () => panel, getElementById: () => title },
        requestAnimationFrame: callback => callback(), setTimeout, clearTimeout,
        appendLog: () => {}, console,
        html2canvas: async (target, options) => {
            assert.equal(target, panel);
            const element = (tagName, ancestor = false, inside = false) => ({tagName, contains: () => ancestor, inside});
            assert.equal(options.ignoreElements(element('DIV')), true);
            assert.equal(options.ignoreElements(element('BODY', true)), false);
            assert.equal(options.ignoreElements(element('TD', false, true)), false);
            assert.equal(options.ignoreElements(element('STYLE')), false);
            return { toBlob: callback => callback({ image: true }) };
        },
    });
    vm.runInContext(functionSource('withScorecardTimeout') + '\n' + functionSource('captureCurrentScorecardImage'), context);
    assert.equal(await context.captureCurrentScorecardImage(), true);
    assert.equal(panel.style.cssText, 'original');
    assert.equal(title.style.color, 'original');
});
