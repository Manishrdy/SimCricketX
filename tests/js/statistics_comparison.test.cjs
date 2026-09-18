const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templatePath = path.resolve(__dirname, '../../templates/statistics.html');
const template = fs.readFileSync(templatePath, 'utf8');
const renderer = template.match(
    /function displayComparison\(players\) \{[\s\S]*?\n\}\n\n\/\/ ===== INSIGHTS VISUALS =====/
);

assert.ok(renderer, 'displayComparison renderer should exist');

function renderComparison(players) {
    const results = { innerHTML: '' };
    const context = {
        document: {
            getElementById(id) {
                assert.equal(id, 'comparison-results');
                return results;
            },
        },
        URLSearchParams,
        window: { location: { search: '?match_format=T20' } },
    };

    vm.runInNewContext(renderer[0].replace(/\n\n\/\/ ===== INSIGHTS VISUALS =====$/, ''), context);
    context.displayComparison(players);
    return results.innerHTML;
}

test('comparison preserves real zeros and uses dashes for missing statistics', () => {
    const html = renderComparison([
        {
            name: 'Zero Player',
            team: 'ZERO',
            batting: { hundreds: 0 },
            bowling: { wickets: 0 },
        },
        {
            name: 'Missing Player',
            team: 'MISS',
            batting: {},
            bowling: {},
        },
    ]);

    assert.match(html, /<div class="comp-key">100s<\/div>[\s\S]*?<span class="comp-val">0<\/span>[\s\S]*?<span class="comp-val">—<\/span>/);
    assert.match(html, /<div class="comp-key">Wkts<\/div>[\s\S]*?<span class="comp-val">0<\/span>[\s\S]*?<span class="comp-val">—<\/span>/);
});

test('zero economy wins against a higher economy and missing values never win', () => {
    const html = renderComparison([
        {name: 'Maiden', bowling: {economy: 0}},
        {name: 'Expensive', bowling: {economy: 8, average: 12}},
    ]);
    assert.match(html, /<div class="comp-key">Eco<\/div>[\s\S]*?<span class="comp-val winner">0<\/span>[\s\S]*?<span class="comp-val">8<\/span>/);
    assert.match(html, /<div class="comp-key">Avg<\/div>[\s\S]*?<span class="comp-val">—<\/span>[\s\S]*?<span class="comp-val">12<\/span>/);
});
test('equal zeros are a tie', () => {
    const html = renderComparison([{name:'One',bowling:{economy:0}},{name:'Two',bowling:{economy:0}}]);
    assert(!html.includes('comp-val winner'));
});
