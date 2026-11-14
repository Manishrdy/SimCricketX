const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/match_dashboard.js', 'utf8');

function helpers(context = {}) {
    const c = {IS_HUNDRED_MATCH: false, ...context};
    vm.runInNewContext(source.slice(source.indexOf('function derivePlayerStats'), source.indexOf('function renderBatterCard')), c);
    return c;
}
const ball = (overrides = {}) => ({striker: 'A', bowler: 'B', over: 0, ball: 0, runs: 0, ...overrides});

test('legacy six-ball history credits no-ball bat runs, boundaries and faced balls', () => {
    const c = helpers();
    const h = [
        ball({runs: 5, is_extra: true, extra_type: 'No Ball'}),
        ball({runs: 2, is_extra: true, extra_type: 'Leg Bye'}),
        ball({runs: 4, is_extra: true, extra_type: 'Byes'}),
        ball({runs: 1, is_extra: true, extra_type: 'Wide'}),
        ball({runs: 6}),
    ];
    const a = c.derivePlayerStats(h, 'A');
    assert.equal(a.runs, 10); assert.equal(a.balls, 4);
    assert.equal(a.fours, 1); assert.equal(a.sixes, 1);
    assert.equal(a.sr, '250.0');
});

test('legacy six-ball bowler figures exclude byes, leg byes and run-outs', () => {
    const c = helpers();
    const h = [
        ball({runs: 4, is_extra: true, extra_type: 'Byes'}),
        ball({runs: 1, is_extra: true, extra_type: 'Leg Bye'}),
        ball({runs: 1, is_extra: true, extra_type: 'Wide'}),
        ball({runs: 5, is_extra: true, extra_type: 'No Ball'}),
        ball({runs: 1, batter_out: true, wicket_type: 'Run Out'}),
        ball({runs: 0, batter_out: true, wicket_type: 'Bowled'}),
        ball({runs: 2}),
    ];
    const b = c.deriveBowlerStats(h, 'B');
    assert.equal(b.runs, 9); assert.equal(b.wickets, 1);
    assert.equal(b.overs, '0.5');
});

test('engine per-ball figures take precedence over inference', () => {
    const c = helpers();
    const h = [ball({runs: 5, is_extra: true, extra_type: 'No Ball', batting_runs: 4, batter_balls: 1,
        batter_fours: 1, batter_sixes: 0, bowler_runs: 5, bowler_balls: 0, bowler_wicket: false}),
    ball({runs: 1, batter_out: true, wicket_type: 'Run Out', batting_runs: 1, batter_balls: 1,
        batter_fours: 0, batter_sixes: 0, bowler_runs: 1, bowler_balls: 1, bowler_wicket: false})];
    const a = c.derivePlayerStats(h, 'A');
    assert.equal(a.runs, 5); assert.equal(a.balls, 2); assert.equal(a.fours, 1);
    const b = c.deriveBowlerStats(h, 'B');
    assert.equal(b.runs, 6); assert.equal(b.wickets, 0); assert.equal(b.overs, '0.1');
});

function worm(context) {
    let chart;
    const c = {IS_HUNDRED_MATCH: false, ...context,
        document: {getElementById: () => ({getContext: () => ({})})},
        Chart: class { constructor(ctx, config) { this.data = config.data; this.options = config.options; chart = this; } update() {} }};
    vm.runInNewContext('let wormChart = null; function _resizeChartToParent() {}\n'
        + source.slice(source.indexOf('function _buildWormSeries'), source.indexOf('function _updateManhattan'))
        + source.slice(source.indexOf('function _wormAxisLimit'), source.indexOf('/** Size canvas')), c);
    return {c, get chart() { return chart; }};
}

test('List A worm axis and target line span all 50 overs', () => {
    const w = worm();
    const inn1 = {ballHistory: [ball({over: 49, ball: 5, runs: 1, innings_over_limit: 50})]};
    w.c._rebuildWorm([ball({over: 30, runs: 4, target: 280, innings_over_limit: 50})], inn1);
    assert.equal(w.chart.options.scales.x.max, 50);
    assert.equal(w.chart.data.datasets.find(d => d.label === 'Target').data[1].x, 50);
});

test('worm axis falls back to scheduled overs and never clips bowled overs', () => {
    const w = worm({matchData: {overs: 50}});
    w.c._rebuildWorm([ball({over: 2})], null);
    assert.equal(w.chart.options.scales.x.max, 50);
    const t20 = worm();
    t20.c._rebuildWorm([ball({over: 0})], null);
    assert.equal(t20.chart.options.scales.x.max, 20);
    const legacy = worm();
    legacy.c._rebuildWorm([ball({over: 34})], null);
    assert.equal(legacy.chart.options.scales.x.max, 35);
});
