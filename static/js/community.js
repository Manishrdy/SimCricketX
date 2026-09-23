/* Community board page script: composer (new/edit) and post page.
 * All user text is written with textContent; server-rendered HTML is escaped
 * by Jinja. Config comes from <script type="application/json" id="cm-config">.
 */
(function () {
  'use strict';

  const cfgEl = document.getElementById('cm-config');
  if (!cfgEl) return;
  const cfg = JSON.parse(cfgEl.textContent);
  const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  let mentionSeq = 0;  // ids for @-tag dropdowns (declared before the init calls below run)

  async function api(url, method, body, isForm) {
    const opts = { method, headers: { 'X-CSRFToken': csrf, 'Accept': 'application/json' }, credentials: 'same-origin' };
    if (body !== undefined) {
      if (isForm) opts.body = body;
      else { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    }
    let res, data = {};
    try {
      res = await fetch(url, opts);
      data = await res.json().catch(() => ({}));
    } catch (e) {
      return { ok: false, status: 0, data: { error: 'Network error. Check your connection and try again.' } };
    }
    if (res.status === 429 && !data.error) data.error = 'You are doing that too often. Please wait a bit.';
    return { ok: res.ok, status: res.status, data };
  }

  function toast(msg) {
    const t = document.createElement('div');
    t.className = 'cm-toast';
    t.setAttribute('role', 'status');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3200);
  }

  function reloadAt(hash) {
    if (hash) history.replaceState(null, '', location.pathname + location.search + '#' + hash);
    location.reload();
  }

  if (cfg.mode === 'new' || cfg.mode === 'edit') initComposer();
  if (cfg.mode === 'post') initPost();
  if (cfg.mode === 'index' || cfg.mode === 'post') initVoting();
  if (cfg.mode === 'index') initSearch();

  // ── @-tags: autocomplete for any textarea/input ─────────────────────────
  // Typing '@' offers people from /api/community/mentions/suggest. Picking one
  // writes '@Display Name ' and remembers their stable id in state.picked; on
  // submit, collectMentions() sends the ids whose '@name' is still in the
  // text. The server re-checks every rule, so this is only a convenience.
  function newMentionState() { return { picked: new Map() }; }

  function collectMentions(state, texts) {
    const all = texts.filter(Boolean).join('\n').toLowerCase();
    return [...state.picked].filter(([, name]) => all.includes('@' + name.toLowerCase())).map(([id]) => id);
  }

  function initMentions(field, state, opts) {
    const url = cfg.endpoints && cfg.endpoints.mention_suggest;
    if (!url || !field || field.dataset.mentions) return;
    field.dataset.mentions = '1';
    opts = opts || {};
    const menu = document.createElement('div');
    menu.className = 'cm-mention-menu';
    menu.id = 'cm-mention-menu-' + (++mentionSeq);
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('aria-label', 'People to tag');
    menu.hidden = true;
    (field.closest('.cm-wrap') || document.body).appendChild(menu);
    field.setAttribute('aria-autocomplete', 'list');
    field.setAttribute('aria-controls', menu.id);
    field.setAttribute('aria-expanded', 'false');

    let people = [], active = -1, token = null, ctrl = null, timer = null, composing = false, dead = null;

    // The '@query' just before the caret, or null. Names can contain spaces,
    // so the query may too (up to 3 words, 30 characters).
    function currentToken() {
      const pos = field.selectionStart;
      if (pos == null || pos !== field.selectionEnd) return null;
      const before = field.value.slice(0, pos);
      const at = before.lastIndexOf('@');
      if (at < 0 || (at > 0 && !/[\s(\[{"']/.test(before[at - 1]))) return null;
      const q = before.slice(at + 1);
      if (q.length > 30 || /[\n@]/.test(q) || /^\s|\s{2}/.test(q) || q.split(' ').length > 3) return null;
      const lq = q.toLowerCase();
      for (const name of state.picked.values()) {
        if (lq.startsWith(name.toLowerCase() + ' ')) return null;  // already tagged, now typing on
      }
      return { start: at, end: pos, q };
    }

    function place() {
      const r = field.getBoundingClientRect();
      const width = Math.min(r.width, 360);
      menu.style.width = width + 'px';
      menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8)) + 'px';
      const below = window.innerHeight - r.bottom;
      if (below < 220 && r.top > below) { menu.style.top = ''; menu.style.bottom = (window.innerHeight - r.top + 4) + 'px'; }
      else { menu.style.bottom = ''; menu.style.top = (r.bottom + 4) + 'px'; }
    }

    function close() {
      menu.hidden = true;
      field.setAttribute('aria-expanded', 'false');
      field.removeAttribute('aria-activedescendant');
      people = []; active = -1;
    }

    function setActive(i) {
      active = i;
      [...menu.children].forEach((el, j) => el.classList.toggle('is-active', j === i));
      if (i >= 0 && menu.children[i]) {
        field.setAttribute('aria-activedescendant', menu.children[i].id);
        menu.children[i].scrollIntoView({ block: 'nearest' });
      }
    }

    function render() {
      menu.replaceChildren();
      if (!people.length) { close(); return; }
      people.forEach((p, i) => {
        const opt = document.createElement('div');
        opt.className = 'cm-mention-opt';
        opt.id = menu.id + '-' + i;
        opt.setAttribute('role', 'option');
        const av = document.createElement('span');
        av.className = 'cm-avatar' + (p.is_admin ? ' cm-avatar--admin' : '');
        av.style.setProperty('--h', p.hue || 210);
        av.setAttribute('aria-hidden', 'true');
        av.textContent = p.name.slice(0, 1).toUpperCase();
        const name = document.createElement('span');
        name.className = 'cm-mention-opt__name';
        name.textContent = p.name;
        opt.append(av, name);
        if (p.is_admin) {
          const b = document.createElement('span');
          b.className = 'cm-admin-badge';
          b.innerHTML = '<i class="fas fa-shield-halved" aria-hidden="true"></i>Admin';
          opt.appendChild(b);
        } else if (p.in_thread) {
          const b = document.createElement('span');
          b.className = 'cm-mention-opt__hint';
          b.textContent = 'In this thread';
          opt.appendChild(b);
        }
        opt.addEventListener('mousedown', e => { e.preventDefault(); pick(i); });
        menu.appendChild(opt);
      });
      place();
      menu.hidden = false;
      field.setAttribute('aria-expanded', 'true');
      setActive(0);
    }

    async function fetchPeople(q) {
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      const params = new URLSearchParams({ q });
      const postId = opts.postId ? opts.postId() : null;
      if (postId) params.set('post', postId);
      if (opts.isPrivate && opts.isPrivate()) params.set('private', '1');
      try {
        const res = await fetch(`${url}?${params}`, { credentials: 'same-origin', signal: ctrl.signal,
                                                      headers: { 'Accept': 'application/json' } });
        if (!res.ok) { close(); return; }
        const data = await res.json();
        const t = currentToken();
        if (!t || t.q !== q) return;  // the text moved on while we waited
        people = data.people || [];
        // A substring search that found nobody can't find anybody for a longer query.
        dead = people.length || !q ? null : q.toLowerCase();
        render();
      } catch (e) {
        if (e.name !== 'AbortError') close();
      }
    }

    function onInput() {
      if (composing) return;
      token = currentToken();
      clearTimeout(timer);
      if (!token || (dead && token.q.toLowerCase().startsWith(dead))) { if (ctrl) ctrl.abort(); close(); return; }
      timer = setTimeout(() => fetchPeople(token.q), 120);
    }

    function pick(i) {
      const p = people[i];
      const t = currentToken() || token;
      if (!p || !t) return;
      const insert = '@' + p.name + ' ';
      field.value = field.value.slice(0, t.start) + insert + field.value.slice(t.end);
      const caret = t.start + insert.length;
      field.setSelectionRange(caret, caret);
      state.picked.set(p.id, p.name);
      close();
      field.dispatchEvent(new Event('input', { bubbles: true }));  // counters
      field.focus();
    }

    field.addEventListener('input', onInput);
    field.addEventListener('click', onInput);
    field.addEventListener('compositionstart', () => { composing = true; });
    field.addEventListener('compositionend', () => { composing = false; onInput(); });
    field.addEventListener('blur', () => setTimeout(close, 120));
    field.addEventListener('keydown', e => {
      if (menu.hidden || e.isComposing) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); setActive((active + 1) % people.length); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((active - 1 + people.length) % people.length); }
      else if (e.key === 'Enter' || e.key === 'Tab') { if (active >= 0) { e.preventDefault(); e.stopImmediatePropagation(); pick(active); } }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); close(); }
    });
    window.addEventListener('resize', () => { if (!menu.hidden) place(); });
    window.addEventListener('scroll', () => { if (!menu.hidden) place(); }, true);
  }

  // ── Live search + autocomplete (index page) ───────────────────────────────
  // Suggestions come from /api/community/search on every keystroke (FTS5
  // prefix match, so half-typed words already hit). The results list below
  // is re-rendered server-side (?partial=1) a moment later. Newer keystrokes
  // abort older requests, so a slow response never overwrites a newer one.
  function initSearch() {
    const form = document.getElementById('cm-search');
    const input = document.getElementById('cm-q');
    const box = document.getElementById('cm-suggest');
    const results = document.getElementById('cm-results');
    if (!form || !input || !box || !results) return;
    const browse = [document.getElementById('cm-browse-controls'), document.getElementById('cm-browse-chips')];
    const baseParams = new URLSearchParams(location.search);
    baseParams.delete('q'); baseParams.delete('cursor');
    let suggestTimer = null, resultsTimer = null, suggestCtrl = null, resultsCtrl = null;
    let items = [], active = -1, lastQuery = input.value.trim(), terms = [], composing = false;

    const escapeRe = t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    function highlight(text, words) {
      const frag = document.createDocumentFragment();
      const list = (words || []).filter(Boolean);
      if (!list.length) { frag.appendChild(document.createTextNode(text)); return frag; }
      const re = new RegExp('\\b(' + list.map(escapeRe).join('|') + ')[\\w\']*', 'gi');
      let last = 0, m;
      while ((m = re.exec(text))) {
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        const mark = document.createElement('mark'); mark.textContent = m[0]; frag.appendChild(mark);
        last = m.index + m[0].length;
        if (!m[0].length) re.lastIndex++;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      return frag;
    }
    function highlightResults(words) {
      results.querySelectorAll('.cm-card__link').forEach(a => {
        const text = a.textContent;
        a.replaceChildren(highlight(text, words));
      });
    }
    const queryTerms = q => (q.toLowerCase().match(/[a-z0-9]{2,}/g) || []);

    function open(isOpen) {
      box.hidden = !isOpen;
      input.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      if (!isOpen) { active = -1; input.removeAttribute('aria-activedescendant'); }
    }
    function setActive(i) {
      items.forEach(el => el.classList.remove('is-active'));
      active = items.length ? (i + items.length) % items.length : -1;
      if (active >= 0) {
        items[active].classList.add('is-active');
        input.setAttribute('aria-activedescendant', items[active].id);
        items[active].scrollIntoView({ block: 'nearest' });
      }
    }

    function renderSuggestions(data) {
      box.replaceChildren();
      items = [];
      const q = data.query;
      if (data.suggestions.length) {
        const label = document.createElement('div');
        label.className = 'cm-suggest__label';
        label.textContent = data.total > data.suggestions.length ? `Top matches · ${data.total} posts` : 'Matching posts';
        box.appendChild(label);
      }
      data.suggestions.forEach((p, i) => {
        const a = document.createElement('a');
        a.className = 'cm-suggest__item'; a.href = p.url; a.id = `cm-sg-${i}`;
        a.setAttribute('role', 'option');
        const icon = document.createElement('span');
        icon.className = `cm-suggest__icon cm-flair--${p.flair}`;
        icon.innerHTML = `<i class="fas ${p.flair_icon.replace(/[^\w-]/g, '')}" aria-hidden="true"></i>`;
        const title = document.createElement('span');
        title.className = 'cm-suggest__title';
        title.appendChild(highlight(p.title, data.terms));
        const meta = document.createElement('span');
        meta.className = 'cm-suggest__meta';
        const bit = (icon, text) => {
          const span = document.createElement('span');
          if (icon) { const i = document.createElement('i'); i.className = icon; i.setAttribute('aria-hidden', 'true'); span.append(i, ' '); }
          span.append(String(text));
          return span;
        };
        meta.append(bit('', p.status === 'open' ? p.flair_label : p.status_label),
                    bit('fas fa-arrow-up', p.score), bit('far fa-comment', p.comment_count));
        if (p.visibility === 'private') meta.append(bit('fas fa-lock', ''));
        a.append(icon, title, meta);
        if (p.snippet) {
          const sn = document.createElement('span');
          sn.className = 'cm-suggest__snippet';
          sn.appendChild(highlight(p.snippet, data.terms));
          a.appendChild(sn);
        }
        box.appendChild(a);
        items.push(a);
      });
      if (!data.suggestions.length) {
        const empty = document.createElement('div');
        empty.className = 'cm-suggest__empty';
        if (data.did_you_mean) {
          empty.append('No matches. Did you mean ');
          const b = document.createElement('button');
          b.type = 'button'; b.textContent = data.did_you_mean;
          b.addEventListener('mousedown', ev => { ev.preventDefault(); input.value = data.did_you_mean; onInput(true); });
          empty.append(b, '?');
        } else {
          empty.textContent = 'No posts match yet.';
        }
        box.appendChild(empty);
      }
      const foot = document.createElement('a');
      foot.id = 'cm-sg-foot';
      foot.setAttribute('role', 'option');
      if (data.suggestions.length) {
        foot.className = 'cm-suggest__foot';
        foot.href = `${cfg.endpoints.results}?q=${encodeURIComponent(q)}`;
        foot.innerHTML = '<i class="fas fa-magnifying-glass" aria-hidden="true"></i><span></span><kbd>Enter</kbd>';
        foot.querySelector('span').textContent = `See all results for “${q}”`;
        foot.addEventListener('click', ev => { ev.preventDefault(); commit(); });
      } else {
        foot.className = 'cm-suggest__foot';
        foot.href = cfg.endpoints.new;
        foot.innerHTML = '<i class="fas fa-pen-to-square" aria-hidden="true"></i><span>Nothing here? Start a new post</span>';
      }
      box.appendChild(foot);
      items.push(foot);
      open(true);
    }

    async function fetchSuggestions(q) {
      if (suggestCtrl) suggestCtrl.abort();
      if (q.length < 2) { open(false); return; }
      suggestCtrl = new AbortController();
      form.classList.add('is-loading');
      try {
        const res = await fetch(`${cfg.endpoints.search}?q=${encodeURIComponent(q)}`, { signal: suggestCtrl.signal, credentials: 'same-origin' });
        if (!res.ok) throw new Error(res.status);
        const data = await res.json();
        if (input.value.trim() !== q) return;  // user kept typing
        terms = data.terms || queryTerms(q);
        renderSuggestions(data);
      } catch (e) {
        if (e.name !== 'AbortError') open(false);
      } finally {
        form.classList.remove('is-loading');
      }
    }

    async function fetchResults(q) {
      if (resultsCtrl) resultsCtrl.abort();
      resultsCtrl = new AbortController();
      const params = q ? new URLSearchParams({ q }) : new URLSearchParams(baseParams);
      const pageUrl = `${cfg.endpoints.results}${params.toString() ? '?' + params : ''}`;
      params.set('partial', '1');
      results.setAttribute('aria-busy', 'true');
      try {
        const res = await fetch(`${cfg.endpoints.results}?${params}`, { signal: resultsCtrl.signal, credentials: 'same-origin' });
        if (!res.ok) throw new Error(res.status);
        const html = await res.text();
        if (input.value.trim() !== q) return;
        results.innerHTML = html;  // server-rendered by Jinja (autoescaped)
        if (q) highlightResults(queryTerms(q));
        browse.forEach(el => { if (el) el.hidden = !!q; });
        history.replaceState(null, '', pageUrl);
        lastQuery = q;
      } catch (e) {
        if (e.name !== 'AbortError') toast('Search failed. Check your connection.');
      } finally {
        results.setAttribute('aria-busy', 'false');
      }
    }

    function onInput(immediate) {
      const q = input.value.trim();
      form.classList.toggle('has-value', !!input.value);
      clearTimeout(suggestTimer); clearTimeout(resultsTimer);
      suggestTimer = setTimeout(() => fetchSuggestions(q), immediate ? 0 : 120);
      if (q === lastQuery) return;
      if (q.length === 1) return;  // one letter is noise; keep the current list
      resultsTimer = setTimeout(() => fetchResults(q), immediate ? 0 : 350);
    }

    function commit() {
      clearTimeout(resultsTimer);
      open(false);
      const q = input.value.trim();
      if (q !== lastQuery) fetchResults(q);
    }

    input.addEventListener('compositionstart', () => { composing = true; });
    input.addEventListener('compositionend', () => { composing = false; onInput(false); });
    input.addEventListener('input', () => { if (!composing) onInput(false); });
    input.addEventListener('focus', () => { if (items.length && input.value.trim().length >= 2) open(true); });
    input.addEventListener('keydown', e => {
      if (e.isComposing) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); if (box.hidden && items.length) open(true); setActive(active + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1); }
      else if (e.key === 'Escape') {
        if (!box.hidden) { e.preventDefault(); open(false); }
        else if (input.value) { input.value = ''; onInput(true); }
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (!box.hidden && active >= 0 && items[active].id !== 'cm-sg-foot') { location.href = items[active].href; return; }
        if (!box.hidden && active >= 0 && items[active].getAttribute('href') === cfg.endpoints.new) { location.href = cfg.endpoints.new; return; }
        commit();
      }
    });
    form.addEventListener('submit', e => { e.preventDefault(); commit(); });
    box.addEventListener('mousedown', e => e.preventDefault());  // keep focus in the input while clicking
    document.addEventListener('click', e => { if (!form.contains(e.target)) open(false); });
    results.addEventListener('click', e => {
      const dym = e.target.closest('[data-dym]');
      const clear = e.target.closest('[data-clear-search]');
      if (dym) { e.preventDefault(); input.value = dym.dataset.dym; onInput(true); }
      else if (clear) { e.preventDefault(); input.value = ''; onInput(true); input.focus(); }
    });
    // "/" focuses search from anywhere on the page (GitHub/YouTube style).
    document.addEventListener('keydown', e => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t.closest && t.closest('input, textarea, select, [contenteditable="true"]')) return;
      e.preventDefault();
      input.focus();
      input.select();
    });

    form.classList.toggle('has-value', !!input.value);
    if (lastQuery) highlightResults(queryTerms(lastQuery));
  }

  // ── Voting (list arrows and post-page pills share one handler) ───────────
  function initVoting() {
    document.addEventListener('click', async e => {
      const btn = e.target.closest('[data-vote]');
      if (!btn || btn.disabled) return;
      const box = btn.closest('[data-vote-post]');
      if (!box) return;
      e.preventDefault();
      const buttons = box.querySelectorAll('[data-vote]');
      buttons.forEach(b => { b.disabled = true; });
      const r = await api(cfg.endpoints.vote.replace('__ID__', box.dataset.votePost), 'POST', { value: Number(btn.dataset.vote) });
      buttons.forEach(b => { b.disabled = false; });
      if (!r.ok) { toast(r.data.error || 'Could not vote.'); return; }
      const d = r.data;
      buttons.forEach(b => {
        const on = Number(b.dataset.vote) === d.vote;
        b.classList.toggle('is-on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      const score = box.querySelector('[data-role="score"]');
      if (score) {
        score.textContent = d.score;
        score.title = `${d.up} up · ${d.down} down`;
        score.classList.toggle('is-up', d.vote === 1);
        score.classList.toggle('is-down', d.vote === -1);
      }
      const up = box.querySelector('[data-role="up"]');
      const down = box.querySelector('[data-role="down"]');
      if (up) up.textContent = d.up;
      if (down) down.textContent = d.down;
    });
  }

  // ── Composer ────────────────────────────────────────────────────────────
  function initComposer() {
    const form = document.getElementById('cm-post-form');
    const errBox = document.getElementById('cm-form-error');
    const bugFields = document.getElementById('cm-bug-fields');
    const bodyLabel = document.getElementById('cm-body-label');
    const bodyHint = document.getElementById('cm-body-hint');
    const privateField = document.getElementById('cm-private-field');
    const stepsBox = document.getElementById('cm-steps');
    const limits = cfg.limits;
    const post = cfg.post;
    let images = post ? post.images.filter(i => !i.expired).map(i => ({ id: i.id, thumb: i.thumb_url, fresh: false })) : [];
    let submitting = false;
    const tags = newMentionState();
    const privateBox = document.getElementById('cm-private');
    const tagOpts = { postId: () => (post ? post.id : null), isPrivate: () => !!(privateBox && privateBox.checked) };
    ['cm-body', 'cm-expected', 'cm-actual'].forEach(id => initMentions(document.getElementById(id), tags, tagOpts));

    const flair = () => (form.querySelector('input[name="flair"]:checked') || {}).value || '';

    function syncFlair() {
      const f = flair();
      const isBug = f === 'bug';
      bugFields.hidden = !isBug;
      bodyLabel.textContent = isBug ? 'Anything else? (optional)' : 'Details';
      bodyHint.textContent = isBug ? 'Error messages, match ID, anything that helps.' : `${limits.body[0]}+ characters. Plain text; links are clickable.`;
      privateField.hidden = f === 'announcement';
    }
    form.querySelectorAll('input[name="flair"]').forEach(r => r.addEventListener('change', syncFlair));

    // Steps
    function addStep(value) {
      if (stepsBox.children.length >= limits.steps[1]) return;
      const row = document.createElement('div');
      row.className = 'cm-step';
      const n = document.createElement('span');
      n.className = 'cm-step__n';
      const input = document.createElement('input');
      input.className = 'cm-input';
      input.maxLength = limits.step[1];
      input.value = value || '';
      input.setAttribute('aria-label', 'Step');
      initMentions(input, tags, tagOpts);  // before the Enter-adds-a-step handler, so picking wins
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'cm-btn cm-btn--ghost cm-btn--sm';
      rm.setAttribute('aria-label', 'Remove step');
      rm.innerHTML = '<i class="fas fa-xmark" aria-hidden="true"></i>';
      rm.addEventListener('click', () => { if (stepsBox.children.length > limits.steps[0]) { row.remove(); renumber(); } else input.value = ''; });
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); addStep(''); renumber(); stepsBox.lastElementChild.querySelector('input').focus(); }
      });
      row.append(n, input, rm);
      stepsBox.appendChild(row);
    }
    function renumber() {
      [...stepsBox.children].forEach((row, i) => {
        row.querySelector('.cm-step__n').textContent = i + 1;
        row.querySelector('input').placeholder = i === 0 ? 'e.g. Start a T20 match with rain enabled' : i === 1 ? 'e.g. Simulate to the 12th over' : 'Next step';
      });
    }
    const initialSteps = post && post.steps && post.steps.length ? post.steps : ['', '', ''];
    initialSteps.forEach(addStep);
    renumber();
    document.getElementById('cm-add-step').addEventListener('click', () => { addStep(''); renumber(); stepsBox.lastElementChild.querySelector('input').focus(); });

    // Counters
    document.querySelectorAll('[data-counter-for]').forEach(c => {
      const el = document.getElementById(c.dataset.counterFor);
      const upd = () => { c.textContent = `${el.value.length}/${el.maxLength}`; };
      el.addEventListener('input', upd);
      upd();
    });

    // Similar posts (new posts only). Newer keystrokes abort older requests.
    if (cfg.mode === 'new') {
      const title = document.getElementById('cm-title');
      const box = document.getElementById('cm-similar');
      const list = document.getElementById('cm-similar-list');
      let timer = null, ctrl = null;
      title.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          const q = title.value.trim();
          if (ctrl) ctrl.abort();
          if (q.length < 6) { box.hidden = true; return; }
          ctrl = new AbortController();
          try {
            const res = await fetch(cfg.endpoints.similar + '?q=' + encodeURIComponent(q), { signal: ctrl.signal, credentials: 'same-origin' });
            const data = await res.json();
            list.replaceChildren();
            (data.posts || []).forEach(p => {
              const a = document.createElement('a');
              a.href = p.url; a.target = '_blank'; a.rel = 'noopener';
              const b = document.createElement('span');
              b.className = `cm-flair cm-flair--${p.flair}`; b.textContent = p.flair_label;
              const t = document.createElement('span'); t.textContent = p.title;
              const v = document.createElement('span');
              v.style.cssText = 'margin-left:auto;color:var(--fg-secondary);font-size:.76rem;white-space:nowrap';
              v.textContent = `${p.score > 0 ? '+' : ''}${p.score} · ${p.status_label}`;
              a.append(b, t, v);
              list.appendChild(a);
            });
            box.hidden = !list.children.length;
          } catch (e) { if (e.name !== 'AbortError') box.hidden = true; }
        }, 350);
      });
    }

    // Images
    const uploads = document.getElementById('cm-uploads');
    const addBtn = document.getElementById('cm-upload-add');
    const fileInput = document.getElementById('cm-file');
    function renderImages() {
      uploads.querySelectorAll('.cm-upload-tile').forEach(t => t.remove());
      images.forEach(img => {
        const tile = document.createElement('div');
        tile.className = 'cm-upload-tile';
        const el = document.createElement('img');
        el.src = img.thumb; el.alt = 'Attached image';
        const rm = document.createElement('button');
        rm.type = 'button'; rm.setAttribute('aria-label', 'Remove image');
        rm.innerHTML = '<i class="fas fa-xmark" aria-hidden="true"></i>';
        rm.addEventListener('click', async () => {
          images = images.filter(i => i !== img);
          renderImages();
          if (img.fresh) api(cfg.endpoints.image.replace('__ID__', img.id), 'DELETE');
        });
        tile.append(el, rm);
        if (img.kb) {
          const s = document.createElement('span'); s.className = 'cm-upload-size'; s.textContent = `${img.kb} KB`; tile.appendChild(s);
        }
        uploads.insertBefore(tile, addBtn);
      });
      addBtn.hidden = images.length >= limits.images;
    }
    renderImages();
    addBtn.addEventListener('click', () => fileInput.click());

    // Large photos are shrunk in the browser first so phones don't upload
    // 10 MB over mobile data. The server still re-validates and re-encodes.
    async function preShrink(file) {
      if (file.size < 2.5 * 1024 * 1024 || !window.createImageBitmap || !/jpe?g|webp/i.test(file.type)) return file;
      try {
        const bmp = await createImageBitmap(file);
        const scale = Math.min(1, 2400 / Math.max(bmp.width, bmp.height));
        const c = document.createElement('canvas');
        c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
        c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
        const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.9));
        return blob && blob.size < file.size ? new File([blob], 'upload.jpg', { type: 'image/jpeg' }) : file;
      } catch (e) { return file; }
    }

    fileInput.addEventListener('change', async () => {
      const file = fileInput.files[0];
      fileInput.value = '';
      if (!file) return;
      const field = form.querySelector('[data-field="images"]');
      setFieldError(field, '');
      if (file.size > limits.image_bytes) { setFieldError(field, 'Images must be 10 MB or smaller.'); return; }
      addBtn.disabled = true;
      addBtn.querySelector('span').textContent = 'Uploading…';
      const fd = new FormData();
      fd.append('image', await preShrink(file));
      const r = await api(cfg.endpoints.upload, 'POST', fd, true);
      addBtn.disabled = false;
      addBtn.querySelector('span').innerHTML = '<i class="fas fa-image" aria-hidden="true"></i><br>Add image';
      if (!r.ok) { setFieldError(field, r.data.error || 'Upload failed.'); return; }
      images.push({ id: r.data.image.id, thumb: r.data.image.thumb_url, fresh: true, kb: Math.round(r.data.bytes / 1024) });
      renderImages();
    });

    function setFieldError(field, msg) {
      if (!field) return;
      field.classList.toggle('has-error', !!msg);
      const p = field.querySelector('.cm-inline-error');
      if (p) { p.textContent = msg; p.hidden = !msg; }
    }

    function showErrors(data) {
      form.querySelectorAll('[data-field]').forEach(f => setFieldError(f, ''));
      const fields = data.fields || {};
      Object.keys(fields).forEach(k => setFieldError(form.querySelector(`[data-field="${k}"]`), fields[k]));
      errBox.querySelector('div').textContent = data.error || 'Something went wrong.';
      errBox.hidden = false;
      (form.querySelector('.has-error') || errBox).scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    form.addEventListener('submit', async e => {
      e.preventDefault();
      if (submitting) return;
      const f = flair();
      const payload = {
        flair: f,
        title: document.getElementById('cm-title').value,
        body: document.getElementById('cm-body').value,
        visibility: document.getElementById('cm-private').checked ? 'private' : 'public',
        image_ids: images.map(i => i.id),
      };
      if (f === 'bug') {
        payload.steps = [...stepsBox.querySelectorAll('input')].map(i => i.value.trim()).filter(Boolean);
        payload.expected = document.getElementById('cm-expected').value;
        payload.actual = document.getElementById('cm-actual').value;
        payload.match_format = document.getElementById('cm-format').value;
      }
      payload.mentions = collectMentions(tags, [payload.body, payload.expected, payload.actual, ...(payload.steps || [])]);
      if (cfg.mode === 'new') {
        payload.page_url = cfg.prefill.page_url || document.referrer || '';
        payload.app_version = cfg.app_version || '';
      }
      submitting = true;
      const btn = document.getElementById('cm-submit');
      btn.disabled = true;
      const r = cfg.mode === 'new'
        ? await api(cfg.endpoints.create, 'POST', payload)
        : await api(cfg.endpoints.update, 'PATCH', payload);
      submitting = false;
      btn.disabled = false;
      if (!r.ok) { showErrors(r.data); return; }
      images.forEach(i => { i.fresh = false; });
      location.href = r.data.url;
    });

    syncFlair();
  }

  // ── Post page ───────────────────────────────────────────────────────────
  function initPost() {
    const ep = cfg.endpoints;
    const commentUrl = id => ep.comment.replace(/\/0$/, '/' + id);
    const modCommentUrl = id => ep.moderate_comment.replace('/0/', '/' + id + '/');

    // Comment composer — moves under the comment being replied to.
    const form = document.getElementById('cm-comment-form');
    const home = form ? form.parentElement : null;
    const homeNext = form ? form.nextElementSibling : null;
    const replyTo = document.getElementById('cm-reply-to');
    const errEl = document.getElementById('cm-comment-error');
    function resetComposer() {
      form.dataset.parent = '';
      replyTo.hidden = true;
      home.insertBefore(form, homeNext);
    }
    const tagOpts = { postId: () => cfg.post_id };
    const commentTags = newMentionState();
    if (form) {
      initMentions(document.getElementById('cm-comment-body'), commentTags, tagOpts);
      document.getElementById('cm-reply-cancel').addEventListener('click', resetComposer);
      form.addEventListener('submit', async e => {
        e.preventDefault();
        const ta = document.getElementById('cm-comment-body');
        const body = ta.value.trim();
        if (!body) return;
        const btn = form.querySelector('button[type="submit"]');
        btn.disabled = true;
        errEl.hidden = true;
        const r = await api(ep.comments, 'POST', { body, parent_id: form.dataset.parent || null,
                                                   mentions: collectMentions(commentTags, [body]) });
        btn.disabled = false;
        if (!r.ok) { errEl.textContent = r.data.error || 'Could not post comment.'; errEl.hidden = false; return; }
        ta.value = '';
        reloadAt('c' + r.data.comment.id);
      });
    }

    document.addEventListener('click', async e => {
      const btn = e.target.closest('[data-action],[data-mod-action]');
      if (!btn) return;
      const commentEl = btn.closest('[data-comment-id]');
      const cid = commentEl ? commentEl.dataset.commentId : null;
      const action = btn.dataset.action;

      if (action === 'reply' && form) {
        form.dataset.parent = btn.dataset.parent;
        replyTo.querySelector('strong').textContent = btn.dataset.name;
        replyTo.hidden = false;
        const item = btn.closest('.cm-thread-item');
        item.appendChild(form);
        const ta = document.getElementById('cm-comment-body');
        ta.focus();
        return;
      }

      if (action === 'edit-comment') {
        const bodyEl = commentEl.querySelector('[data-role="body"]');
        if (bodyEl.querySelector('textarea')) return;
        const original = bodyEl.textContent;
        const ta = document.createElement('textarea');
        ta.className = 'cm-textarea'; ta.value = original; ta.maxLength = 2000; ta.style.minHeight = '80px';
        const save = document.createElement('button');
        save.type = 'button'; save.className = 'cm-btn cm-btn--primary cm-btn--sm'; save.textContent = 'Save';
        const cancel = document.createElement('button');
        cancel.type = 'button'; cancel.className = 'cm-btn cm-btn--sm'; cancel.textContent = 'Cancel';
        const err = document.createElement('p'); err.className = 'cm-inline-error'; err.hidden = true;
        const bar = document.createElement('div'); bar.style.cssText = 'display:flex;gap:.4rem;margin-top:.4rem';
        bar.append(save, cancel);
        bodyEl.replaceChildren(ta, bar, err);
        const editTags = newMentionState();
        initMentions(ta, editTags, tagOpts);
        ta.focus();
        cancel.addEventListener('click', () => { bodyEl.textContent = original; });
        save.addEventListener('click', async () => {
          save.disabled = true;
          const r = await api(commentUrl(cid), 'PATCH', { body: ta.value, mentions: collectMentions(editTags, [ta.value]) });
          save.disabled = false;
          if (!r.ok) { err.textContent = r.data.error || 'Could not save.'; err.hidden = false; return; }
          reloadAt('c' + cid);
        });
        return;
      }

      if (action === 'delete-comment') {
        let reason = '';
        if (cfg.is_admin) { reason = prompt('Delete this comment. Reason (shown to admins):', ''); if (reason === null) return; }
        else if (!confirm('Delete your comment?')) return;
        const r = await api(commentUrl(cid), 'DELETE', { reason });
        if (!r.ok) { toast(r.data.error || 'Could not delete.'); return; }
        reloadAt();
        return;
      }

      if (action === 'delete-post') {
        let reason = '';
        if (cfg.is_admin) { reason = prompt('Delete this post. Reason (shown to admins):', ''); if (reason === null) return; }
        else if (!confirm('Delete your post? This cannot be undone.')) return;
        const r = await api(ep.post, 'DELETE', { reason });
        if (!r.ok) { toast(r.data.error || 'Could not delete.'); return; }
        location.href = r.data.url;
        return;
      }

      if (action === 'report') {
        const dlg = document.getElementById('cm-report-dialog');
        const rform = document.getElementById('cm-report-form');
        rform.reset();
        dlg.dataset.comment = btn.dataset.comment || '';
        dlg.showModal();
        dlg.addEventListener('close', async function onClose() {
          dlg.removeEventListener('close', onClose);
          if (dlg.returnValue !== 'ok') return;
          const r = await api(ep.report, 'POST', {
            post_id: cfg.post_id, comment_id: dlg.dataset.comment || null,
            reason: rform.reason.value, details: rform.details.value,
          });
          toast(r.ok ? 'Thanks. An admin will review it.' : (r.data.error || 'Could not send report.'));
        });
        return;
      }

      if (action === 'mute') {
        const hours = prompt(`Mute ${btn.dataset.name} on the board for how many hours? (0 = unmute, 168 = 1 week)`, '24');
        if (hours === null) return;
        const r = await api(ep.mute, 'POST', { email: btn.dataset.email, hours: parseInt(hours, 10) || 0 });
        toast(r.ok ? (r.data.muted_until ? 'User muted.' : 'User unmuted.') : (r.data.error || 'Failed.'));
        if (r.ok) setTimeout(reloadAt, 800);
        return;
      }

      if (action === 'mod-comment') {
        if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
        const r = await api(modCommentUrl(cid), 'POST', { action: btn.dataset.op });
        if (!r.ok) { toast(r.data.error || 'Failed.'); return; }
        reloadAt(r.data.purged ? '' : 'c' + cid);
        return;
      }

      if (action === 'duplicate') {
        const raw = prompt('Merge into which post? Paste its link or ID.');
        if (!raw) return;
        const m = raw.match(/\/community\/p\/([\w-]+)/);
        await moderate('duplicate', m ? m[1] : raw.trim());
        return;
      }

      if (btn.dataset.modAction) {
        if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
        await moderate(btn.dataset.modAction);
      }
    });

    document.querySelectorAll('[data-mod]').forEach(sel => sel.addEventListener('change', () => moderate(sel.dataset.mod, sel.value)));

    async function moderate(action, value) {
      const r = await api(ep.moderate, 'POST', { action, value });
      if (!r.ok) { toast(r.data.error || 'Failed.'); return; }
      if (r.data.purged) { location.href = '/admin/community'; return; }
      reloadAt();
    }
  }
})();
