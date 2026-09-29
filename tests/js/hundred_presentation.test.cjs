const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/match_dashboard.js', 'utf8');
function helpers(hundred = true) {
    const context = {IS_HUNDRED_MATCH: hundred};
    const stats = source.slice(source.indexOf('function derivePlayerStats'), source.indexOf('function renderBatterCard'));
    const worm = source.slice(source.indexOf('function _buildWormSeries'), source.indexOf('function _updateManhattan'));
    vm.runInNewContext(stats + worm, context);
    return context;
}
const event = (overrides = {}) => ({striker: 'A', bowler: 'B', over: 0, ball: 0,
    runs: 0, batting_runs: 0, batter_balls: 1, batter_fours: 0, batter_sixes: 0,
    bowler_runs: 0, bowler_wicket: false, is_legal: true, ...overrides});

test('Hundred incremental figures exclude byes and run-out wickets', () => {
    const c = helpers();
    const history = [event({runs: 2, extra_type: 'Byes', is_extra: true}),
        event({runs: 1, batting_runs: 1, bowler_runs: 1, batter_out: true})];
    const bowler = c.deriveBowlerStats(history, 'B');
    assert.equal(bowler.runs, 1); assert.equal(bowler.wickets, 0);
    assert.equal(bowler.overs, '2'); assert.equal(bowler.econ, '0.50');
    const batter = c.derivePlayerStats(history, 'A');
    assert.equal(batter.balls, 2); assert.equal(batter.runs, 1);
});

test('authoritative totals survive a partial or legacy history', () => {
    const c = helpers();
    const history = [event({batter_totals: {A: {runs: 24, balls: 13, fours: 3, sixes: 1}},
        bowler_totals: {B: {runs: 18, wickets: 2, balls_bowled: 20}}})];
    assert.equal(c.derivePlayerStats(history, 'A').runs, 24);
    assert.equal(c.deriveBowlerStats(history, 'B').econ, '0.90');
    assert.equal(c.deriveBowlerStats(history, 'B').wickets, 2);
});

test('no-ball bat runs and faced ball are explicit', () => {
    const c = helpers();
    const h = [event({runs: 5, batting_runs: 4, batter_fours: 1, bowler_runs: 5,
        is_extra: true, extra_type: 'No Ball', is_legal: false})];
    const a = c.derivePlayerStats(h, 'A');
    assert.equal(a.runs, 4); assert.equal(a.balls, 1); assert.equal(a.fours, 1);
    const b = c.deriveBowlerStats(h, 'B');
    assert.equal(b.overs, '0'); assert.equal(b.econ, '—');
});

test('unavailable legacy components remain unavailable', () => {
    const c = helpers();
    const h = [{striker: 'A', bowler: 'B', runs: 4, is_extra: true}];
    assert.equal(c.derivePlayerStats(h, 'A').runs, '—');
    assert.equal(c.deriveBowlerStats(h, 'B').runs, '—');
});

test('worm extras increase runs without moving legal-ball position', () => {
    const c = helpers();
    const h = [event({runs: 1, legal_balls_after: 1}),
        event({runs: 1, legal_balls_after: 1, is_legal: false}),
        event({runs: 4, legal_balls_after: 2})];
    assert.deepEqual(Array.from(c._buildWormSeries(h).path, p => [p.x, p.y]), [[0, 0], [1, 1], [1, 2], [2, 6]]);
});

test('six-ball-format figure units remain unchanged', () => {
    const c = helpers(false);
    const h = Array.from({length: 6}, () => ({striker: 'A', bowler: 'B', runs: 1}));
    assert.equal(c.deriveBowlerStats(h, 'B').overs, '1.0');
    assert.equal(c.deriveBowlerStats(h, 'B').econ, '6.0');
});

test('Hundred worm axis and target use the full ball allocation and update after rain', () => {
    const c = helpers();
    let chart;
    c.document = {getElementById: () => ({getContext: () => ({})})};
    c.Chart = class {
        constructor(ctx, config) { this.data = config.data; this.options = config.options; chart = this; }
        update() {}
    };
    const rebuild = source.slice(source.indexOf('function _rebuildWorm'), source.indexOf('/** Size canvas'));
    vm.runInNewContext('let wormChart = null; function _resizeChartToParent() {}\n' + rebuild, c);
    const first = {ballHistory: [event({legal_balls_after: 100, innings_ball_limit: 100})]};
    c._rebuildWorm([event({target: 144, legal_balls_after: 84, innings_ball_limit: 100})], first);
    assert.equal(chart.options.scales.x.max, 100);
    assert.equal(chart.data.datasets.find(d => d.label === 'Target').data[1].x, 100);
    c._rebuildWorm([event({legal_balls_after: 25, innings_ball_limit: 50})], null);
    assert.equal(chart.options.scales.x.max, 50);
});
