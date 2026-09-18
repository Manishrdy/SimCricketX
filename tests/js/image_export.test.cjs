const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, '../..', file), 'utf8');
function setup(mode = 'ok', width = 1000, height = 16000) {
    const released = [], timers = [], alerts = [], captures = [], downloads = [], buttons = {};
    const element = { scrollWidth: width, scrollHeight: height, getBoundingClientRect: () => ({ width, height }) };
    const canvas = { width: 1, height: 1, toBlob(callback) {
        if (mode === 'encode-throw') throw Error('SecurityError');
        callback(mode === 'empty' ? null : new Blob(['image']));
    } };
    const window = { location: { search: '?match_format=FC' }, setTimeout: fn => timers.push(fn) };
    if (mode !== 'missing') window.html2canvas = (_, options) => {
        captures.push(options);
        if (mode === 'throw') throw Error('capture failed');
        if (mode === 'reject') return Promise.reject(Error('capture rejected'));
        return Promise.resolve(canvas);
    };
    const context = { window, Blob, URLSearchParams, alert: message => alerts.push(message), console: { error() {} },
        URL: { createObjectURL: () => 'blob:test', revokeObjectURL: url => released.push(url) },
        document: {
            documentElement: { getAttribute: () => 'light' }, body: { appendChild() {} },
            createElement: () => ({ click() { if (mode === 'download-throw') throw Error('download failed'); downloads.push(this.download); }, remove() {} }),
            getElementById: id => id === 'scorecard-content' ? element : id === 'batting' ? { querySelector: () => element } :
                buttons[id] ||= { innerHTML: 'Save', disabled: false, addEventListener(_, fn) { this.click = fn; } },
            querySelectorAll: () => [{ innerText: 'Full scorecard' }, { innerText: 'Fourth innings\nPlayer\tRuns\nKeeper\t42' }]
        }
    };
    vm.createContext(context); vm.runInContext(read('static/js/image_export.js'), context);
    return { context, canvas, captures, downloads, timers, alerts, released, buttons, element };
}
for (const [width, height] of [[1000, 16000], [1000, 100000], [15000, 1000], [400, 600]]) {
    test(`capture ${width}x${height} respects pixel and side limits and releases bitmap`, async () => {
        const s = setup('ok', width, height);
        await s.context.window.scxImageExport.capturePng(s.element, { scale: 2.5 });
        const { scale, width: w, height: h } = s.captures[0];
        assert(w * h * scale * scale <= 4_000_001); assert(w * scale <= 8192.001); assert(h * scale <= 8192.001);
        assert.equal(s.canvas.width, 0); assert.equal(s.canvas.height, 0);
    });
}
for (const mode of ['missing', 'throw', 'reject', 'empty', 'encode-throw', 'download-throw', 'ok']) {
    test(`standalone export restores controls after ${mode}; TXT works without CDN`, async () => {
        const s = setup(mode);
        const template = read('templates/scorecard_view.html');
        const script = template.slice(template.indexOf('const scorecardFilename'), template.indexOf('</script>', template.indexOf('const scorecardFilename'))).replace(/{{[^\n]+}}/, '"Scorecard_Test"');
        vm.runInContext(script, s.context);
        await s.buttons.saveScorecard.click.call(s.buttons.saveScorecard);
        assert.equal(s.buttons.saveScorecard.disabled, false); assert.equal(s.buttons.saveScorecard.innerHTML, 'Save');
        if (mode !== 'ok') assert.match(s.alerts[0], /Reload.*Download TXT/);
        else { assert.equal(s.downloads.length, 1); assert.equal(s.released.length, 0); s.timers[0](); assert.deepEqual(s.released, ['blob:test']); }
        if (mode === 'missing') { s.buttons.saveScorecardText.click(); assert.match(s.downloads[0], /\.txt$/); }
        if (mode === 'empty' || mode === 'encode-throw') assert.equal(s.canvas.width, 0);
        if (mode === 'download-throw') assert.deepEqual(s.released, ['blob:test']);
    });
    test(`statistics restores export button after ${mode}`, async () => {
        const s = setup(mode), template = read('templates/statistics.html');
        const start = template.indexOf('async function exportImage(');
        vm.runInContext(template.slice(start, template.indexOf('// ===== KPI COMPUTATION', start)), s.context);
        const button = { innerHTML: 'PNG', disabled: false };
        await s.context.exportImage('batting', 'png', button);
        assert.equal(button.disabled, false); assert.equal(button.innerHTML, 'PNG');
        if (mode !== 'ok') assert.match(s.alerts[0], /CSV or TXT/);
    });
}
