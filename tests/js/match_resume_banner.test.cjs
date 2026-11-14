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
    const sidebarIds = ['status', 'toss', 'players', 'striker', 'non-striker', 'bowler', 'next']
        .map(name => 'live-innings-' + name);
    for (const id of ['sb-score', 'sb-overs', 'sb-phase', 'spin-toss', ...sidebarIds]) {
        const element = createElement();
        element.parentElement = createElement();
        elements.set(id, element);
    }
    let onReady;
    const requests = [];
    const resumed = [];
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
        appendLog() {}, console: {error(message, error) { throw error; }},
        scheduleNextBall: delay => resumed.push(delay),
        showDecisionModal: decision => resumed.push(decision),
        _processBallResult: card => resumed.push(card),
        fetch: async (url, options) => {
            requests.push({url, options});
            return {ok: true, json: async () => options
                ? {timeout_active: false, timeout_available: false} : state};
        }};
    vm.runInNewContext(banner + resume, context);
    onReady();
    await new Promise(setImmediate);
    return {elements, requests, resumed, render: context.renderLiveInnings};
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

test('live innings restores roles, displays pending choices, and clears at a break', async () => {
    const live = {status: 'live', innings: 2, batting_team: 'Away',
        striker: 'Batter B', non_striker: 'Batter A', bowler: 'Bowler A', next_batter: 'Batter C'};
    const {elements, render} = await restore({live_innings: live}, 'T20');
    const row = name => elements.get('live-innings-' + name);
    assert.equal(row('toss').hidden, true);
    assert.equal(row('status').textContent, 'Innings 2 · Away batting');
    assert.equal(row('striker').textContent, 'Batter B');
    assert.equal(row('non-striker').textContent, 'Batter A');
    assert.equal(row('bowler').textContent, 'Bowler A');
    assert.equal(row('next').textContent, 'Batter C');

    render({...live, selecting_batter: 'non_striker'});
    assert.equal(row('striker').textContent, 'Batter B');
    assert.equal(row('non-striker').textContent, 'Selecting batter…');
    render({...live, selecting_bowler: true, next_batter: null});
    assert.equal(row('bowler').textContent, 'Selecting bowler…');
    assert.equal(row('next').textContent, 'No batters left');

    render({status: 'break'});
    assert.equal(row('status').textContent, 'Innings break');
    assert.equal(row('players').hidden, true);
    for (const name of ['striker', 'non-striker', 'bowler', 'next']) {
        assert.equal(row(name).textContent, '—');
    }
    render({...live, innings: 3});
    assert.equal(row('players').hidden, false);
    assert.equal(row('status').textContent, 'Innings 3 · Away batting');
    render({status: 'complete'});
    assert.equal(row('players').hidden, true);
    assert.equal(row('status').textContent, 'Match complete');
});

test('reload during a break restores the card before play advances', async () => {
    const card = {innings_end: true, innings_number: 1, scorecard_data: {}, commentary: 'End of innings'};
    const {elements, resumed} = await restore({
        innings: 2, live_innings: {status: 'break'}, pending_card: {payload: card},
    }, 'T20');
    assert.equal(elements.get('live-innings-status').textContent, 'Innings break');
    assert.equal(elements.get('live-innings-players').hidden, true);
    assert.equal(resumed.length, 1);
    assert.equal(resumed[0].scorecard_data, card.scorecard_data);
    assert.equal(resumed[0].commentary, '');
});

test('reload restores a pending manual selection before play advances', async () => {
    const decision = {decision_type: 'next_bowler'};
    const {resumed} = await restore({pending_selection: decision}, 'T20');
    assert.deepEqual(resumed, [decision]);
});
