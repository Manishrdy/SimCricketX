/* SimCricketX onboarding guide — engine.
 *
 * Tour content lives in scx_guide_tours.js (window.SCX_GUIDE_TOURS); this file
 * only knows how to show it. Server context comes from the JSON island
 * #scx-guide-config (templates/partials/guide.html, routes/guide_routes.py).
 *
 * Two flows:
 *   - Page tours: auto-start once, the first time a page is opened; "?" replays.
 *   - Journey: brand-new accounts get a "Getting started" checklist
 *     (team → second team → first match) plus journey-aware tours.
 *
 * A step whose target isn't on the page is dropped when the tour starts, so a
 * layout change degrades a tour rather than breaking it.
 */
(function () {
    'use strict';

    const cfgEl = document.getElementById('scx-guide-config');
    if (!cfgEl) return;
    let cfg;
    try { cfg = JSON.parse(cfgEl.textContent); } catch (_) { return; }
    cfg.seen = cfg.seen || {};
    cfg.journey = cfg.journey || { status: 'none' };

    const TOURS = window.SCX_GUIDE_TOURS || {};
    const REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const SHEET_MAX = 640;

    function esc(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[ch]));
    }

    function journeyLive() {
        const j = cfg.journey;
        return (j.status === 'active' || j.status === 'eligible') && !!j.stage;
    }

    const ctx = {
        get page() { return cfg.page; },
        get journey() { return cfg.journey; },
        get seen() { return cfg.seen; },
        name: cfg.name || '',
        urls: cfg.urls || {},
        rules: Object.assign({ min: 11, max: 25, wk: 1, bowl: 5 }, cfg.rules || {}),
        esc,
        // In the journey and not yet finished it.
        get inJourney() { return journeyLive() && cfg.journey.stage !== 'complete'; },
        stage(...names) { return journeyLive() && names.indexOf(cfg.journey.stage) !== -1; },
    };

    const storage = window.scxStorage || {
        getItem() { return null; }, setItem() {}
    };

    // ── Persistence ───────────────────────────────────────────────────────
    function post(url, body) {
        if (cfg.readonly) return Promise.resolve(null);
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            // A tour's last button often navigates away straight after this
            // call; keepalive stops the navigation from cancelling the save.
            keepalive: true,
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': cfg.csrf || '' },
            body: JSON.stringify(body || {}),
        }).then(r => (r.ok ? r.json() : null)).catch(() => null);
    }

    function markTour(id, status) {
        if (cfg.seen[id] === 'done' && status === 'skipped') return;
        cfg.seen[id] = status;
        post(cfg.urls.progress, { tour: id, status });
    }

    function setJourney(status) {
        cfg.journey = Object.assign({}, cfg.journey, { status });
        post(cfg.urls.progress, { journey: status });
    }

    // ── Helpers ───────────────────────────────────────────────────────────
    const val = (v, ...args) => (typeof v === 'function' ? v(ctx, ...args) : v);

    function isVisible(el) {
        if (!el || !el.isConnected) return false;
        if (el.closest('[hidden], [aria-hidden="true"]') && !el.matches('[data-guide-visible]')) return false;
        const rects = el.getClientRects();
        if (!rects.length) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return false;
        const cs = window.getComputedStyle(el);
        return cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity) > 0.05;
    }

    function findTarget(target) {
        if (!target) return null;
        const selectors = Array.isArray(target) ? target : [target];
        for (const sel of selectors) {
            let nodes;
            try { nodes = document.querySelectorAll(sel); } catch (_) { continue; }
            for (const node of nodes) if (isVisible(node)) return node;
        }
        return null;
    }

    function blockingDialogOpen() {
        const nodes = document.querySelectorAll('[role="dialog"][aria-modal="true"], dialog[open]');
        for (const node of nodes) {
            if (node.closest('.scxg-layer')) continue;
            if (node.getAttribute('aria-hidden') === 'true') continue;
            if (isVisible(node)) return true;
        }
        return false;
    }

    function isSheet() { return window.innerWidth < SHEET_MAX; }

    // ── Tour runner ───────────────────────────────────────────────────────
    let active = null;   // { tour, steps, index, manual, target, prevFocus, raf, lastKey }
    let layer = null;

    function buildLayer() {
        if (layer) return layer;
        layer = document.createElement('div');
        layer.className = 'scxg-layer';
        layer.innerHTML = `
            <div class="scxg-blocker" data-scxg-blocker></div>
            <div class="scxg-spot" aria-hidden="true"></div>
            <div class="scxg-pop" role="dialog" aria-modal="true" aria-labelledby="scxg-title" aria-describedby="scxg-body" tabindex="-1">
                <div class="scxg-pop-head">
                    <span class="scxg-kicker"></span>
                    <button type="button" class="scxg-x" data-scxg="close" aria-label="Close guide">
                        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3.5 3.5l9 9m0-9l-9 9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
                    </button>
                </div>
                <h2 class="scxg-title" id="scxg-title"></h2>
                <div class="scxg-body" id="scxg-body"></div>
                <div class="scxg-foot">
                    <div class="scxg-progress">
                        <div class="scxg-dots" aria-hidden="true"></div>
                        <span class="scxg-count"></span>
                    </div>
                    <div class="scxg-actions">
                        <button type="button" class="scxg-btn scxg-btn-ghost" data-scxg="skip">Skip tour</button>
                        <button type="button" class="scxg-btn scxg-btn-soft" data-scxg="back">Back</button>
                        <button type="button" class="scxg-btn scxg-btn-primary" data-scxg="next">Next</button>
                    </div>
                </div>
            </div>`;
        layer.addEventListener('click', onLayerClick);
        document.body.appendChild(layer);
        return layer;
    }

    function onLayerClick(event) {
        const btn = event.target.closest('[data-scxg]');
        if (!btn || !active) return;
        const action = btn.getAttribute('data-scxg');
        if (action === 'next') next();
        else if (action === 'back') go(active.index - 1, -1);
        else if (action === 'skip' || action === 'close') finish('skipped');
    }

    function onKey(event) {
        if (!active) return;
        if (event.key === 'Escape') { event.preventDefault(); finish('skipped'); return; }
        const inField = /^(INPUT|TEXTAREA|SELECT)$/.test((event.target && event.target.tagName) || '');
        if (inField) return;
        if (event.key === 'ArrowRight') { event.preventDefault(); next(); }
        else if (event.key === 'ArrowLeft' && active.index > 0) { event.preventDefault(); go(active.index - 1, -1); }
        else if (event.key === 'Tab') trapTab(event);
    }

    function trapTab(event) {
        const pop = layer.querySelector('.scxg-pop');
        const items = Array.from(pop.querySelectorAll('button, a[href]')).filter(n => !n.hidden && n.offsetParent !== null);
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (!pop.contains(document.activeElement)) { event.preventDefault(); first.focus(); return; }
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }

    function start(tourId, opts) {
        const tour = TOURS[tourId];
        if (!tour) return false;
        opts = opts || {};
        if (active) teardown();

        const steps = (tour.steps || []).filter(step => {
            if (step.when && !step.when(ctx)) return false;
            if (!step.target) return true;
            return !!findTarget(step.target) || step.fallback === 'center';
        });
        if (!steps.length) return false;

        buildLayer();
        active = {
            tour, steps, index: 0, manual: !!opts.manual,
            prevFocus: document.activeElement, target: null, raf: 0, lastKey: '',
        };
        document.addEventListener('keydown', onKey, true);
        document.documentElement.classList.add('scxg-open');
        layer.classList.add('is-open');
        layer.classList.toggle('scxg-reduced', !!REDUCED);
        go(0, 1);
        return true;
    }

    function next() {
        if (!active) return;
        const step = active.steps[active.index];
        if (active.index >= active.steps.length - 1) {
            const cta = step.cta ? val(step.cta) : null;
            finish('done');
            if (cta && cta.href) window.location.href = cta.href;
            else if (cta && cta.tour) setTimeout(() => start(cta.tour, { manual: true }), 50);
            return;
        }
        go(active.index + 1, 1);
    }

    function go(index, dir) {
        if (!active) return;
        const steps = active.steps;
        // Targets can vanish between start and now (e.g. a panel collapsed);
        // walk past them in the direction of travel.
        while (index >= 0 && index < steps.length) {
            const step = steps[index];
            if (!step.target || findTarget(step.target) || step.fallback === 'center') break;
            index += dir;
        }
        if (index < 0) index = 0;
        if (index >= steps.length) { finish('done'); return; }
        active.index = index;
        render();
    }

    function render() {
        const { steps, index, tour } = active;
        const step = steps[index];
        const pop = layer.querySelector('.scxg-pop');
        const target = findTarget(step.target);
        active.target = target;
        active.lastKey = '';

        const kicker = val(step.kicker !== undefined ? step.kicker : tour.kicker) || '';
        pop.querySelector('.scxg-kicker').textContent = kicker;
        pop.querySelector('.scxg-kicker').hidden = !kicker;
        pop.querySelector('.scxg-title').textContent = val(step.title) || '';
        pop.querySelector('.scxg-body').innerHTML = val(step.body) || '';

        const total = steps.length;
        pop.querySelector('.scxg-count').textContent = total > 1 ? `${index + 1} of ${total}` : '';
        const dots = pop.querySelector('.scxg-dots');
        dots.innerHTML = total > 1
            ? steps.map((_, i) => `<span class="${i === index ? 'is-current' : i < index ? 'is-done' : ''}"></span>`).join('')
            : '';

        const last = index === total - 1;
        const cta = last && step.cta ? val(step.cta) : null;
        const nextBtn = pop.querySelector('[data-scxg="next"]');
        nextBtn.textContent = cta ? cta.label : last ? (total > 1 ? 'Finish' : 'Got it') : 'Next';
        pop.querySelector('[data-scxg="back"]').hidden = index === 0;
        const skipBtn = pop.querySelector('[data-scxg="skip"]');
        skipBtn.hidden = last && !cta;
        skipBtn.textContent = last && cta ? (step.ctaDismiss || 'Not now') : (tour.skipLabel || 'Skip tour');

        layer.classList.toggle('is-centered', !target);
        layer.classList.toggle('is-sheet', isSheet());
        pop.classList.remove('is-in');
        // Restart the entrance animation on each step.
        void pop.offsetWidth;
        pop.classList.add('is-in');

        if (target) scrollToTarget(target, pop);
        startTracking();
        requestAnimationFrame(() => nextBtn.focus({ preventScroll: true }));
    }

    function scrollToTarget(el, pop) {
        const r = el.getBoundingClientRect();
        const vh = window.innerHeight;
        const navH = 72;
        const sheetH = isSheet() ? Math.min(pop.offsetHeight + 24, vh * 0.55) : 0;
        const room = vh - navH - sheetH;
        const fits = r.height <= room - 24;
        const topInView = r.top >= navH && r.bottom <= vh - sheetH;
        if (topInView && (fits || r.top < vh / 3)) return;
        const desired = fits ? navH + (room - r.height) / 2 : navH + 12;
        window.scrollBy({ top: r.top - desired, behavior: REDUCED ? 'auto' : 'smooth' });
    }

    function startTracking() {
        cancelAnimationFrame(active.raf);
        const tick = () => {
            if (!active) return;
            position();
            active.raf = requestAnimationFrame(tick);
        };
        tick();
    }

    function position() {
        const spot = layer.querySelector('.scxg-spot');
        const pop = layer.querySelector('.scxg-pop');
        const target = active.target;
        const sheet = isSheet();
        const vw = window.innerWidth;
        const vh = window.innerHeight;

        if (!target || !target.isConnected) {
            const key = `c:${vw}x${vh}:${sheet}`;
            if (key === active.lastKey) return;
            active.lastKey = key;
            layer.classList.add('is-centered');
            layer.classList.toggle('is-sheet', sheet);
            spot.style.cssText = '';
            pop.style.left = pop.style.top = '';
            return;
        }

        const pad = active.steps[active.index].padding != null ? active.steps[active.index].padding : 6;
        const r = target.getBoundingClientRect();
        const key = `${Math.round(r.left)},${Math.round(r.top)},${Math.round(r.width)},${Math.round(r.height)}:${vw}x${vh}:${pop.offsetHeight}`;
        if (key === active.lastKey) return;
        active.lastKey = key;
        layer.classList.toggle('is-sheet', sheet);

        const radius = Math.min(16, parseFloat(getComputedStyle(target).borderRadius) + pad || 10);
        spot.style.left = `${r.left - pad}px`;
        spot.style.top = `${r.top - pad}px`;
        spot.style.width = `${r.width + pad * 2}px`;
        spot.style.height = `${r.height + pad * 2}px`;
        spot.style.borderRadius = `${radius}px`;

        if (sheet) { pop.style.left = pop.style.top = ''; return; }

        const pw = pop.offsetWidth;
        const ph = pop.offsetHeight;
        const gap = 14;
        const margin = 12;
        const pref = active.steps[active.index].placement;
        const order = [pref, 'bottom', 'top', 'right', 'left'].filter((p, i, a) => p && a.indexOf(p) === i);
        const clampX = x => Math.max(margin, Math.min(x, vw - pw - margin));
        const clampY = y => Math.max(margin, Math.min(y, vh - ph - margin));
        const spots = {
            bottom: { x: clampX(r.left + r.width / 2 - pw / 2), y: r.bottom + pad + gap, ok: r.bottom + pad + gap + ph <= vh - margin },
            top: { x: clampX(r.left + r.width / 2 - pw / 2), y: r.top - pad - gap - ph, ok: r.top - pad - gap - ph >= margin },
            right: { x: r.right + pad + gap, y: clampY(r.top + r.height / 2 - ph / 2), ok: r.right + pad + gap + pw <= vw - margin },
            left: { x: r.left - pad - gap - pw, y: clampY(r.top + r.height / 2 - ph / 2), ok: r.left - pad - gap - pw >= margin },
        };
        let chosen = order.map(p => spots[p]).find(s => s.ok);
        // Nothing fits beside a very large target: float over its lower edge.
        if (!chosen) chosen = { x: clampX(r.left + r.width / 2 - pw / 2), y: clampY(vh - ph - margin * 2) };
        pop.style.left = `${Math.round(chosen.x)}px`;
        pop.style.top = `${Math.round(chosen.y)}px`;
    }

    function teardown() {
        if (!active) return;
        cancelAnimationFrame(active.raf);
        document.removeEventListener('keydown', onKey, true);
        document.documentElement.classList.remove('scxg-open');
        if (layer) layer.classList.remove('is-open', 'is-centered', 'is-sheet');
        const prev = active.prevFocus;
        active = null;
        if (prev && prev.isConnected && typeof prev.focus === 'function') {
            try { prev.focus({ preventScroll: true }); } catch (_) { /* ignore */ }
        }
    }

    function finish(status) {
        if (!active) return;
        const tour = active.tour;
        teardown();
        if (tour.persist !== false) markTour(tour.id, status);
        if (typeof tour.onEnd === 'function') tour.onEnd(ctx, status, api);
        renderJourney();
    }

    // ── Auto-start ────────────────────────────────────────────────────────
    function toursForPage() {
        return Object.values(TOURS).filter(t => t.page === cfg.page || t.page === '*');
    }

    function eligible(tour) {
        if (cfg.seen[tour.id]) return false;
        return !tour.auto || !!tour.auto(ctx);
    }

    function whenClear(fn, tries) {
        tries = tries || 0;
        if (active) return;
        if (blockingDialogOpen() && tries < 90) { setTimeout(() => whenClear(fn, tries + 1), 800); return; }
        fn();
    }

    function autoStart() {
        if (cfg.readonly) return;
        const forced = new URLSearchParams(window.location.search).get('guide');
        if (forced) {
            // One-shot: a reload shouldn't replay it.
            const url = new URL(window.location.href);
            url.searchParams.delete('guide');
            try { window.history.replaceState(window.history.state, '', url.toString()); } catch (_) { /* ignore */ }
        }
        if (forced && TOURS[forced] && (TOURS[forced].page === cfg.page || TOURS[forced].page === '*')) {
            whenClear(() => start(forced, { manual: true }));
            return;
        }
        const candidates = toursForPage()
            .filter(t => !t.waitFor && eligible(t))
            .sort((a, b) => (b.priority || 0) - (a.priority || 0));
        if (candidates.length) whenClear(() => { for (const t of candidates) if (start(t.id)) break; });
    }

    // Tours that start when part of the page appears (e.g. wizard step 2).
    function watchDeferred() {
        const deferred = toursForPage().filter(t => t.waitFor);
        if (!deferred.length) return;
        let queued = false;
        let retries = 0;
        const check = () => {
            queued = false;
            if (active || cfg.readonly) return;
            let pending = false;
            for (const t of deferred) {
                if (!eligible(t)) continue;
                if (findTarget(t.waitFor)) { retries = 0; whenClear(() => start(t.id)); return; }
                // In the DOM but not visible yet: usually mid fade-in, and no
                // further mutation will arrive to trigger another check.
                try { pending = pending || !!document.querySelector(t.waitFor); } catch (_) { /* bad selector */ }
            }
            if (pending && retries < 12) { retries += 1; queued = true; setTimeout(check, 250); }
            else retries = 0;
        };
        const mo = new MutationObserver(() => {
            if (!queued) { queued = true; setTimeout(check, 450); }
        });
        mo.observe(document.body, { attributes: true, attributeFilter: ['class', 'hidden', 'style'], subtree: true, childList: true });
    }

    // ── Replay ("?" button) ───────────────────────────────────────────────
    function replay() {
        const tours = toursForPage().filter(t => t.replay !== false && t.page === cfg.page);
        // A step-specific tour whose section is currently showing wins.
        const live = tours.filter(t => t.waitFor && findTarget(t.waitFor));
        const main = tours.find(t => t.main) || tours.find(t => !t.waitFor);
        const pick = live[live.length - 1] || main;
        if (pick && start(pick.id, { manual: true })) return;
        start('no_tour', { manual: true });
    }

    function resetAll() {
        post(cfg.urls.reset, {}).then(res => {
            if (!res) return;
            const url = new URL(window.location.href);
            url.searchParams.delete('guide');
            window.location.assign(url.toString());
        });
    }

    // ── Journey checklist ─────────────────────────────────────────────────
    const JOURNEY = [
        { stage: 'team1', label: 'Create your first team' },
        { stage: 'team2', label: 'Create a second team' },
        { stage: 'match', label: 'Simulate your first match' },
    ];
    const STAGE_ORDER = ['team1', 'team2', 'match', 'complete'];

    function journeyAction() {
        const j = cfg.journey;
        const u = cfg.urls;
        if (j.stage === 'team1' || j.stage === 'team2') {
            if (cfg.page === 'team_create') return { label: 'Show me how', tour: 'team_create' };
            const draftHint = j.drafts > 0
                ? `You have ${j.drafts} draft team${j.drafts === 1 ? '' : 's'}. A draft only counts once its squad is complete, so finish it in <a href="${esc(u.manageTeams)}">Manage Teams</a> or build a new one.`
                : '';
            return {
                label: j.stage === 'team1' ? 'Create a team' : 'Create team #2',
                href: u.createTeam,
                hint: draftHint || (j.stage === 'team1'
                    ? `Pick ${ctx.rules.min}–${ctx.rules.max} players from the player pool, choose a captain and a wicketkeeper, then save.`
                    : 'One down! A match needs two sides, so build an opponent the same way.'),
            };
        }
        if (j.stage === 'match') {
            if (cfg.page === 'match_setup') return { label: 'Show me how', tour: 'match_setup', hint: 'Pick your two teams, set the conditions, confirm the XIs and press Start.' };
            return { label: 'Set up a match', href: u.matchSetup, hint: 'Both teams are ready. Pick them, set the conditions and watch it play out ball by ball.' };
        }
        return null;
    }

    function renderJourney() {
        const old = document.querySelector('.scxg-journey');
        if (old) old.remove();
        const legacyHint = document.querySelector('.home-guide');
        const j = cfg.journey;
        const show = journeyLive() && j.stage !== 'complete' && cfg.page !== 'match_detail';
        // .home-guide is display:flex, which beats the [hidden] attribute.
        if (legacyHint) legacyHint.style.display = show ? 'none' : '';
        if (!show) return;

        const mount = document.getElementById('scxg-journey-mount');
        const inline = !!mount;
        const current = STAGE_ORDER.indexOf(j.stage);
        const action = journeyAction();
        let collapsed = false;
        if (!inline) {
            // On the page where the current step happens (action.tour set),
            // the user is busy with the form: start as a pill, out of the way.
            const saved = storage.getItem('scxg-journey-collapsed');
            collapsed = (action && action.tour) || isSheet() ? true : saved === '1';
        }

        const card = document.createElement('aside');
        card.className = `scxg-journey ${inline ? 'is-inline' : 'is-floating'}${collapsed ? ' is-collapsed' : ''}`;
        card.setAttribute('aria-label', 'Getting started');
        card.innerHTML = `
            <button type="button" class="scxg-j-pill" data-j="expand" aria-expanded="false">
                <span class="scxg-j-ring" style="--p:${current / 3}"><span>${current}/3</span></span>
                <span>Getting started</span>
            </button>
            <div class="scxg-j-panel">
                <div class="scxg-j-head">
                    <div>
                        <span class="scxg-j-kicker">Getting started · Step ${Math.min(current + 1, 3)} of 3</span>
                        <h2 class="scxg-j-title">Play your first match</h2>
                    </div>
                    ${inline ? '' : '<button type="button" class="scxg-j-min" data-j="collapse" aria-expanded="true" aria-label="Minimise checklist"><svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3.5 8h9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button>'}
                </div>
                <ol class="scxg-j-steps">
                    ${JOURNEY.map((s, i) => {
                        const state = i < current ? 'is-done' : i === current ? 'is-current' : '';
                        const mark = i < current
                            ? '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
                            : String(i + 1);
                        const sr = i < current ? ' (done)' : i === current ? ' (current step)' : '';
                        return `<li class="${state}"><span class="scxg-j-mark">${mark}</span><span>${esc(s.label)}<span class="scxg-sr">${sr}</span></span></li>`;
                    }).join('')}
                </ol>
                ${action && action.hint ? `<p class="scxg-j-hint">${action.hint}</p>` : ''}
                <div class="scxg-j-actions">
                    ${action ? (action.href
                        ? `<a class="scxg-btn scxg-btn-primary" href="${esc(action.href)}">${esc(action.label)}<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3 8h9m-3.5-4L12 8l-3.5 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></a>`
                        : `<button type="button" class="scxg-btn scxg-btn-primary" data-j="tour" data-tour="${esc(action.tour)}">${esc(action.label)}</button>`) : ''}
                    <button type="button" class="scxg-btn scxg-btn-ghost" data-j="dismiss">Skip getting started</button>
                </div>
                <div class="scxg-j-confirm" hidden>
                    <p>Hide this checklist for good? Page guides stay available from the <strong>?</strong> button.</p>
                    <div class="scxg-j-actions">
                        <button type="button" class="scxg-btn scxg-btn-soft" data-j="dismiss-yes">Yes, hide it</button>
                        <button type="button" class="scxg-btn scxg-btn-ghost" data-j="dismiss-no">Keep it</button>
                    </div>
                </div>
            </div>`;

        card.addEventListener('click', event => {
            const btn = event.target.closest('[data-j]');
            if (!btn) return;
            const what = btn.getAttribute('data-j');
            if (what === 'expand' || what === 'collapse') {
                const nowCollapsed = what === 'collapse';
                card.classList.toggle('is-collapsed', nowCollapsed);
                if (!(action && action.tour) && !isSheet()) storage.setItem('scxg-journey-collapsed', nowCollapsed ? '1' : '0');
                const focusEl = card.querySelector(nowCollapsed ? '[data-j="expand"]' : '[data-j="collapse"]');
                if (focusEl) focusEl.focus();
            } else if (what === 'tour') {
                start(btn.getAttribute('data-tour'), { manual: true });
            } else if (what === 'dismiss') {
                card.querySelector('.scxg-j-confirm').hidden = false;
                btn.closest('.scxg-j-actions').hidden = true;
                card.querySelector('[data-j="dismiss-no"]').focus();
            } else if (what === 'dismiss-no') {
                card.querySelector('.scxg-j-confirm').hidden = true;
                const actions = card.querySelector('.scxg-j-actions');
                actions.hidden = false;
                card.querySelector('[data-j="dismiss"]').focus();
            } else if (what === 'dismiss-yes') {
                setJourney('dismissed');
                renderJourney();
            }
        });

        if (inline) mount.appendChild(card);
        else document.body.appendChild(card);
    }

    // ── Public API + boot ─────────────────────────────────────────────────
    const api = { start, replay, resetAll, setJourney, ctx, isOpen: () => !!active };
    window.SCXGuide = api;

    function boot() {
        // A brand-new account starts the journey the first time it sees any
        // guided page; recording it pins them as a "new user" even after they
        // build their first team (which would otherwise make them look existing).
        if (cfg.journey.status === 'eligible') setJourney('active');

        document.addEventListener('click', event => {
            if (event.target.closest('[data-guide-replay]')) { event.preventDefault(); replay(); }
            else if (event.target.closest('[data-guide-reset]')) { event.preventDefault(); resetAll(); }
        });
        renderJourney();
        setTimeout(autoStart, 500);
        watchDeferred();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
})();
