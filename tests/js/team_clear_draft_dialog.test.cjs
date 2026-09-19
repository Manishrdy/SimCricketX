const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(
    path.resolve(__dirname, '../../templates/team_create.html'),
    'utf8'
);

function extract(name) {
    const start = template.indexOf(`  function ${name}(`);
    assert.ok(start >= 0, `${name} should exist`);
    return template.slice(start, template.indexOf('\n  }', start) + 4);
}

test('clearing a draft no longer goes through window.confirm', () => {
    assert.match(template, /id="tc-clear-dialog"/);
    assert.match(template, /data-focus-dialog/);
    assert.match(template, /data-dialog-open-class="show"/);
    assert.match(template, /data-dialog-close="\[data-clear-cancel\]"/);
    assert.match(template, /data-clear-confirm/);
    assert.match(template, /This cannot be undone\./);
    assert.doesNotMatch(template, /confirm\('Clear your unsaved draft/);
    // The dialog's own Cancel takes initial focus, not the destructive button.
    const dialog = template.slice(template.indexOf('id="tc-clear-dialog"'),
        template.indexOf('data-clear-confirm'));
    assert.match(dialog, /data-clear-cancel data-dialog-initial/);
});

test('opening and closing the dialog only toggles the overlay class', () => {
    const dialog = { classList: { items: new Set() } };
    dialog.classList.add = name => dialog.classList.items.add(name);
    dialog.classList.remove = name => dialog.classList.items.delete(name);
    dialog.classList.contains = name => dialog.classList.items.has(name);

    const context = {
        clearDialog: dialog,
        document: { getElementById: () => null },
    };
    vm.createContext(context);
    vm.runInContext(extract('openClearDialog'), context);
    vm.runInContext(extract('closeClearDialog'), context);

    vm.runInContext('openClearDialog()', context);
    assert.equal(dialog.classList.contains('show'), true);
    vm.runInContext('closeClearDialog()', context);
    assert.equal(dialog.classList.contains('show'), false);
});

test('the reset is a named step the dialog calls, not inlined in the button handler', () => {
    // Cancelling or dismissing must not wipe the draft, so the wipe lives in
    // resetDraftState() and only the confirm branch reaches it.
    const reset = extract('resetDraftState');
    assert.match(reset, /clearDraft\(\)/);
    assert.match(reset, /state\.rosters = \{\}/);
    const handler = template.slice(template.indexOf("clearBtn.addEventListener('click', openClearDialog)"));
    const confirmBranch = handler.slice(handler.indexOf('data-clear-confirm'));
    assert.match(confirmBranch, /resetDraftState\(\)/);
    const cancelBranch = handler.slice(handler.indexOf('data-clear-cancel'),
        handler.indexOf('data-clear-confirm'));
    assert.doesNotMatch(cancelBranch, /resetDraftState/);
});
