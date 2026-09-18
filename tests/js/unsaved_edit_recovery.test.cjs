const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const teamTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../templates/team_create.html'),
    'utf8'
);
const groundTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../templates/ground_conditions.html'),
    'utf8'
);

function functionBody(source, name) {
    const start = source.indexOf(`function ${name}(`);
    assert.notEqual(start, -1, `${name} should exist`);
    const bodyStart = source.indexOf('{', start);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index += 1) {
        if (source[index] === '{') depth += 1;
        if (source[index] === '}') depth -= 1;
        if (depth === 0) return source.slice(bodyStart + 1, index);
    }
    assert.fail(`${name} should have a complete body`);
}

test('team editing uses a recoverable per-team draft on Back or app termination', () => {
    assert.match(teamTemplate, /team_edit_draft_v1:\$\{EDIT_DATA\.identity\.short_code\}/);
    assert.doesNotMatch(functionBody(teamTemplate, 'saveDraftNow'), /if \(IS_EDIT\) return/);
    assert.doesNotMatch(functionBody(teamTemplate, 'scheduleAutoSave'), /if \(IS_EDIT\) return/);
    assert.match(
        teamTemplate,
        /if \(IS_EDIT\) window\.addEventListener\('pagehide', saveDraftNow\)/
    );
    assert.match(teamTemplate, /comparableSnapshot\(editDraft\).*comparableSnapshot\(snapshot\(\)\)/s);
});

test('ground edits persist by format and require save or discard before switching', () => {
    assert.match(
        groundTemplate,
        /const DRAFT_KEY = 'ground_conditions_draft_v1:' \+ MATCH_FORMAT/
    );
    assert.match(groundTemplate, /window\.addEventListener\('pagehide', persistDraftNow\)/);
    assert.match(groundTemplate, /if \(!hasUnsavedChanges \|\| tab\.classList\.contains\('active'\)\) return/);
    assert.match(groundTemplate, /id="gcSaveAndSwitch"/);
    assert.match(groundTemplate, /id="gcDiscardChanges"/);
    assert.match(groundTemplate, /if \(await saveAll\(\)\) location\.href = destination/);
    assert.match(
        groundTemplate,
        /window\.saveAll = async function \(\) \{[\s\S]*?clearGroundDraft\(\)[\s\S]*?return saved/
    );
});
