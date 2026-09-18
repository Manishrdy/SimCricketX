const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, '../..', file), 'utf8');
const adapter = read('static/js/safe_storage.js');
const scripts = file => [...read(file).matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
function setup(failure) {
    const stored = new Map([['theme', 'dark']]);
    const storage = Object.fromEntries(['getItem', 'setItem', 'removeItem'].map(method => [method, (key, value) => {
        if (failure === method || failure === 'all') throw Error('SecurityError');
        if (method === 'getItem') return stored.get(key) ?? null;
        if (method === 'setItem') stored.set(key, value);
        if (method === 'removeItem') stored.delete(key);
    }]));
    function node() {
        const listeners = {}, attrs = {}, classes = new Set();
        return { listeners, style: {}, value: '', textContent: '',
            addEventListener(event, fn) { listeners[event] = fn; },
            setAttribute(key, value) { attrs[key] = value; }, getAttribute(key) { return attrs[key]; },
            querySelector() { return this.icon ||= node(); },
            classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) }
        };
    }
    const nodes = {}, card = node(), link = node(); card.textContent = 'Alpha'; card.setAttribute('data-code', 'ALP');
    const document = { ...node(), documentElement: node(), body: node(), title: 'Teams',
        getElementById: id => nodes[id] ||= node(),
        querySelectorAll: selector => selector === '.glass-card[data-code]' ? [card] : selector === '.main-content a[href]' ? [link] : []
    };
    const window = { ...node(), setInterval() {}, location: { search: '?clear_team_draft=1&clear_team_edit_draft=ALP', href: 'https://example.test/teams?clear_team_draft=1&clear_team_edit_draft=ALP', hash: '' }, history: { replaceState() {} } };
    Object.defineProperty(window, 'localStorage', { get() { if (failure === 'getter') throw Error('SecurityError'); return storage; } });
    const context = vm.createContext({ window, document, URL, URLSearchParams, setTimeout() {} });
    vm.runInContext(adapter, context);
    return { context, window, document, nodes, card, link, stored };
}
for (const failure of ['getter', 'getItem', 'setItem', 'removeItem', 'all']) {
    test(`storage ${failure} failure: memory remains consistent`, () => {
        const s = setup(failure), storage = s.window.scxStorage;
        storage.getItem('theme'); storage.setItem('theme', 'light');
        storage.removeItem('theme'); assert.equal(storage.getItem('theme'), null);
        storage.setItem('theme', 'dark'); assert.equal(storage.getItem('theme'), 'dark');
    });
    test(`storage ${failure} failure: team filtering, editing, navigation and modal work`, () => {
        const s = setup(failure);
        for (const script of scripts('templates/manage_teams.html')) vm.runInContext(script, s.context);
        const search = s.nodes['team-search'];
        search.value = 'missing'; search.listeners.input(); assert.equal(s.card.style.display, 'none');
        search.value = 'alpha'; search.listeners.input(); assert.equal(s.card.style.display, '');
        s.nodes['theme-toggle'].onclick();
        assert.equal(s.nodes['theme-toggle'].icon.className, s.document.documentElement.getAttribute('data-theme') === 'dark' ? 'fa-solid fa-sun' : 'fa-solid fa-moon');
        s.context.confirmDelete('ALP'); assert(s.nodes.confirmModal.classList.contains('active'));
        s.document.listeners.keydown({ key: 'Escape' }); assert(!s.nodes.confirmModal.classList.contains('active'));
        s.card.listeners.click({ target: { closest: () => null } }); assert.equal(s.window.location.href, '/team/ALP/edit');
        s.link.href = '/home'; s.link.listeners.click.call(s.link); assert.equal(s.link.style.opacity, '0.7');
    });
    test(`storage ${failure} failure: shared theme and changelog initialize and dismiss`, () => {
        const s = setup(failure);
        const theme = scripts('templates/layout.html').find(script => script.includes('// Theme Logic'));
        vm.runInContext(theme, s.context);
        s.nodes['theme-btn'].listeners.click(); s.window.toggleThemeFromDropdown();
        const home = scripts('templates/home.html').at(-1).replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '1');
        vm.runInContext(home, s.context); s.document.listeners.DOMContentLoaded();
        assert.equal(s.nodes['changelog-backdrop'].style.display, 'flex');
        s.nodes['changelog-close-btn'].listeners.click();
        assert.equal(s.nodes['changelog-backdrop'].style.display, 'none');
        assert.equal(s.window.scxStorage.getItem('scx_seen_version'), '1');
    });
}
test('available storage persists values and adapter loads before page scripts', () => {
    const s = setup(); s.window.scxStorage.setItem('key', 'value'); assert.equal(s.stored.get('key'), 'value');
    s.window.scxStorage.removeItem('key'); assert(!s.stored.has('key'));
    for (const file of ['templates/layout.html', 'templates/manage_teams.html']) {
        const source = read(file); assert(source.indexOf('js/safe_storage.js') < source.indexOf('<script>'));
    }
});
