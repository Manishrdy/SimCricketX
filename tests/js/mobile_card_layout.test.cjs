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

test('team editor cards keep actions on the identity row, stack FC ratings, and wrap names', () => {
    const source = read('templates/team_create.html');
    assert(source.includes('flex-wrap: wrap'));
    assert(source.includes('grid-template-columns: 1fr;'));
    assert(source.includes('minmax(min(100%, 180px), 1fr)'));
    const nameRule = source.slice(source.indexOf('.tc-player-name {'), source.indexOf('.tc-player-meta {'));
    assert.equal(nameRule.includes('nowrap'), false);
    const card = source.slice(source.indexOf('title="Drag to reorder'));
    assert(card.includes('<div class="tc-player-top">'));
    // Slim card: actions ride the identity row; only FC ratings take a second
    // line, and the row still wraps rather than overflowing on narrow phones.
    assert(card.indexOf('tc-player-actions') < card.indexOf('${fcRatings}'));
    assert(card.indexOf('tc-player-actions') > card.indexOf('tc-player-top'));
    assert(card.indexOf('${fcRatings}') < card.indexOf('</div>`;'));
    assert(card.includes('data-roster-action="remove"'));
    const topRule = source.slice(source.indexOf('.tc-player-top {'), source.indexOf('.tc-player-actions {'));
    assert(topRule.includes('flex-wrap: wrap'));
});

test('squad pills are one slim row, with thumb targets only on narrow screens', () => {
    const source = read('templates/match_setup.html');
    const pill = source.slice(source.indexOf('.player-pill {'), source.indexOf('.player-pill:hover'));
    assert(pill.includes('padding: 0.3rem 0.5rem;'));
    // Actions ride the identity row; margin-left:auto parks them at the end.
    const actions = source.slice(source.indexOf('.player-pill-actions {'),
        source.indexOf('.player-pill-actions button {'));
    assert(actions.includes('margin-left: auto;'));
    assert.equal(actions.includes('flex-basis: 100%'), false);
    // The full-width 44px action row is now scoped to narrow viewports only.
    const mobile = source.slice(source.indexOf('@media (max-width: 640px) {',
        source.indexOf('.player-pill-actions {')));
    assert(mobile.includes('flex-basis: 100%;'));
    assert(mobile.includes('min-height: 44px;'));
    // display:inline-flex on the buttons would otherwise defeat [hidden],
    // which is how reserve pills drop their reorder arrows.
    assert(source.includes('.player-pill-actions button[hidden] { display: none; }'));
});

test('squad pill keeps the full player name reachable when it ellipsizes', () => {
    const source = read('templates/match_setup.html');
    assert(source.includes('<span class="player-pill-name" title="${safeName}">'));
    // Short visible label, full phrase in the accessible name and tooltip.
    assert(source.includes("move.textContent = inXI ? 'To reserve' : 'To XI';"));
    assert(source.includes("const moveAction = inXI ? 'Move to reserve' : 'Add to XI';"));
    assert(source.includes('`${moveAction}: ${pill.dataset.name}`'));
});

test('the pool column is the wider half of the builder split', () => {
    const source = read('templates/team_create.html');
    const builder = source.slice(source.indexOf('.tc-builder {'), source.indexOf('.tc-panel {'));
    // Pool rows are pure identity; the roster's narrower column still has to
    // clear its order number, three actions and the FC rating strip.
    assert(builder.includes('grid-template-columns: 1.18fr 1fr;'));
    assert(builder.includes('grid-template-columns: 1fr;'));  // single column under 900px
});

test('touch drag still resolves a handle to its draggable card', () => {
    const source = read('templates/team_create.html');
    // player_touch_drag.js does handle.closest('[draggable="true"]'), so every
    // grip must stay inside a card that carries the attribute. Moving the
    // actions onto the identity row must not break that chain.
    const roster = source.slice(source.indexOf('title="Drag to reorder'));
    const rosterCard = roster.slice(0, roster.indexOf('</div>`;'));
    assert(rosterCard.includes('class="tc-grip player-touch-handle"'));
    const pool = source.slice(source.indexOf('data-pool-id="${escHtml(p.id)}"'));
    const poolCard = pool.slice(0, pool.indexOf('</div>`;'));
    assert(poolCard.includes('player-touch-handle'));
    // The FC rating strip opts out of dragging so the inputs stay usable.
    assert(source.includes('<div class="tc-fc-ratings" draggable="false"'));
});

test('slim player cards stay shorter than the old stacked card', () => {
    const source = read('templates/team_create.html');
    const cardRule = source.slice(source.indexOf('.tc-player {'), source.indexOf('.tc-player:hover'));
    // The card used to be 0.55rem/0.8rem padding with a 0.45rem row gap.
    assert(cardRule.includes('padding: 0.3rem 0.5rem;'));
    assert(cardRule.includes('gap: 0.3rem;'));
    // Outer card stays a column so a narrow viewport can stack it.
    assert(cardRule.includes('flex-direction: column;'));
    // FC ratings put the label beside the input instead of above it.
    const ratingRule = source.slice(source.indexOf('.tc-fc-rating {'), source.indexOf('.tc-fc-rating label'));
    assert(ratingRule.includes('flex-direction: row;'));
});
