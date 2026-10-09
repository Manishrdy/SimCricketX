const assert = require('node:assert/strict');
global.window = {};
require('../../static/js/squad_readiness.js');
const check = window.SquadReadiness.checks;
const limits = {min: 11, max: 25, wk: 1, bowl: 5};
const players = roles => roles.map(role => ({role}));
const legal = players(['Wicketkeeper', ...Array(5).fill('All-rounder'), ...Array(5).fill('Batsman')]);
assert.equal(check(legal, 'Captain', 'Keeper', limits).ready, true);
assert.equal(check(legal, '', 'Keeper', limits).rows[3].action, 'captain');
assert.equal(check(legal, 'Captain', '', limits).rows[4].action, 'keeper');
const empty = check([], '', '', limits);
assert.equal(empty.ready, false);
assert.equal(empty.rows[4].action, 'Wicketkeeper');
assert.match(empty.note, /count toward/);
const full = check(players(Array(25).fill('Batsman')), 'Captain', '', limits);
assert.match(full.note, /Replace at least 6/);
assert.equal(full.rows[2].action, 'Bowling options');
assert.equal(check(players(Array(26).fill('Batsman')), '', '', limits).rows[0].action, 'roster');
// Checking another format must not retain the previous format's readiness.
assert.equal(check([], '', '', limits).ready, false);
assert.equal(check(legal, 'Captain', 'Keeper', limits).ready, true);
console.log('Squad readiness cases passed');
// Exercise the rendered buttons and rerendering without a browser dependency.
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; }
    append(...children) { this.children.push(...children); }
    replaceChildren() { this.children = []; }
    addEventListener(event, handler) { this.listeners[event] = handler; }
}
global.document = {createElement: tag => new Element(tag), createTextNode: text => text};
const root = new Element('div');
let selected;
window.SquadReadiness.render(root, [], '', '', limits, action => { selected = action; });
const rows = root.children[1].children;
rows[2].children[1].listeners.click();
assert.equal(selected, 'Bowling options');
window.SquadReadiness.render(root, legal, 'Captain', 'Keeper', limits, () => {});
assert.match(root.children[0].textContent, /Ready to publish/);
assert.ok(root.children[1].children.every(row => row.children.length === 1));
console.log('Readiness action and rerender checks passed');
