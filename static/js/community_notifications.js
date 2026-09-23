/* Global community alerts. User content is only assigned through textContent. */
(function () {
  'use strict';
  const panel = document.getElementById('cmAlerts');
  const bell = document.getElementById('cmNavBell');
  if (!panel || !bell) return;
  const dot = document.getElementById('cmNavBellDot');
  const items = document.getElementById('cmAlertItems');
  const status = document.getElementById('cmAlertStatus');
  const historyStatus = document.getElementById('cmHistoryStatus');
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  let pending = null;
  let writing = false;
  function message(text) {
    status.textContent = text;
    if (historyStatus) historyStatus.textContent = text;
  }
  function badge(count) {
    dot.hidden = count === 0;
    dot.textContent = count > 99 ? '99+' : String(count);
    bell.setAttribute('aria-label', `Notifications, ${count} unread`);
    document.querySelectorAll('[data-unread-count]').forEach(el => { el.textContent = count; });
  }
  const kinds = { mention: ['fa-at', 'Mention'], reply: ['fa-reply', 'Reply'], comment: ['fa-comment', 'Thread reply'], admin_response: ['fa-shield-alt', 'Admin response'], status_change: ['fa-circle-check', 'Status update'] };
  function relativeTime(value) {
    const minutes = Math.max(0, Math.floor((Date.now() - new Date(value)) / 60000));
    if (minutes < 1) return 'Just now';
    if (minutes < 60) return `${minutes}m ago`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
    if (minutes < 10080) return `${Math.floor(minutes / 1440)}d ago`;
    return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
  function render(data) {
    const focusedId = panel.contains(document.activeElement) ? document.activeElement.dataset.notificationId : null;
    badge(data.unread);
    items.replaceChildren();
    data.items.forEach(note => {
      const link = document.createElement('a');
      link.className = 'cm-alerts__item' + (note.read ? '' : ' cm-unread');
      link.href = note.url;
      link.dataset.notificationId = note.id;
      const kind = kinds[note.kind] ? note.kind : 'update';
      const [iconName, label] = kinds[kind] || ['fa-bell', 'Update'];
      const icon = document.createElement('span');
      icon.className = 'cm-alerts__icon cm-alerts__icon--' + kind;
      icon.setAttribute('aria-hidden', 'true');
      const glyph = document.createElement('i');
      glyph.className = 'fas ' + iconName;
      icon.append(glyph);
      const body = document.createElement('span');
      body.className = 'cm-alerts__body';
      const meta = document.createElement('span');
      meta.className = 'cm-alerts__meta';
      const type = document.createElement('span');
      type.textContent = label + ' · ';
      const text = document.createElement('span');
      text.className = 'cm-alerts__text';
      text.textContent = note.text;
      const time = document.createElement('time');
      time.dateTime = note.created_at;
      time.textContent = relativeTime(note.created_at);
      time.title = new Date(note.created_at).toLocaleString();
      meta.append(type, time);
      body.append(meta, text);
      const unreadDot = document.createElement('span');
      unreadDot.className = 'cm-alerts__unread-dot';
      unreadDot.setAttribute('aria-label', 'Unread');
      unreadDot.hidden = note.read;
      link.append(icon, body, unreadDot);
      items.append(link);
    });
    status.textContent = '';
    if (!data.items.length) {
      const empty = document.createElement('div');
      empty.className = 'cm-alerts__empty';
      const title = document.createElement('strong');
      title.textContent = "You're all caught up";
      const hint = document.createElement('p');
      hint.textContent = 'New replies and mentions will appear here.';
      empty.append(title, hint);
      items.append(empty);
    }
    if (focusedId) items.querySelector(`[data-notification-id="${focusedId}"]`)?.focus();
  }
  function refresh() {
    if (document.hidden || writing) return Promise.resolve();
    if (pending) return pending;
    pending = (async () => {
      try {
        const res = await fetch(panel.dataset.listUrl, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
        if (!res.ok) throw new Error();
        render(await res.json());
      } catch (_) {
        status.textContent = 'Could not refresh notifications. Reopen to retry.';
      } finally { pending = null; }
    })();
    return pending;
  }
  function close() {
    panel.hidden = true;
    bell.setAttribute('aria-expanded', 'false');
  }
  bell.addEventListener('click', event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    panel.hidden = !panel.hidden;
    bell.setAttribute('aria-expanded', String(!panel.hidden));
    if (!panel.hidden) { refresh(); panel.querySelector('button').focus(); }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !panel.hidden) { close(); bell.focus(); }
  });
  document.addEventListener('click', event => {
    if (!panel.contains(event.target) && !bell.contains(event.target)) close();
  });
  document.addEventListener('focusin', event => {
    if (!panel.hidden && !panel.contains(event.target) && !bell.contains(event.target)) close();
  });
  document.addEventListener('click', async event => {
    const link = event.target.closest('[data-notification-id]');
    const all = event.target.closest('[data-notifications-read-all]');
    if (!link && !all) return;
    // Keep native new-tab navigation; the request below still records the click.
    const modified = link && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey);
    if (!modified) event.preventDefault();
    if (writing) return;
    writing = true;
    document.querySelectorAll('[data-notifications-read-all]').forEach(b => { b.disabled = true; });
    try {
      if (pending) await pending;
      const res = await fetch(panel.dataset.readUrl, {
        method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf, Accept: 'application/json' },
        body: JSON.stringify(all ? { all: true } : { ids: [Number(link.dataset.notificationId)] })
      });
      if (!res.ok) throw new Error();
      const data = await res.json();
      badge(data.unread);
      const selector = all ? '[data-notification-id]' : `[data-notification-id="${link.dataset.notificationId}"]`;
      document.querySelectorAll(selector).forEach(el => {
        el.classList.remove('cm-unread');
        const indicator = el.querySelector('.cm-alerts__unread-dot');
        if (indicator) indicator.hidden = true;
      });
      message(all ? 'All notifications marked read.' : 'Notification marked read.');
      if (link && !modified) window.location.assign(link.href);
    } catch (_) {
      message('Could not mark notifications read. Please try again.');
    } finally {
      writing = false;
      document.querySelectorAll('[data-notifications-read-all]').forEach(b => { b.disabled = false; });
    }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.setInterval(refresh, 30000);
  refresh();
})();
