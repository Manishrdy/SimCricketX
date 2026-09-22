const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '../../templates/team_create.html'),
  'utf8',
);

function extract(name) {
  const start = source.indexOf(`  function ${name}(`);
  assert.notEqual(start, -1, `${name} must exist in team_create.html`);
  return source.slice(start, source.indexOf('\n  }', start) + 4);
}

function validSquad() {
  return Array.from({ length: 11 }, (_, index) => ({
    name: `Player ${index + 1}`,
    role: index === 0
      ? 'Wicketkeeper'
      : (index < 6 ? 'Bowler' : 'Batsman'),
  }));
}

function setup({ extraRoster = [] } = {}) {
  const context = {
    VALID_FORMATS: ['T20', 'T10', 'ListA', 'FC'],
    MIN_PLAYERS: 11,
    MAX_PLAYERS: 25,
    MIN_WK: 1,
    MIN_BOWL: 5,
    // Deliberately stale, as happens when the browser restores/autofills the
    // visible controls without dispatching input events.
    state: {
      identity: {
        team_name: '', short_code: '', home_ground: '',
        pitch_preference: '', team_color: '',
      },
      activeFmt: 'ListA',
      rosters: { T20: extraRoster, T10: [], ListA: validSquad(), FC: [] },
      leaders: {
        T20: { captain: '', wicketkeeper: '' },
        T10: { captain: '', wicketkeeper: '' },
        ListA: { captain: 'Player 2', wicketkeeper: 'Player 1' },
        FC: { captain: '', wicketkeeper: '' },
      },
    },
    nameInput: { value: 'List A XI' },
    shortInput: { value: 'LAXI' },
    groundInput: { value: 'Home Ground' },
    pitchSelect: { value: 'Hard' },
    colorInput: { value: '#123456' },
    saveBtn: { disabled: true },
    validationMsg: { innerHTML: '' },
    updateTabStatus() {},
    renderRuleChips() {},
  };
  vm.createContext(context);
  vm.runInContext(extract('computeRuleChecks'), context);
  vm.runInContext(extract('updateValidation'), context);
  return context;
}

test('one valid List A squad enables save when every other format is empty', () => {
  const context = setup();
  context.updateValidation();
  assert.equal(context.saveBtn.disabled, false);
});

test('a non-empty invalid format still blocks the team-level save', () => {
  const context = setup({ extraRoster: [{ name: 'Partial', role: 'Batsman' }] });
  context.updateValidation();
  assert.equal(context.saveBtn.disabled, true);
});
