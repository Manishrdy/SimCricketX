const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function setup(accepted = true) {
    const document = new EventTarget();
    const window = new EventTarget();
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
    function pointer(type, pointerType = 'touch', pointerId = 1) {
        const e = new Event(type, { cancelable: true });
        Object.defineProperties(e, {
            target: { value: { closest: () => handle } },
            pointerType: { value: pointerType }, pointerId: { value: pointerId },
            isPrimary: { value: true }, clientX: { value: 50 }, clientY: { value: 100 }
        });
        document.dispatchEvent(e);
    }
    return { pointer, seen, window };
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
