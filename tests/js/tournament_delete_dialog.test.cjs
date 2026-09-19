const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(
    path.resolve(__dirname, '../../templates/tournaments/dashboard.html'),
    'utf8'
);

function extract(name) {
    const start = template.indexOf(`        function ${name}(`);
    assert.ok(start >= 0, `${name} should exist`);
    return template.slice(start, template.indexOf('\n        }', start) + 10);
}

test('tournament delete no longer uses a browser confirm dialog', () => {
    assert.match(template, /id="delete-tournament-dialog"/);
    assert.match(template, /id="delete-tournament-cautions"/);
    assert.doesNotMatch(template, /confirm\(form\.dataset\.confirmMessage\)/);
    assert.doesNotMatch(template, /data-confirm-message=/);
    assert.match(template, /Cancel/);
    assert.match(template, /This cannot be undone\./);
});

test('opening and cancelling the delete dialog toggles the in-page overlay', () => {
    const dialog = { classList: { items: new Set() } };
    dialog.classList.add = (name) => dialog.classList.items.add(name);
    dialog.classList.remove = (name) => dialog.classList.items.delete(name);
    dialog.classList.contains = (name) => dialog.classList.items.has(name);

    const context = {
        document: {
            getElementById(id) {
                return id === 'delete-tournament-dialog' ? dialog : null;
            },
        },
    };
    vm.createContext(context);
    vm.runInContext(extract('openDeleteTournamentDialog'), context);
    vm.runInContext(extract('closeDeleteTournamentDialog'), context);

    vm.runInContext('openDeleteTournamentDialog()', context);
    assert.equal(dialog.classList.contains('show'), true);
    vm.runInContext('closeDeleteTournamentDialog()', context);
    assert.equal(dialog.classList.contains('show'), false);
});
