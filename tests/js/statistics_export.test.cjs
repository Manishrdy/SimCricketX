const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../templates/statistics.html'), 'utf8');
const extract = name => source.slice(source.indexOf(`function ${name}(`), source.indexOf('\n}', source.indexOf(`function ${name}(`)) + 2);
for (const type of ['batting', 'bowling', 'fielding', 'partnerships']) {
    test(`${type}: CSV and TXT export every filtered row regardless of page`, () => {
        const rows = Array.from({ length: 100 }, (_, i) => ({
            dataset: { role: i % 2 ? 'Bowler' : 'Batsman' }, style: {},
            querySelectorAll: () => [{ textContent: `Player ${i}` }, { textContent: i < 80 ? 'Alpha' : 'Beta' }]
        }));
        const table = { querySelectorAll: selector => selector === 'tbody tr' ? rows : [{ textContent: 'Player' }, { textContent: 'Team' }] };
        const controls = { '.tbl-search': { value: '' }, '.tbl-team-sel': { value: '' }, '.tbl-role-sel': { value: '' } };
        const panel = { querySelector: selector => controls[selector] };
        const exports = [];
        const context = { pgState: { [type]: { page: 1, size: 25 } }, URLSearchParams,
            window: { location: { search: '?match_format=ListA' } },
            location: { search: '?match_format=ListA' },
            document: { getElementById: id => id === type + '-table' ? table : id === type ? panel : {} },
            dlBlob: (content, filename) => exports.push({ content, filename }) };
        vm.createContext(context);
        for (const name of ['applyFilter', 'filterPartnerships', 'applyPagination', 'exportFiltered']) vm.runInContext(extract(name), context);
        const verify = count => {
            context.exportFiltered(type, 'csv'); context.exportFiltered(type, 'txt');
            assert.equal(exports.at(-2).content.trimEnd().split('\n').length, count + 1);
            assert.match(exports.at(-1).content, new RegExp(`Records: ${count}\\n`));
            assert.equal(exports.at(-2).filename, `ListA_all_${type}_stats.csv`);
        };
        context.applyPagination(type); assert.equal(rows.filter(r => r.style.display === '').length, 25); verify(100);
        context.pgState[type].page = 3; context.applyPagination(type); verify(100);
        const filter = () => type === 'partnerships' ? context.filterPartnerships() : context.applyFilter(type);
        const count = type === 'partnerships' ? 80 : 40;
        if (type === 'partnerships') controls['.tbl-search'].value = 'Alpha';
        else { controls['.tbl-team-sel'].value = 'Alpha'; controls['.tbl-role-sel'].value = 'Batsman'; }
        filter(); verify(count);
        context.pgState[type].page = 2; context.applyPagination(type); verify(count);
        assert(!exports.at(-2).content.includes('Player 81'));
        controls['.tbl-search'].value = 'missing'; filter(); verify(0);
    });
}
