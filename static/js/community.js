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
              v.textContent = `${p.vote_count} · ${p.status_label}`;
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

    const vote = document.getElementById('cm-vote');
    if (vote) vote.addEventListener('click', async () => {
      vote.disabled = true;
      const r = await api(ep.vote, 'POST', {});
      vote.disabled = false;
      if (!r.ok) { toast(r.data.error || 'Could not vote.'); return; }
      vote.classList.toggle('is-on', r.data.voted);
      vote.setAttribute('aria-pressed', r.data.voted ? 'true' : 'false');
      vote.querySelector('i').className = 'fas ' + (r.data.voted ? 'fa-check' : 'fa-arrow-up');
      document.getElementById('cm-vote-count').textContent = r.data.vote_count;
    });

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
    if (form) {
      document.getElementById('cm-reply-cancel').addEventListener('click', resetComposer);
      form.addEventListener('submit', async e => {
        e.preventDefault();
        const ta = document.getElementById('cm-comment-body');
        const body = ta.value.trim();
        if (!body) return;
        const btn = form.querySelector('button[type="submit"]');
        btn.disabled = true;
        errEl.hidden = true;
        const r = await api(ep.comments, 'POST', { body, parent_id: form.dataset.parent || null });
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
        ta.focus();
        cancel.addEventListener('click', () => { bodyEl.textContent = original; });
        save.addEventListener('click', async () => {
          save.disabled = true;
          const r = await api(commentUrl(cid), 'PATCH', { body: ta.value });
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
