const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(
    path.resolve(__dirname, '../../templates/tournaments/create.html'),
    'utf8'
);

test('valid tournament form enters a pending state and rejects a second submit', () => {
    const validator = template.match(
        /function validateForm\(\) \{[\s\S]*?\r?\n    \}\r?\n\r?\n    \/\/ Initialize on page load/
    );
    assert.ok(validator, 'validateForm should exist');

    const submitBtn = { disabled: false, innerHTML: '' };
    const elements = {
        selectedMode: { value: 'round_robin' },
        'tournament-name': { value: 'Delayed Response Cup' },
        submitBtn,
    };
    const context = {
        alert() {
            assert.fail('valid form should not alert');
        },
        document: {
            getElementById(id) {
                return elements[id];
            },
            querySelectorAll(selector) {
                assert.equal(selector, 'input[name="team_ids"]:checked');
                return { length: 2 };
            },
        },
    };

    vm.runInNewContext(
        `let isSubmitting = false;\n${validator[0].replace(/\r?\n\r?\n    \/\/ Initialize on page load$/, '')}`,
        context
    );

    assert.equal(vm.runInNewContext('validateForm()', context), true);
    assert.equal(submitBtn.disabled, true);
    assert.match(submitBtn.innerHTML, /Creating Tournament/);
    assert.equal(vm.runInNewContext('validateForm()', context), false);
});

test('tournament form sends a server idempotency token', () => {
    assert.match(
        template,
        /<input type="hidden" name="creation_token" value="\{\{ creation_token \}\}">/
    );
});
