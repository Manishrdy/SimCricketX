const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(
    path.resolve(__dirname, '../../templates/tournaments/create.html'),
    'utf8'
);

// The ids a render actually contains. With no teams, each `{% if teams %}`
// body is dropped (keeping any `{% else %}` body); with teams, the reverse.
function renderedIds(hasTeams) {
    const markup = template.replace(
        /\{% if teams %\}([\s\S]*?)(?:\{% else %\}([\s\S]*?))?\{% endif %\}/g,
        (_, ifBody, elseBody = '') => (hasTeams ? ifBody : elseBody)
    );
    return new Set([...markup.matchAll(/\bid="([^"{]+)"/g)].map(m => m[1]));
}

function fakeElement() {
    return {
        value: '',
        textContent: '',
        innerHTML: '',
        disabled: false,
        style: {},
        classList: { add() {}, remove() {}, toggle() {} },
    };
}

// Runs the page script and its DOMContentLoaded handler against a document
// holding only the elements the given render would produce.
function loadPage(hasTeams, checkedTeams = 0) {
    const script = template.match(
        /<script>\s*\/\/ Team format availability map[\s\S]*?<\/script>/
    )[0]
        .replace(/^<script>|<\/script>$/g, '')
        .replace('{{ team_formats_json | safe }}', '{}')
        .replace('{{ format_labels | tojson }}', '{"T20": "T20"}');

    const elements = {};
    for (const id of renderedIds(hasTeams)) elements[id] = fakeElement();
    const ready = [];
    const context = {
        window: { addEventListener() {} },
        document: {
            getElementById: id => elements[id] || null,
            querySelector: () => null,
            querySelectorAll(selector) {
                if (selector.startsWith('input[name="team_ids"]:checked')) {
                    return { length: checkedTeams };
                }
                return [];
            },
            addEventListener(type, fn) {
                if (type === 'DOMContentLoaded') ready.push(fn);
            },
        },
    };
    vm.runInNewContext(script, context);
    ready.forEach(fn => fn());
    return elements;
}

test('the no-teams render really omits the mode section', () => {
    assert.ok(renderedIds(true).has('modeGrid'));
    assert.ok(!renderedIds(false).has('modeGrid'));
    assert.ok(renderedIds(false).has('teamCounter'));
});

test('page load does not throw for a user with no teams', () => {
    const elements = loadPage(false);
    assert.equal(elements.teamCounter.textContent, '0 selected');
});

test('with teams, the mode grid still shows its placeholder and then modes', () => {
    const idle = loadPage(true);
    assert.match(idle.modeGrid.innerHTML, /Select at least 2 teams/);
    assert.equal(idle.submitBtn.disabled, true);

    const picked = loadPage(true, 4);
    assert.match(picked.modeGrid.innerHTML, /data-mode="round_robin"/);
    assert.match(picked.modeGrid.innerHTML, /data-mode="ipl_style"/);
});
