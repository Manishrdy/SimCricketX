const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../templates/my_matches.html'), 'utf8');
const start = source.indexOf('        // ============ Load More');
const script = source.slice(start, source.indexOf('\n    })();', start))
    .replace(/{{ page\|default\(1\) }}/g, '1').replace(/{{ total_matches\|default\(0\) }}/g, '30')
    .replace(/{{ url_for\("my_matches"\) }}/g, '/my-matches');
test('Load More preserves the submitted query and filters, and retries failed pages', async () => {
    const urls = [], cards = [], wrap = { querySelector: () => ({}) };
    let click, fail = true;
    const button = { addEventListener: (_, fn) => click = fn, closest: () => wrap };
    const grid = { querySelectorAll: () => cards, appendChild: card => cards.push(card) };
    const context = { URLSearchParams, isManageMode: false, console: { error() {} }, showToast() {},
        window: { location: { search: '?q=Rare&format=T20&type=exhibition' } },
        document: { getElementById: id => id === 'load-more-btn' ? button : grid },
        fetch: async url => { urls.push(url); return { ok: !fail, text: async () => '' }; },
        DOMParser: class { parseFromString() { return { querySelectorAll: () => [{ style: {} }], getElementById: () => true }; } }
    };
    vm.runInNewContext(script, context);
    await click.call(button); fail = false;
    await click.call(button); await click.call(button);
    assert.deepEqual(urls.map(url => new URL(url, 'https://example.test').searchParams.get('page')), ['2', '2', '3']);
    for (const url of urls) {
        const params = new URL(url, 'https://example.test').searchParams;
        assert.equal(params.get('q'), 'Rare'); assert.equal(params.get('format'), 'T20'); assert.equal(params.get('type'), 'exhibition');
    }
    assert.equal(cards.length, 2);
});
