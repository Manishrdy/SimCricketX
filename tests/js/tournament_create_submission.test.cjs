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
        /function validateForm\(\) \{[\s\S]*?\r?\n    \}/
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
                assert.ok(['input[name="team_ids"]:checked', 'input[name="stadium_ids"]:checked'].includes(selector));
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

test('Back restores submission controls while keeping the creation token', () => {
    const listener = template.match(/window\.addEventListener\('pageshow', function \(event\) \{[\s\S]*?\n    \}\);/)[0];
    let handler;
    const button = {disabled: true, innerHTML: 'Creating Tournament…'};
    const token = {value: 'same-intent'};
    const context = {isSubmitting: true, window: {addEventListener: (_, fn) => handler = fn},
        document: {getElementById: id => id === 'submitBtn' ? button : {value: 'round_robin'}, querySelector: () => token}};
    vm.runInNewContext(listener, context);
    handler({persisted: false});
    assert(context.isSubmitting);
    handler({persisted: true});
    assert.equal(context.isSubmitting, false);
    assert.equal(button.disabled, false);
    assert.match(button.innerHTML, /Create Tournament/);
    assert.equal(token.value, 'same-intent');
});

test('tournament form input has maxlength 100', () => {
    assert.match(
        template,
        /<input[^>]*id="tournament-name"[^>]*maxlength="100"/
    );
});

test('validateForm rejects whitespace-only tournament name', () => {
    const validator = template.match(
        /function validateForm\(\) \{[\s\S]*?\r?\n    \}/
    );
    assert.ok(validator, 'validateForm should exist');

    let alertedMessage = null;
    const submitBtn = { disabled: false, innerHTML: '' };
    const elements = {
        selectedMode: { value: 'round_robin' },
        'tournament-name': { value: '   ' },
        submitBtn,
    };
    const context = {
        alert(msg) {
            alertedMessage = msg;
        },
        document: {
            getElementById(id) {
                return elements[id];
            },
            querySelectorAll(selector) {
                return { length: 2 };
            },
        },
    };

    vm.runInNewContext(
        `let isSubmitting = false;\n${validator[0].replace(/\r?\n\r?\n    \/\/ Initialize on page load$/, '')}`,
        context
    );

    assert.equal(vm.runInNewContext('validateForm()', context), false);
    assert.equal(alertedMessage, 'Please enter a tournament name.');
    assert.equal(submitBtn.disabled, false);
});

test('validateForm rejects tournament name longer than 100 characters', () => {
    const validator = template.match(
        /function validateForm\(\) \{[\s\S]*?\r?\n    \}/
    );
    assert.ok(validator, 'validateForm should exist');

    let alertedMessage = null;
    const submitBtn = { disabled: false, innerHTML: '' };
    const elements = {
        selectedMode: { value: 'round_robin' },
        'tournament-name': { value: 'A'.repeat(101) },
        submitBtn,
    };
    const context = {
        alert(msg) {
            alertedMessage = msg;
        },
        document: {
            getElementById(id) {
                return elements[id];
            },
            querySelectorAll(selector) {
                return { length: 2 };
            },
        },
    };

    vm.runInNewContext(
        `let isSubmitting = false;\n${validator[0].replace(/\r?\n\r?\n    \/\/ Initialize on page load$/, '')}`,
        context
    );

    assert.equal(vm.runInNewContext('validateForm()', context), false);
    assert.equal(alertedMessage, 'Tournament name must be 100 characters or less.');
    assert.equal(submitBtn.disabled, false);
});


test('creation rejects zero or more than fifteen stadiums before submitting', () => {
    const validator = template.match(/function validateForm\(\) \{[\s\S]*?\r?\n    \}/)[0];
    for (const count of [0, 16]) {
        let message;
        const context = {isSubmitting:false, alert: text => {message = text;},
            document:{querySelectorAll: () => ({length:count})}};
        vm.runInNewContext(validator, context);
        assert.equal(context.validateForm(), false);
        assert.match(message, /between 1 and 15 stadiums/);
    }
});

test('stadium selection caps at fifteen and re-enables choices after removal', () => {
    const fn = template.match(/function updateStadiumSelection\(\) \{[\s\S]*?\r?\n    \}/)[0];
    const boxes = Array.from({length:16}, (_, i) => ({checked:i < 15, disabled:false}));
    const counter = {};
    const context = {document:{querySelectorAll:() => boxes, getElementById:() => counter}};
    vm.runInNewContext(fn, context);
    context.updateStadiumSelection();
    assert.equal(boxes[15].disabled, true);
    assert.equal(boxes[0].disabled, false);
    boxes[0].checked = false;
    context.updateStadiumSelection();
    assert.equal(boxes[15].disabled, false);
    assert.match(counter.textContent, /14 \/ 15/);
});
