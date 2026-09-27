const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../static/js/match_detail.js'), 'utf8');
const banner = source.slice(source.indexOf('function updateScoreBanner(data)'), source.indexOf('function renderOverFlow()'));
const resume = source.slice(source.indexOf('// ── MATCH RESUME LOGIC'));

async function restore(overrides = {}, format = 'Hundred') {
    const elements = new Map();
    const createElement = () => ({style: {}, dataset: {}, hidden: false, textContent: '',
        appendChild(child) { elements.set(child.id, child); }});
    for (const id of ['sb-score', 'sb-overs', 'sb-phase', 'spin-toss']) {
        const element = createElement();
        element.parentElement = createElement();
        elements.set(id, element);
    }
    let onReady;
    const requests = [];
    const state = {status: 'in_progress', score: 30, wickets: 1, current_over: 5,
        current_ball: 0, innings: 1, total_overs: 20, phase_name: 'Middle',
        legal_balls: 25, innings_ball_limit: 100,
        timeout_active: false, timeout_available: true,
        striker: {}, non_striker: {}, current_bowler: {}, ...overrides};
    const context = {document: {getElementById: id => elements.get(id), createElement,
        addEventListener: (event, callback) => { onReady = callback; }, querySelector: () => null},
        window: {resumeMode: true, location: {pathname: '/match/test'}},
        matchData: {match_id: 'test', overs: 20}, simulationMode: 'manual',
        IS_HUNDRED_MATCH: format === 'Hundred', BALLS_PER_SET: format === 'Hundred' ? 5 : 6,
        LIVE_RATE_UNIT: format === 'Hundred' ? 1 : 6, getMatchFormat: () => format,
        appendLog() {}, console: {error(error) { throw error; }},
        fetch: async (url, options) => {
            requests.push({url, options});
            return {ok: true, json: async () => options
                ? {timeout_active: false, timeout_available: false} : state};
        }};
    vm.runInNewContext(banner + resume, context);
    onReady();
    await new Promise(setImmediate);
    return {elements, requests};
}

test('refresh at 25 balls restores the available timeout and ends powerplay', async () => {
    const {elements} = await restore();
    assert.equal(elements.get('sb-phase').textContent, '');
    assert.equal(elements.get('sb-overs').textContent, '25/100 balls');
    assert.equal(elements.get('hundred-timeout').hidden, false);
    assert.equal(elements.get('hundred-timeout').textContent, 'Strategic timeout');
});

test('refresh during timeout restores a working resume button', async () => {
    const {elements, requests} = await restore({timeout_active: true, timeout_available: false});
    const button = elements.get('hundred-timeout');
    assert.equal(button.hidden, false);
    assert.equal(button.textContent, 'Resume play');
    assert.equal(button.dataset.active, 'true');
    await button.onclick();
    assert.equal(requests.at(-1).url, '/match/test/strategic-timeout');
    assert.deepEqual(JSON.parse(requests.at(-1).options.body), {active: false});
    assert.equal(button.hidden, true);
    assert.equal(button.disabled, false);
});

test('refresh before powerplay ends preserves powerplay and hides timeout', async () => {
    const {elements} = await restore({current_over: 4, current_ball: 4, legal_balls: 24,
        phase_name: 'Powerplay', timeout_available: false});
    assert.equal(elements.get('sb-phase').textContent, 'POWERPLAY');
    assert.equal(elements.get('hundred-timeout').hidden, true);
});

test('refresh preserves authoritative ball counts and revised innings limit', async () => {
    const {elements} = await restore({legal_balls: 26, innings_ball_limit: 65});
    assert.equal(elements.get('sb-overs').textContent, '26/65 balls');
});

test('refresh retains explicit null phases and FC legacy fallback', async () => {
    for (const [phase_name, format] of [[null, 'Hundred'], [undefined, 'FC']]) {
        const {elements} = await restore({phase_name}, format);
        assert.equal(elements.get('sb-phase').textContent, '');
    }
});

test('refresh preserves ListA server phases and legacy T20 fallback', async () => {
    const listA = await restore({phase_name: 'PP1'}, 'ListA');
    assert.equal(listA.elements.get('sb-phase').textContent, 'POWERPLAY (PP1)');
    const t20 = await restore({phase_name: undefined}, 'T20');
    assert.equal(t20.elements.get('sb-phase').textContent, 'POWERPLAY');
});
