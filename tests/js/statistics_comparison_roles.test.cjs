const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../templates/statistics.html'), 'utf8');
const extract = name => source.slice(source.indexOf(`function ${name}(`), source.indexOf('\n}', source.indexOf(`function ${name}(`)) + 2);
for (const format of ['T20', 'ListA', 'FC']) test(`${format}: wicketkeepers are selectable for batting, not bowling`, async () => {
    const nodes = {};
    for (const id of ['player1-select', 'player2-select', 'compare-btn']) nodes[id] = {
        value: '', options: [], set innerHTML(_) { this.options = []; }, appendChild(option) { this.options.push(option); }
    };
    const roles = ['Batsman', 'All-rounder', 'Wicketkeeper', 'Bowler'];
    const urls = [];
    const context = { allPlayers: [], currentRole: 'batsman', withListALength: url => url, URLSearchParams,
        window: { location: { search: '?match_format=' + format } },
        document: { getElementById: id => nodes[id], createElement: () => ({}) },
        fetch: async url => { urls.push(url); return { json: async () => ({ success: true,
            available_players: roles.map((role, id) => ({ id: String(id), name: role, role, team: format })) }) }; }
    };
    vm.createContext(context);
    for (const name of ['loadPlayersForComparison', 'filterPlayersByRole', 'updateCompareButton']) vm.runInContext(extract(name), context);
    context.loadPlayersForComparison(); await new Promise(setImmediate);
    assert.equal(new URL(urls[0], 'https://example.test').searchParams.get('match_format'), format);
    for (const id of ['player1-select', 'player2-select']) assert.deepEqual(nodes[id].options.map(o => o.value), ['0', '1', '2']);
    nodes['player1-select'].value = '2'; nodes['player2-select'].value = '0'; context.updateCompareButton();
    assert.equal(nodes['compare-btn'].disabled, false);
    context.filterPlayersByRole('bowler');
    assert.deepEqual(nodes['player1-select'].options.map(o => o.value), ['1', '3']);
});
