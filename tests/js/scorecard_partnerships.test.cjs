const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const detail = fs.readFileSync(path.join(__dirname, '../../static/js/match_detail.js'), 'utf8');
const part = (from, to) => detail.slice(detail.indexOf(from), detail.indexOf(to, detail.indexOf(from)));

function el() {
    const rows = [];
    return {
        style: {}, innerHTML: '', textContent: '', className: '', rows,
        insertRow() { const r = {innerHTML: '', className: ''}; rows.push(r); return r; },
    };
}

function setup(hundred = false) {
    const els = {};
    ['scorecard-fow', 'scorecard-fow-text', 'scorecard-partnerships', 'scorecard-partnerships-tbody']
        .forEach(id => { els[id] = el(); });
    const c = {document: {getElementById: id => els[id] || null}, IS_HUNDRED_MATCH: hundred};
    vm.createContext(c);
    vm.runInContext(detail.slice(detail.indexOf('function escapeHtml'), detail.indexOf('}', detail.indexOf('function escapeHtml')) + 1), c);
    vm.runInContext(part('function ordinalWicket', 'function closeScorecard'), c);
    return {c, els};
}

const stands = [
    {wicket_number: 1, batsman1_name: 'R Sharma', batsman1_contribution: 15, batsman1_balls: 10,
     batsman2_name: 'S Gill', batsman2_contribution: 8, batsman2_balls: 12, runs: 23, balls: 22,
     fours: 3, sixes: 0, is_unbroken: false},
    {wicket_number: 2, batsman1_name: '<b>X</b>', batsman1_contribution: 18, batsman1_balls: 10,
     batsman2_name: 'S Gill', batsman2_contribution: 9, batsman2_balls: 5, runs: 27, balls: 15,
     fours: null, sixes: null, is_unbroken: true},
];
const fow = [{wicket: 1, score: 23, batter: 'R Sharma', over: '3.4', wicket_type: 'Caught'}];

test('renders the FoW line and one row per stand, escaped, unbroken starred', () => {
    const {c, els} = setup();
    c.renderScorecardPartnerships({partnerships: stands, fall_of_wickets: fow});
    assert.equal(els['scorecard-fow'].style.display, 'block');
    assert.match(els['scorecard-fow-text'].innerHTML, /<strong>1-23<\/strong> \(R Sharma, 3\.4 ov\)/);
    const rows = els['scorecard-partnerships-tbody'].rows;
    assert.equal(rows.length, 2);
    assert.match(rows[0].innerHTML, /1st/);
    assert.match(rows[0].innerHTML, /R Sharma <span class="pship-share">15 \(10\)<\/span>/);
    assert.equal(rows[1].className, 'pship-unbroken');
    assert.match(rows[1].innerHTML, /27\*/);
    assert.match(rows[1].innerHTML, /&lt;b&gt;X&lt;\/b&gt;/);
    assert.doesNotMatch(rows[1].innerHTML, /<b>X<\/b>/);
    assert.match(rows[1].innerHTML, /<td>–<\/td>/);  // legacy: unknown boundaries
    assert.equal(els['scorecard-partnerships'].style.display, 'block');
});

test('hides both sections when the payload carries none (super over, old card)', () => {
    const {c, els} = setup();
    c.renderScorecardPartnerships({partnerships: stands, fall_of_wickets: fow});
    c.renderScorecardPartnerships({});
    assert.equal(els['scorecard-fow'].style.display, 'none');
    assert.equal(els['scorecard-partnerships'].style.display, 'none');
    assert.equal(els['scorecard-partnerships-tbody'].innerHTML, '');
});

test('Hundred FoW uses balls, not overs', () => {
    const {c, els} = setup(true);
    c.renderScorecardPartnerships({partnerships: stands, fall_of_wickets: [{wicket: 1, score: 23, batter: 'A', over: '17'}]});
    assert.match(els['scorecard-fow-text'].innerHTML, /\(A, 17 b\)/);
});
