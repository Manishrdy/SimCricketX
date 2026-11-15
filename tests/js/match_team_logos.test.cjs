const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../..');
const fallbackSrc = fs.readFileSync(path.join(root, 'static/js/team_logo_fallback.js'), 'utf8');
const detailSrc = fs.readFileSync(path.join(root, 'static/js/match_detail.js'), 'utf8');

function makeClassList(initial = []) {
    const set = new Set(initial);
    return {
        add: (...names) => names.forEach(n => set.add(n)),
        remove: (...names) => names.forEach(n => set.delete(n)),
        contains: name => set.has(name),
    };
}

function makeImg(attrs = {}) {
    const listeners = new Map();
    const img = {
        tagName: 'IMG',
        dataset: {},
        _attrs: { ...attrs },
        _removed: false,
        _parent: null,
        complete: attrs.complete ?? false,
        naturalWidth: attrs.naturalWidth ?? 256,
        getAttribute(name) { return this._attrs[name] ?? null; },
        set src(v) { this._attrs.src = v; },
        get src() { return this._attrs.src; },
        closest(selector) {
            return selector === '[data-logo-slot]' ? this._parent : null;
        },
        addEventListener(evt, fn) { listeners.set(evt, fn); },
        emit(evt) {
            const fn = listeners.get(evt);
            if (fn) fn();
        },
        remove() {
            this._removed = true;
            if (this._parent && this._parent._img === this) {
                this._parent._img = null;
            }
        },
    };
    return img;
}

function setupFallback(imgs = []) {
    const context = {
        window: {},
        document: {
            readyState: 'complete',
            querySelectorAll: () => imgs,
            addEventListener: () => {},
        },
    };
    vm.createContext(context);
    vm.runInContext(fallbackSrc, context);
    return context.window.scxLogoFallback;
}

test('team_logo_fallback removes broken image on error and drops has-logo', () => {
    const slot = { classList: makeClassList(['has-logo']) };
    const img = makeImg({ src: '/team/1/logo.png?v=1' });
    img._parent = slot;
    slot._img = img;

    setupFallback([img]);
    assert.equal(img._removed, false);
    assert.equal(slot.classList.contains('has-logo'), true);

    img.emit('error');
    assert.equal(img._removed, true);
    assert.equal(slot.classList.contains('has-logo'), false);
});

test('team_logo_fallback handles images that failed before script ran', () => {
    const slot = { classList: makeClassList(['has-logo']) };
    const img = makeImg({ src: '/team/1/logo.png?v=1', complete: true, naturalWidth: 0 });
    img._parent = slot;
    slot._img = img;

    setupFallback([img]);
    assert.equal(img._removed, true);
    assert.equal(slot.classList.contains('has-logo'), false);
});

test('team_logo_fallback leaves valid loaded images intact', () => {
    const slot = { classList: makeClassList(['has-logo']) };
    const img = makeImg({ src: '/team/1/logo.png?v=1', complete: true, naturalWidth: 256 });
    img._parent = slot;
    slot._img = img;

    setupFallback([img]);
    assert.equal(img._removed, false);
    assert.equal(slot.classList.contains('has-logo'), true);
});

function setupBanner(teamLogos) {
    const batNameEl = { textContent: 'HOM' };
    const badgeEl = {
        classList: makeClassList(teamLogos.home ? ['has-logo'] : []),
        _img: teamLogos.home ? makeImg({ src: teamLogos.home }) : null,
        querySelector(sel) { return sel === 'img' ? this._img : null; },
        prepend(node) {
            node._parent = this;
            this._img = node;
        },
    };
    if (badgeEl._img) badgeEl._img._parent = badgeEl;

    const watched = [];
    const context = {
        matchData: {
            team_home: 'HOM_1',
            team_away: 'AWY_1',
            playing_xi: {
                home: [{ name: 'Home Batter' }],
                away: [{ name: 'Away Batter' }],
            },
        },
        window: {
            teamLogos,
            scxLogoFallback: { watch: img => watched.push(img) },
        },
        document: {
            getElementById(id) {
                if (id === 'sb-bat-name') return batNameEl;
                if (id === 'sb-bat-badge') return badgeEl;
                return null;
            },
            createElement(tag) {
                assert.equal(tag, 'img');
                return makeImg();
            },
        },
    };
    vm.createContext(context);
    const slice = detailSrc.slice(
        detailSrc.indexOf('function getBattingSide'),
        detailSrc.indexOf('function isImpactPlayerEnabled')
    );
    vm.runInContext(slice, context);
    return { context, batNameEl, badgeEl, watched };
}

test('score banner swaps batting team logo on toss/innings change and falls back when absent', () => {
    const { context, batNameEl, badgeEl, watched } = setupBanner({
        home: '/team/1/logo.png?v=1',
        away: '/team/2/logo.png?v=3',
    });

    // Switch to away team (with logo)
    context.setScoreBannerBattingTeam('AWY', 'away');
    assert.equal(batNameEl.textContent, 'AWY');
    assert.equal(badgeEl.classList.contains('has-logo'), true);
    assert.equal(badgeEl._img.src, '/team/2/logo.png?v=3');
    assert.equal(watched.length, 1);

    // Switch to a team without a logo
    context.window.teamLogos.home = null;
    context.setScoreBannerBattingTeam('HOM', 'home');
    assert.equal(batNameEl.textContent, 'HOM');
    assert.equal(badgeEl.classList.contains('has-logo'), false);
    assert.equal(badgeEl._img, null);
});

test('score banner uses fallback watcher if injected logo fails to load', () => {
    const fallback = setupFallback([]);
    const batNameEl = { textContent: 'HOM' };
    const badgeEl = {
        classList: makeClassList(),
        _img: null,
        querySelector(sel) { return sel === 'img' ? this._img : null; },
        prepend(node) {
            node._parent = this;
            this._img = node;
        },
    };
    const context = {
        matchData: { team_home: 'HOM_1', team_away: 'AWY_1', playing_xi: { home: [], away: [] } },
        window: {
            teamLogos: { home: null, away: '/team/2/logo.png?v=broken' },
            scxLogoFallback: fallback,
        },
        document: {
            getElementById(id) {
                if (id === 'sb-bat-name') return batNameEl;
                if (id === 'sb-bat-badge') return badgeEl;
                return null;
            },
            createElement: () => makeImg(),
        },
    };
    vm.createContext(context);
    const slice = detailSrc.slice(
        detailSrc.indexOf('function getBattingSide'),
        detailSrc.indexOf('function isImpactPlayerEnabled')
    );
    vm.runInContext(slice, context);

    context.setScoreBannerBattingTeam('AWY', 'away');
    assert.equal(badgeEl.classList.contains('has-logo'), true);
    badgeEl._img.emit('error');
    assert.equal(badgeEl.classList.contains('has-logo'), false);
    assert.equal(badgeEl._img, null);
    assert.equal(batNameEl.textContent, 'AWY');
});
