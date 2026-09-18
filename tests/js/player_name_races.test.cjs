const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

for (const template of ['player_pool_form.html', 'admin/player_pool_form.html']) {
    function setup() {
        const source = fs.readFileSync(path.join(__dirname, '../../templates', template), 'utf8');
        const script = source.match(/<script>([\s\S]*?)<\/script>/)[1]
            .replace(/{{[^}]*}}/g, 'false');
        const nodes = {};
        const document = { getElementById(id) {
            return nodes[id] ||= { value: '', style: {}, classList: { toggle() {} },
                addEventListener(event, handler) { this[event] = handler; } };
        } };
        const pending = [], timers = new Map();
        let timerId = 0;
        vm.runInNewContext(script, { document,
            setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
            clearTimeout(id) { timers.delete(id); },
            fetch: () => new Promise((resolve, reject) => pending.push({
                resolve: exists => resolve({ json: () => Promise.resolve({ exists, name: 'Taken' }) }), reject
            }))
        });
        return { nodes, pending, input(value, dispatch = true) {
            nodes.nameInput.value = value;
            nodes.nameInput.input();
            if (dispatch) { for (const fn of timers.values()) fn(); timers.clear(); }
        } };
    }
    const flush = () => new Promise(setImmediate);
    test(`${template}: late duplicate cannot disable a newer unique name`, async () => {
        const s = setup(); s.input('Taken'); s.input('Unique');
        s.pending[1].resolve(false); await flush();
        const message = s.nodes.nameWarn.innerHTML;
        s.pending[0].resolve(true); await flush();
        assert.equal(s.nodes.submitBtn.disabled, false);
        assert.equal(s.nodes.nameWarn.innerHTML, message);
    });
    for (const value of ['', 'x']) test(`${template}: clearing/short input invalidates requests (${value})`, async () => {
        const s = setup(); s.input('Taken'); s.input(value);
        s.pending[0].resolve(true); await flush();
        assert.equal(s.nodes.submitBtn.disabled, false);
        assert.equal(s.nodes.nameWarn.textContent, '');
    });
    test(`${template}: stale rejection cannot enable a newer duplicate`, async () => {
        const s = setup(); s.input('Unique'); s.input('Taken');
        s.pending[1].resolve(true); await flush();
        s.pending[0].reject(Error('offline')); await flush();
        assert.equal(s.nodes.submitBtn.disabled, true);
    });
    test(`${template}: generations protect A to B to normalized A and the debounce interval`, async () => {
        const s = setup(); s.input('Taken'); s.input('Unique'); s.input(' TAKEN ', false);
        s.pending[0].resolve(true); await flush();
        assert.equal(s.nodes.submitBtn.disabled, false);
        assert.match(s.nodes.nameWarn.innerHTML, /Checking/);
    });
    test(`${template}: editing a confirmed duplicate clears its disabled state immediately`, async () => {
        const s = setup(); s.input('Taken'); s.pending[0].resolve(true); await flush();
        assert.equal(s.nodes.submitBtn.disabled, true);
        s.input('Unique', false);
        assert.equal(s.nodes.submitBtn.disabled, false);
    });
}
