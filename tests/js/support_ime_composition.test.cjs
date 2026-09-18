const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const scripts = [
    ['support widget', '../../static/js/support_widget.js', 'form'],
    ['admin support', '../../static/js/admin_support.js', 'composer'],
];

function loadKeydownHandler(relativePath, submitTarget) {
    const source = fs.readFileSync(path.resolve(__dirname, relativePath), 'utf8');
    const listener = source.match(
        /input\.addEventListener\('keydown', function \(e\) \{[\s\S]*?\n    \}\);/
    );
    assert.ok(listener, `keydown listener should exist in ${relativePath}`);

    let keydown;
    let submissions = 0;
    const context = {
        input: {
            addEventListener(type, handler) {
                assert.equal(type, 'keydown');
                keydown = handler;
            },
        },
        Event: function Event(type) {
            return { type };
        },
    };
    context[submitTarget] = {
        dispatchEvent(event) {
            assert.equal(event.type, 'submit');
            submissions += 1;
        },
    };

    vm.runInNewContext(listener[0], context);
    return {
        fire(event) {
            let prevented = false;
            keydown({
                isComposing: false,
                keyCode: 0,
                shiftKey: false,
                preventDefault() {
                    prevented = true;
                },
                ...event,
            });
            return { prevented, submissions };
        },
    };
}

scripts.forEach(([name, scriptPath, submitTarget]) => {
    test(`${name} ignores Enter while an IME composition is active`, () => {
        const handler = loadKeydownHandler(scriptPath, submitTarget);

        assert.deepEqual(
            handler.fire({ key: 'Enter', isComposing: true }),
            { prevented: false, submissions: 0 }
        );
        assert.deepEqual(
            handler.fire({ key: 'Enter', keyCode: 229 }),
            { prevented: false, submissions: 0 }
        );
    });

    test(`${name} sends on Enter and preserves Shift+Enter`, () => {
        const handler = loadKeydownHandler(scriptPath, submitTarget);

        assert.deepEqual(
            handler.fire({ key: 'Enter', shiftKey: true }),
            { prevented: false, submissions: 0 }
        );
        assert.deepEqual(
            handler.fire({ key: 'Enter' }),
            { prevented: true, submissions: 1 }
        );
    });
});
