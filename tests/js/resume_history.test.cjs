const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const detail = fs.readFileSync(path.join(__dirname, '../../static/js/match_detail.js'), 'utf8');
const part = (from, to) => detail.slice(detail.indexOf(from), detail.indexOf(to, detail.indexOf(from)));

function setup(currentMainView = 'commentary') {
    const refreshed = [];
    const c = {
        currentMainView,
        refreshDashboard: (...args) => refreshed.push(args),
        renderOverFlow() {}, renderThisOverBalls() {},
    };
    vm.createContext(c);
    vm.runInContext(`let ballHistory = [], overRuns = [], currentOverBalls = [], innings1Data = null,
        thisOverBallResults = [], completedOverTotals = [], currentOverRunsAccum = 0;`, c);
    vm.runInContext(part('function restoreDeliveryHistory', '// ── MATCH RESUME LOGIC'), c);
    // Plain copies: objects built inside the vm context have foreign prototypes.
    const get = name => JSON.parse(JSON.stringify(vm.runInContext(name, c)));
    return {c, get, refreshed};
}
// Two six-ball overs of innings 1 (8 and 6 runs), then 1.2 overs of the chase.
const ball = (innings, over, ball, runs) => ({innings, over, ball, runs, delivery_id: `${innings}:${over}.${ball}`});
const inn1 = [...Array(6)].map((_, i) => ball(1, 0, i, i < 4 ? 2 : 0))
    .concat([...Array(6)].map((_, i) => ball(1, 1, i, 1)));
const inn2 = [...Array(6)].map((_, i) => ball(2, 0, i, 1)).concat([ball(2, 1, 0, 4), ball(2, 1, 1, 0)]);

test('nothing to restore leaves the live state alone', () => {
    const {c, get} = setup();
    assert.equal(c.restoreDeliveryHistory(undefined, {innings: 1}), false);
    assert.equal(c.restoreDeliveryHistory([], {innings: 1}), false);
    assert.equal(get('innings1Data'), null);
});

test('mid-chase refresh restores both innings and the over strips', () => {
    const {c, get, refreshed} = setup('matchcenter');
    assert.equal(c.restoreDeliveryHistory([...inn1, ...inn2], {innings: 2, current_over: 1}), true);
    assert.deepEqual(get('ballHistory'), inn2);
    assert.deepEqual(get('innings1Data').ballHistory, inn1);
    assert.deepEqual(get('completedOverTotals'), [{over: 0, runs: 6}]);
    assert.deepEqual(get('thisOverBallResults').map(b => b.runs), [4, 0]);
    assert.equal(get('currentOverRunsAccum'), 4);
    assert.equal(refreshed.length, 1);
    assert.equal(refreshed[0][0], vm.runInContext('ballHistory', c));
    assert.equal(refreshed[0][2], vm.runInContext('innings1Data', c));
});

test('innings-break refresh keeps innings 1 for the pending innings-end card to file', () => {
    // state.innings has already moved to 2 and the over counter reset.
    const {c, get, refreshed} = setup();
    c.restoreDeliveryHistory(inn1, {innings: 2, current_over: 0});
    assert.deepEqual(get('ballHistory'), inn1);
    assert.equal(get('innings1Data'), null);
    assert.deepEqual(get('completedOverTotals'), [{over: 0, runs: 8}, {over: 1, runs: 6}]);
    assert.equal(refreshed.length, 0);

    // The innings_end branch of _processBallResult moves it into the overlay slot.
    const filing = part('innings1Data = { ballHistory: [...ballHistory]', 'if (typeof resetDashboardForNewInnings');
    vm.runInContext(filing, c);
    assert.deepEqual(get('innings1Data').ballHistory, inn1);
    assert.deepEqual(get('ballHistory'), []);
});

test('resume uses the six-ball ledger and only falls back to outcome tokens without one', () => {
    const resume = detail.slice(detail.indexOf('// ── MATCH RESUME LOGIC'));
    assert.match(resume, /IS_HUNDRED_MATCH \? state\.hundred_deliveries : state\.deliveries/);
    assert.match(resume, /!restoredHistory && state\.current_over_outcomes/);
});
