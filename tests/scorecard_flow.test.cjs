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
        scheduleNextBall: () => resumed++, delay: 0,
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

test('pending captain response schedules polling without appending a ball', async () => {
    const status = { hidden: true };
    const delays = [];
    const logs = [];
    const context = vm.createContext({
        document: { getElementById: id => id === 'fc-captain-status' ? status : null },
        ballInFlight: true, renderCaptainDiscussion: () => {},
        scheduleNextBall: ms => delays.push(ms),
        appendLog: (...args) => logs.push(args),
    });
    vm.runInContext(functionSource('_processBallResult'), context);
    await context._processBallResult({ fc_decision_pending: true, retry_after: 0.5 });
    assert.equal(context.ballInFlight, false);
    assert.equal(status.hidden, false);
    assert.deepEqual(delays, [500]);
    assert.deepEqual(logs, []);
    // Rate limiting during polling retains the waiting state.
    await context._processBallResult({ rate_limited: true, retry_after: 1 });
    assert.equal(status.hidden, false);
    assert.deepEqual(delays, [500, 1000]);
    await context._processBallResult({ error: 'Decision failed' });
    assert.equal(status.hidden, true);
});


test('simulated coach and captain dialogue is escaped and shown once per decision', () => {
    const logs = [];
    const start = source.indexOf('const captainDiscussionSeen = new Set();');
    const end = source.indexOf('// All ball-result processing', start);
    const context = vm.createContext({
        appendLog: text => logs.push(text),
        escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    });
    vm.runInContext(source.slice(start, end), context);
    const coach = {id: '1:30:coach', speaker: 'Coach', text: 'We are 410/6. <script>'};
    const captain = {id: '1:30:captain', speaker: 'Captain', text: "We're declaring."};
    context.renderCaptainDiscussion({fc_captain_discussion: [coach]});
    context.renderCaptainDiscussion({fc_captain_discussion: [coach, captain]});
    context.renderCaptainDiscussion({fc_captain_discussion: [captain]});
    assert.equal(logs.length, 2);
    assert.match(logs[0], /Coach \(simulated\)/);
    assert.match(logs[0], /&lt;script&gt;/);
    assert.match(logs[1], /We're declaring/);
});
