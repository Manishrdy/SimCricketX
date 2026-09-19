const test = require('node:test');
const assert = require('node:assert/strict');
const {availability, activeOrder, move, syncTeamChoices} = require('../../static/js/tour_builder.js');
const formats = included => Object.fromEntries(['FC', 'ListA', 'T20'].map(f => [f, {available: included.includes(f)}]));
const teams = {A:{formats:formats(['T20'])}, B:{formats:formats(['T20','ListA'])},
    C:{formats:formats(['ListA'])}, D:{formats:formats(['FC','ListA','T20'])}, India:{formats:formats(['FC'])}};
test('only shared playable squads enable counts', () => {
    assert.deepEqual(availability(teams,'A','B'),{FC:false,ListA:false,T20:true});
    assert.deepEqual(availability(teams,'B','C'),{FC:false,ListA:true,T20:false});
    assert.deepEqual(availability(teams,'A','C'),{FC:false,ListA:false,T20:false});
    assert.deepEqual(availability(teams,'D','India'),{FC:true,ListA:false,T20:false});
});
test('incomplete selection and same-team selection disable every format', () => {
    assert.ok(Object.values(availability(teams,'A','')).every(v => !v));
    assert.ok(Object.values(availability(teams,'D','D')).every(v => !v));
});
test('order excludes zero and invalid counts and preserves selected order', () => {
    assert.deepEqual(activeOrder(['T20','FC','ListA'],{FC:'3',ListA:'3',T20:'0'}),['FC','ListA']);
    assert.deepEqual(activeOrder(['FC'],{FC:'1.5',ListA:'-1',T20:'2'}),['T20']);
});
test('moving a series preserves all entries and respects boundaries', () => {
    assert.deepEqual(move(['FC','ListA','T20'],2,0),['T20','FC','ListA']);
    assert.deepEqual(move(['FC','ListA'],0,-1),['FC','ListA']);
});

function teamSelect(value) {
    return {value, options: ['', 'A', 'B', 'C'].map(value => ({value, disabled:false}))};
}
test('each dropdown disables the team selected in the other and restores old options', () => {
    const home = teamSelect('A'), away = teamSelect('B');
    syncTeamChoices(home, away, home);
    assert.equal(home.options[2].disabled, true);
    assert.equal(away.options[1].disabled, true);
    assert.equal(away.options[0].disabled, false);
    home.value = 'C';
    syncTeamChoices(home, away, home);
    assert.equal(away.options[1].disabled, false);
    assert.equal(away.options[3].disabled, true);
    away.value = '';
    syncTeamChoices(home, away, away);
    assert.ok(home.options.every(option => !option.disabled));
});
test('duplicate restored or forced selections clear the conflicting team', () => {
    const home = teamSelect('A'), away = teamSelect('A');
    syncTeamChoices(home, away);
    assert.equal(away.value, '');
    assert.equal(away.options[1].disabled, true);
    away.value = 'A';
    syncTeamChoices(home, away, away);
    assert.equal(home.value, '');
    assert.equal(home.options[1].disabled, true);
});
