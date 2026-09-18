const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function setup(accepted = true) {
    const document = new EventTarget();
    const window = new EventTarget();
    const frames = new Map();
    let frameId = 0;
    window.requestAnimationFrame = callback => { frames.set(++frameId, callback); return frameId; };
    window.cancelAnimationFrame = id => frames.delete(id);
    window.innerHeight = 600;
    window.getComputedStyle = node => ({ overflowY: node.overflowY || 'visible' });
    const source = new EventTarget();
    const target = new EventTarget();
    const seen = [];
    source.classList = { remove() {} };
    const handle = {
        closest: () => source,
        hasPointerCapture: () => false
    };
    document.elementFromPoint = () => target;
    source.addEventListener('dragstart', e => {
        e.dataTransfer.setData('text/plain', 'player:42');
        seen.push('start');
    });
    source.addEventListener('dragend', () => seen.push('end'));
    target.addEventListener('dragover', e => { if (accepted) e.preventDefault(); });
    target.addEventListener('drop', e => {
        seen.push(e.dataTransfer.getData('text/plain'));
        assert.equal(e.clientY, 100);
    });
    vm.runInNewContext(fs.readFileSync('static/js/player_touch_drag.js', 'utf8'), { document, window, Event });
    function pointer(type, pointerType = 'touch', pointerId = 1, y = 100) {
        const e = new Event(type, { cancelable: true });
        Object.defineProperties(e, {
            target: { value: { closest: () => handle } },
            pointerType: { value: pointerType }, pointerId: { value: pointerId },
            isPrimary: { value: true }, clientX: { value: 50 }, clientY: { value: y }
        });
        document.dispatchEvent(e);
    }
    return { pointer, seen, window, document, target, frames,
        tick(time) { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(fn => fn(time)); }
    };
}
test('touch and pen deliver player data to an accepting drop target', () => {
    for (const type of ['touch', 'pen']) {
        const s = setup();
        s.pointer('pointerdown', type); s.pointer('pointermove', type); s.pointer('pointerup', type);
        assert.deepEqual(s.seen, ['start', 'player:42', 'end']);
    }
});
test('mouse continues to use native drag events', () => {
    const s = setup(); s.pointer('pointerdown', 'mouse'); s.pointer('pointerup', 'mouse');
    assert.deepEqual(s.seen, []);
});
test('cancelled gestures and unaccepted targets never drop', () => {
    const s = setup(); s.pointer('pointerdown'); s.pointer('pointercancel'); s.pointer('pointerup');
    assert.deepEqual(s.seen, ['start', 'end']);
    const rejected = setup(false); rejected.pointer('pointerdown'); rejected.pointer('pointerup');
    assert.deepEqual(rejected.seen, ['start', 'end']);
});
test('another pointer cannot complete the drag and blur cleans up', () => {
    const s = setup(); s.pointer('pointerdown'); s.pointer('pointerup', 'touch', 2);
    assert.deepEqual(s.seen, ['start']);
    s.window.dispatchEvent(new Event('blur')); s.pointer('pointerup');
    assert.deepEqual(s.seen, ['start', 'end']);
});

function scrollNode(height, viewport, top = 0) {
    let position = 0;
    return { scrollHeight: height, clientHeight: viewport, overflowY: 'auto',
        get scrollTop() { return position; },
        set scrollTop(value) { position = Math.max(0, Math.min(height - viewport, value)); },
        getBoundingClientRect: () => ({ top, bottom: top + viewport }) };
}
test('stationary finger scrolls a list, then the page at the list boundary', () => {
    const s = setup(), list = scrollNode(1200, 400, 200), page = scrollNode(2000, 600);
    s.target.parentElement = list; list.parentElement = page; s.document.scrollingElement = page;
    let hits = 0; s.document.elementFromPoint = () => { hits++; return s.target; };
    s.pointer('pointerdown', 'touch', 1, 590); s.tick(0); s.tick(16);
    assert(list.scrollTop > 0); assert.equal(page.scrollTop, 0); assert(hits >= 4);
    list.scrollTop = 800; s.tick(32); assert(page.scrollTop > 0);
    s.pointer('pointercancel'); assert.equal(s.frames.size, 0);
    const stopped = page.scrollTop; s.tick(48); assert.equal(page.scrollTop, stopped);
});
test('middle of a list does not scroll; top edge scrolls upward at bounded speed', () => {
    const s = setup(), list = scrollNode(1200, 400, 100);
    list.scrollTop = 500; s.target.parentElement = list;
    s.pointer('pointerdown', 'pen', 1, 300); s.tick(0); s.tick(16); assert.equal(list.scrollTop, 500);
    s.pointer('pointermove', 'pen', 1, 105); s.tick(10000);
    assert(list.scrollTop < 500); assert(list.scrollTop >= 500 - 480 * .032);
    s.pointer('pointercancel', 'pen');
});
test('visual viewport edges are used and Escape cancels animation without dropping', () => {
    const s = setup(), page = scrollNode(2000, 600); s.document.scrollingElement = page;
    s.window.visualViewport = { offsetTop: 100, height: 250 };
    s.pointer('pointerdown', 'touch', 1, 345); s.tick(0); s.tick(16); assert(page.scrollTop > 0);
    const escape = new Event('keydown'); Object.defineProperty(escape, 'key', { value: 'Escape' });
    s.document.dispatchEvent(escape); assert.equal(s.frames.size, 0); assert.deepEqual(s.seen, ['start', 'end']);
});
