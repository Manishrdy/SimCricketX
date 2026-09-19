const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../templates/ground_conditions.html'), 'utf8');
function extract(name) {
    const start = source.indexOf(`        function ${name}(`);
    assert(start >= 0, name);
    return source.slice(start, source.indexOf('\n        }', start) + 10);
}
function setup(value = '0') {
    const input = dataset => ({ value, dataset });
    const context = { CONFIG: { pitch_profiles: { Hard: {} } }, document: {
        getElementById: () => input({}), querySelector: () => input({}),
        querySelectorAll(selector) {
            if (selector.includes('pitch-card')) return [{ dataset: { pitch: 'Hard' } }];
            if (selector.includes('matrix-card')) return [{ dataset: { phase: 'pp1' } }];
            if (selector.includes('matrix-input')) return [{ value: '1', dataset: { outcome: 'Dot' } }];
            if (selector.includes('data-la-field')) return [input({ laField: 'run_factor' }), input({ laField: 'wicket_mult' })];
            if (selector.includes('data-la-ds')) return [input({ laDs: 'Dot' })];
            if (selector.includes('data-la-ft')) return [input({ laFt: 'Four' })];
            if (selector.includes('data-la-pb')) return [input({ laPb: 'pp1', laPbScope: 'all', laPbOutcome: 'Four' })];
            if (selector.includes('data-la-dew')) return [input({ laDew: 'Four' })];
            if (selector.includes('wf-input')) return [input({ wf: 'Fast', style: 'Fast' })];
            return [];
        }
    } };
    vm.createContext(context);
    for (const name of ['numericValue', 'applyBlending', 'buildT20Config', 'buildListAConfig', 'buildFCConfig', 'readProfileFromDom', 'readPhaseBoostsFromDom']) vm.runInContext(extract(name), context);
    return context;
}
test('T20 saves zero run, wicket, phase and blending factors and previews read zero', () => {
    const c = setup(), cfg = c.buildT20Config();
    assert.equal(cfg.pitch_profiles.Hard.run_factor, 0);
    assert.equal(cfg.pitch_profiles.Hard.wicket_factors.Fast, 0);
    assert.equal(cfg.blending.pitch_weight, 0);
    for (const [phase, values] of Object.entries(cfg.phase_boosts)) {
        for (const [key, value] of Object.entries(values)) if (!key.startsWith('overs_')) assert.equal(value, 0, phase + '.' + key);
    }
    assert.equal(c.readProfileFromDom('Hard').run_factor, 0);
    assert.equal(c.readProfileFromDom('Hard').wicket_factors.Fast, 0);
    assert.equal(c.readPhaseBoostsFromDom().powerplay.boundary_multiplier, 0);
});
test('List A saves zero run, rotation, fine tuning, phase and dew values', () => {
    const cfg = setup().buildListAConfig(), profile = cfg.pitch_profiles.Hard;
    assert.equal(profile.run_factor, 0); assert.equal(profile.wicket_mult, 0);
    assert.equal(profile.dot_single.Dot, 0); assert.equal(profile.fine_tune.Four, 0);
    assert.equal(cfg.phase_boosts.pp1.all.Four, 0); assert.equal(cfg.dew.factors.Four, 0);
});
test('FC saves zero run, wicket and rough factors', () => {
    const cfg = setup().buildFCConfig(), profile = cfg.pitch_profiles.Hard;
    assert.equal(profile.run_factor, 0); assert.equal(profile.wicket_factors_start.Fast, 0);
    assert.equal(profile.wicket_factors_end.Fast, 0); assert.equal(cfg.rough_targeting.Hard, 0);
});
for (const value of ['', ' ', 'invalid', 'Infinity']) test(`invalid or missing input uses fallback (${JSON.stringify(value)})`, () => {
    const cfg = setup(value).buildT20Config();
    assert.equal(cfg.pitch_profiles.Hard.run_factor, 1);
    assert.equal(cfg.phase_boosts.powerplay.boundary_multiplier, 1.25);
});
test('List A preserves 1.0 and custom phase boosts', () => {
    const cfg1 = setup('1.0').buildListAConfig();
    assert.equal(cfg1.phase_boosts.pp1.all.Four, 1.0);
    const cfgCustom = setup('1.35').buildListAConfig();
    assert.equal(cfgCustom.phase_boosts.pp1.all.Four, 1.35);
});
