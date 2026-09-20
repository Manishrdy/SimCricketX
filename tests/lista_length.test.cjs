const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync(require('node:path').join(__dirname, '../static/js/lista_length.js'), 'utf8');

function page(pathname) {
    const location = new URL('https://example.test' + pathname);
    let refreshes = 0;
    const selected = { value: 'retained-player-selection' };
    const context = {
        URL, URLSearchParams, location,
        history: { replaceState: (_, __, url) => { location.href = url; } },
        document: {
            addEventListener: () => {},
            querySelectorAll: () => [],
            getElementById: id => id === 'cmp-refresh' ? { click: () => refreshes++ } : selected,
        },
    };
    context.window = context;
    vm.createContext(context); vm.runInContext(code, context);
    return { context, location, selected, refreshes: () => refreshes };
}

test('comparison filters refresh selected players without reloading the page', () => {
    const p = page('/compare-players');
    p.context.setListALength('40');
    assert.equal(p.location.searchParams.get('scheduled_overs'), '40');
    assert.equal(p.refreshes(), 1);
    assert.equal(p.selected.value, 'retained-player-selection');
    assert.equal(p.context.withListALength('/api/compare-players?identity_ids=1,2'),
                 '/api/compare-players?identity_ids=1%2C2&scheduled_overs=40');
    p.context.setListALength('');
    assert.equal(p.context.withListALength('/statistics?match_format=ListA&scheduled_overs=40'),
                 '/statistics?match_format=ListA');
});

test('server-rendered filters preserve scope and clear pagination', () => {
    const p = page('/statistics?match_format=ListA&view=tournament&tournament_id=7&page=3');
    p.context.setListALength('50');
    assert.equal(p.location.searchParams.get('tournament_id'), '7');
    assert.equal(p.location.searchParams.get('scheduled_overs'), '50');
    assert.equal(p.location.searchParams.has('page'), false);
});
