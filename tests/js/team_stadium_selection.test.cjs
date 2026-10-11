const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../templates/team_create.html'), 'utf8');
const start = source.lastIndexOf('  function sync() {');
const sync = source.slice(start, source.indexOf('\n  }', start) + 4)
  .replace(/{{ \(team.home_ground if legacy_venue else ''\)\|tojson }}/g, "legacy ? 'Original ground' : ''")
  .replace(/{{ \(team.pitch_preference if legacy_venue else ''\)\|tojson }}/g, "legacy ? 'Green' : ''");
function setup(legacy) {
  const groundField = {}, pitchField = {}, summary = {};
  const context = {legacy, Event:class {constructor(type){this.type=type}},
    select:{value:'new-stadium',selectedOptions:[{dataset:{name:'My stadium',pitch:'Flat'}}]},
    ground:{value:'Original ground',closest:()=>groundField,dispatchEvent(){}},
    pitch:{value:'Green',closest:()=>pitchField,dispatchEvent(){}},
    document:{getElementById:()=>summary}};
  vm.createContext(context); vm.runInContext(sync, context);
  return {context,groundField,pitchField,summary};
}
test('selected venue supplies identity and keeps old ground controls hidden', () => {
  const {context:c,groundField,pitchField,summary} = setup(false);
  c.sync();
  assert.equal(c.ground.value,'My stadium'); assert.equal(c.pitch.value,'Flat');
  assert.equal(groundField.hidden,true); assert.equal(pitchField.hidden,true);
  assert.equal(c.ground.required,false); assert.equal(c.pitch.required,false);
  assert.match(summary.textContent,/My stadium · T20 default: Flat/);
  c.select.value=''; c.sync();
  assert.equal(c.ground.value,''); assert.equal(c.pitch.value,'');
  assert.match(summary.textContent,/No stadium selected/);
});
test('returning to keep existing ground restores its original identity', () => {
  const {context:c,summary} = setup(true);
  c.sync(); assert.equal(c.ground.value,'My stadium');
  c.select.value=''; c.sync();
  assert.equal(c.ground.value,'Original ground'); assert.equal(c.pitch.value,'Green');
  assert.match(summary.textContent,/Keeping Original ground/);
});
