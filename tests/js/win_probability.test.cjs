const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../..');
const dashboard = fs.readFileSync(path.join(root, 'static/js/match_dashboard.js'), 'utf8');
const detail = fs.readFileSync(path.join(root, 'static/js/match_detail.js'), 'utf8');

function setup() {
    const ids = ['win-prob-display', 'win-prob-container', 'win-prob-chasing-bar', 'win-prob-defending-bar',
        'wp-chasing-pct', 'wp-defending-pct', 'wp-chasing-name', 'wp-defending-name', 'sb-bat-name',
        'over-timeline', 'striker-card', 'non-striker-card', 'bowler-card', 'partnership-bar',
        'latest-ball-ticker', 'wagon-wheel-svg'];
    const els = {};
    for (const id of ids) els[id] = {innerHTML: '', textContent: '', style: {}, querySelectorAll: () => []};
    els['win-prob-display'].innerHTML = '<server meter>';
    els['sb-bat-name'].textContent = 'HOM';
    const c = {matchData: {team_home: 'HOM_1', team_away: 'AWY_1'},
        document: {getElementById: id => els[id] || null}};
    vm.createContext(c);
    vm.runInContext('let wagonViewMode = "current"; let _timelineRenderedCount = 0;', c);
    const part = (src, from, to) => src.slice(src.indexOf(from), src.indexOf(to, src.indexOf(from)));
    vm.runInContext(part(dashboard, 'function resetDashboardForNewInnings', 'function updateWagonViewControls'), c);
    vm.runInContext(part(detail, 'function updateWinProbBanner', '// ── RAIN / DLS STATUS'), c);
    const shown = () => els['win-prob-container'].style.display;
    return {c, els, shown};
}

test('the dashboard no longer computes or draws its own estimate', () => {
    assert.doesNotMatch(dashboard, /updateWinProbability|win-prob-display|8\.5/);
});

test('first innings shows the server value for the batting side', () => {
    const {c, els, shown} = setup();
    c.updateWinProbBanner({innings_number: 1, match_over: false, win_probability: 61.6});
    assert.equal(shown(), 'block');
    assert.equal(els['wp-chasing-pct'].textContent, '62%');
    assert.equal(els['wp-defending-pct'].textContent, '38%');
    assert.equal(els['wp-chasing-name'].textContent, 'HOM');
    assert.equal(els['wp-defending-name'].textContent, 'AWY');
});

test('innings break hides the meter and the chase brings it back for the new batting side', () => {
    const {c, els, shown} = setup();
    c.updateWinProbBanner({innings_number: 1, win_probability: 61.6});
    c.updateWinProbBanner({innings_number: 1, innings_end: true});
    assert.equal(shown(), 'none');
    c.resetDashboardForNewInnings();
    assert.equal(els['win-prob-display'].innerHTML, '<server meter>');

    els['sb-bat-name'].textContent = 'AWY';
    c.updateWinProbBanner({innings_number: 2, win_probability: 38.4});
    assert.equal(shown(), 'block');
    assert.equal(els['wp-chasing-pct'].textContent, '38%');
    assert.equal(els['wp-chasing-name'].textContent, 'AWY');
});

test('ties, match end and FC keep the meter hidden', () => {
    for (const data of [{innings_number: 2, match_tied: true, win_probability: 50},
        {innings_number: 2, match_over: true, win_probability: 100},
        {innings_number: 3, win_probability: null}]) {
        const {c, shown} = setup();
        c.updateWinProbBanner({innings_number: 1, win_probability: 50});
        c.updateWinProbBanner(data);
        assert.equal(shown(), 'none');
    }
    const fc = setup();
    fc.c.updateWinProbBanner({innings_number: 1, win_probability: null});
    assert.notEqual(fc.shown(), 'block');
});

test('resume restores the meter from the live-state snapshot', () => {
    const resume = detail.slice(detail.indexOf('// ── MATCH RESUME LOGIC'));
    assert.match(resume, /updateWinProbBanner\(\{[\s\S]*?win_probability: state\.win_probability/);
});
