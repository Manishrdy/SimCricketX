const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.resolve(__dirname, '../../static/js/copy_match_log.js'),
    'utf8'
);

function setup(navigatorValue) {
    const handlers = {};
    const attributes = {};
    const timers = [];
    let selected = 0;
    const elements = {
        'copy-log-btn': {
            innerHTML: '<i></i> Copy',
            style: {},
            disabled: false,
            addEventListener(type, handler) {
                handlers.copy = handler;
            },
            setAttribute(name, value) {
                attributes[name] = value;
            },
            focus() {},
        },
        'commentary-log': { innerText: 'Over 1.1: FOUR' },
        'copy-log-fallback': { hidden: true },
        'copy-log-text': {
            value: '',
            focus() {},
            select() {
                selected += 1;
            },
            setSelectionRange() {},
        },
        'copy-log-status': { textContent: '' },
        'select-log-text': {
            addEventListener(type, handler) {
                handlers.select = handler;
            },
        },
        'close-copy-fallback': {
            addEventListener(type, handler) {
                handlers.close = handler;
            },
        },
    };
    const context = {
        document: {
            getElementById(id) {
                return elements[id];
            },
        },
        navigator: navigatorValue,
        setTimeout(callback) {
            timers.push(callback);
        },
    };
    vm.runInNewContext(source, context);
    return { attributes, elements, handlers, selected: () => selected, timers };
}

test('missing clipboard API exposes selected text and visible instructions', async () => {
    const page = setup({});

    await page.handlers.copy();

    assert.equal(page.elements['copy-log-fallback'].hidden, false);
    assert.equal(page.elements['copy-log-text'].value, 'Over 1.1: FOUR');
    assert.equal(page.selected(), 1);
    assert.match(page.elements['copy-log-status'].textContent, /Automatic copy is unavailable/);
    assert.equal(page.attributes['aria-expanded'], 'true');
});

test('rejected clipboard write falls back instead of failing silently', async () => {
    const page = setup({
        clipboard: {
            async writeText() {
                throw new Error('permission denied');
            },
        },
    });

    await page.handlers.copy();

    assert.equal(page.elements['copy-log-fallback'].hidden, false);
    assert.equal(page.elements['copy-log-text'].value, 'Over 1.1: FOUR');
    assert.equal(page.elements['copy-log-btn'].disabled, false);
});

test('successful clipboard write keeps the existing copied feedback', async () => {
    let copiedText = '';
    const page = setup({
        clipboard: {
            async writeText(value) {
                copiedText = value;
            },
        },
    });

    await page.handlers.copy();

    assert.equal(copiedText, 'Over 1.1: FOUR');
    assert.match(page.elements['copy-log-btn'].innerHTML, /Copied!/);
    assert.equal(page.elements['copy-log-fallback'].hidden, true);
    assert.equal(page.timers.length, 1);
});
