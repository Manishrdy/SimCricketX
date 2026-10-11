const test = require('node:test');
const assert = require('node:assert/strict');
const {setPath, setBlend} = require('../../static/js/stadium_tuning.js');
test('edits preserve zero and isolate formats/pitches/chasing settings', () => {
  const settings = {T20:{pitches:{Hard:{scoring:1},Green:{scoring:1}}},Hundred:{phases:{middle:{chasing:{wicket:1}}}}};
  setPath(settings,['T20','pitches','Hard','scoring'],0);
  setPath(settings,['Hundred','phases','middle','chasing','wicket'],2);
  assert.equal(settings.T20.pitches.Hard.scoring,0);
  assert.equal(settings.T20.pitches.Green.scoring,1);
  assert.equal(settings.Hundred.phases.middle.chasing.wicket,2);
});
test('blend complements include endpoints and do not edit other formats', () => {
  const settings = {FC:{blending:{pitch_weight:.1,skill_weight:.9}},T20:{blending:{pitch_weight:.6,skill_weight:.4}}};
  for (const percent of [0,100,43.21]) {
    setBlend(settings,'T20',percent);
    assert.equal(settings.T20.blending.pitch_weight,percent/100);
    assert.equal(settings.T20.blending.pitch_weight+settings.T20.blending.skill_weight,1);
    assert.deepEqual(settings.FC.blending,{pitch_weight:.1,skill_weight:.9});
  }
});
