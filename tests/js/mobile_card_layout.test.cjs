const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

test('card grids use a fluid min so 320px viewports are not forced wider', () => {
    const grids = {
        'templates/manage_teams.html': 'minmax(min(100%, 340px), 1fr)',
        'templates/scenarios.html': 'minmax(min(100%, 340px), 1fr)',
        'templates/my_analytics.html': 'minmax(min(100%, 300px), 1fr)',
        'templates/match_setup.html': 'minmax(min(100%, 270px), 1fr)'
    };
    for (const [file, expected] of Object.entries(grids)) {
        const source = read(file);
        assert(source.includes(expected), `${file} should use ${expected}`);
        assert.equal(source.includes('minmax(340px, 1fr)'), false, file);
        assert.equal(source.includes('minmax(300px, 1fr)'), false, file);
        assert.equal(source.includes('minmax(270px, 1fr)'), false, file);
    }
});

test('team editor cards stack identity, FC ratings and actions, and wrap names', () => {
    const source = read('templates/team_create.html');
    assert(source.includes('flex-wrap: wrap'));
    assert(source.includes('grid-template-columns: 1fr;'));
    assert(source.includes('minmax(min(100%, 180px), 1fr)'));
    const nameRule = source.slice(source.indexOf('.tc-player-name {'), source.indexOf('.tc-player-meta {'));
    assert.equal(nameRule.includes('nowrap'), false);
    const card = source.slice(source.indexOf('title="Drag to reorder'));
    assert(card.includes('<div class="tc-player-top">'));
    assert(card.indexOf('tc-player-top') < card.indexOf('${fcRatings}'));
    assert(card.indexOf('${fcRatings}') < card.indexOf('tc-player-actions'));
    assert(card.includes('data-roster-action="remove"'));
});
