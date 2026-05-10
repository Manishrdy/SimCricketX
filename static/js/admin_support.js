(function () {
    'use strict';

    var listEl = document.getElementById('support-conversation-list');
    var searchEl = document.getElementById('support-search');
    var refreshBtn = document.getElementById('support-refresh');
    var notifyToggleBtn = document.getElementById('support-notify-toggle');
    var inboxCountEl = document.getElementById('support-inbox-count');
    var titleEl = document.getElementById('support-thread-title');
    var subtitleEl = document.getElementById('support-thread-subtitle');
    var threadAvatarEl = document.getElementById('support-thread-avatar');
    var threadStatusEl = document.getElementById('support-thread-status');
    var messagesEl = document.getElementById('support-thread-messages');
    var contextEl = document.getElementById('support-context-body');
    var composer = document.getElementById('support-admin-composer');
    var input = document.getElementById('support-admin-input');
    var sendBtn = document.getElementById('support-admin-send');
    var closeBtn = document.getElementById('support-close');
    var reopenBtn = document.getElementById('support-reopen');
    var deleteBtn = document.getElementById('support-delete');

    var socket = null;
    var selectedId = null;
    var conversations = {};
    var statusFilter = 'open';
    var searchTimer = null;
    var seenMessageIds = {};
    var renderedMessages = [];

    var GROUP_WINDOW_MS = 3 * 60 * 1000;

    var NOTIFY_LS_KEY = 'scx-support-notify-enabled';
    var notifyEnabled = false;
    var activeNotifications = {};

    function requestJson(url, options) {
        return fetch(url, options || {}).then(function (resp) {
            return resp.json().then(function (body) {
                if (!resp.ok) throw new Error(body.error || 'request_failed');
                return body;
            });
        });
    }

    function fmt(iso) {
        if (!iso) return '';
        try {
            return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        } catch (e) {
            return iso;
        }
    }

    function fmtTime(iso) {
        if (!iso) return '';
        try {
            return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        } catch (e) {
            return '';
        }
    }

    function fmtRelative(iso) {
        if (!iso) return '';
        try {
            var then = new Date(iso).getTime();
            if (isNaN(then)) return '';
            var diff = Date.now() - then;
            if (diff < 60 * 1000) return 'now';
            if (diff < 60 * 60 * 1000) return Math.floor(diff / 60000) + 'm';
            if (diff < 24 * 60 * 60 * 1000) return Math.floor(diff / 3600000) + 'h';
            if (diff < 7 * 24 * 60 * 60 * 1000) return Math.floor(diff / 86400000) + 'd';
            return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
        } catch (e) {
            return '';
        }
    }

    function dayKey(iso) {
        if (!iso) return '';
        var d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
    }

    function dayLabel(iso) {
        if (!iso) return '';
        var d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        var today = new Date();
        var yesterday = new Date();
        yesterday.setDate(today.getDate() - 1);
        if (dayKey(d.toISOString()) === dayKey(today.toISOString())) return 'Today';
        if (dayKey(d.toISOString()) === dayKey(yesterday.toISOString())) return 'Yesterday';
        return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
    }

    function initials(label) {
        var raw = String(label || '').trim();
        if (!raw) return '?';
        if (raw.indexOf('@') !== -1) raw = raw.split('@')[0];
        var parts = raw.split(/[\s._-]+/).filter(Boolean);
        if (!parts.length) return raw.slice(0, 2).toUpperCase();
        if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
        return (parts[0][0] + parts[1][0]).toUpperCase();
    }

    function statusInfo(conv) {
        if (!conv) return { key: 'open', label: 'Open' };
        if (conv.status === 'closed') return { key: 'closed', label: 'Closed' };
        if (conv.status === 'pending_admin') return { key: 'pending', label: 'Awaiting reply' };
        return { key: 'open', label: 'Open' };
    }

    function loadConversations() {
        var params = new URLSearchParams();
        if (statusFilter) params.set('status', statusFilter);
        if (searchEl.value.trim()) params.set('q', searchEl.value.trim());
        listEl.innerHTML = '<div class="support-empty"><i class="fas fa-inbox"></i><span>Loading conversations…</span></div>';
        return requestJson('/api/admin/support/conversations?' + params.toString())
            .then(function (body) {
                conversations = {};
                (body.conversations || []).forEach(function (conv) {
                    conversations[conv.id] = conv;
                });
                renderList(currentConversationRows());
            })
            .catch(function () {
                listEl.innerHTML = '<div class="support-empty"><i class="fas fa-triangle-exclamation"></i><span>Could not load conversations.</span></div>';
            });
    }

    function upsertConversation(conv) {
        if (!conv) return;
        conversations[conv.id] = conv;
        renderList(currentConversationRows());
        if (selectedId === conv.id) updateHeader(conv);
    }

    function currentConversationRows() {
        return Object.keys(conversations)
            .map(function (id) { return conversations[id]; })
            .filter(function (conv) {
                if (statusFilter === 'closed') return conv.status === 'closed';
                return conv.status !== 'closed';
            })
            .sort(function (a, b) {
                return String(b.last_message_at || b.created_at || '').localeCompare(String(a.last_message_at || a.created_at || ''));
            });
    }

    function renderList(rows) {
        if (inboxCountEl) {
            if (rows.length) {
                inboxCountEl.textContent = rows.length;
                inboxCountEl.hidden = false;
            } else {
                inboxCountEl.hidden = true;
            }
        }
        if (!rows.length) {
            listEl.innerHTML = '<div class="support-empty"><i class="fas fa-inbox"></i><span>No conversations found.</span></div>';
            return;
        }
        listEl.innerHTML = '';
        rows.forEach(function (conv) {
            var info = statusInfo(conv);
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'support-conversation' +
                (conv.id === selectedId ? ' active' : '') +
                (conv.unread_count ? ' is-unread' : '');
            btn.dataset.id = conv.id;

            var avatar = document.createElement('div');
            avatar.className = 'support-conversation__avatar';
            avatar.textContent = initials(conv.user_label || conv.user_id);

            var main = document.createElement('div');
            main.className = 'support-conversation__main';

            var top = document.createElement('div');
            top.className = 'support-conversation__top';
            var nameEl = document.createElement('span');
            nameEl.className = 'support-conversation__user';
            nameEl.textContent = conv.user_label || conv.user_id || '—';
            var timeEl = document.createElement('span');
            timeEl.className = 'support-conversation__time';
            timeEl.textContent = fmtRelative(conv.last_message_at || conv.created_at);
            top.appendChild(nameEl);
            top.appendChild(timeEl);

            var row = document.createElement('div');
            row.className = 'support-conversation__row';
            var preview = document.createElement('span');
            preview.className = 'support-conversation__preview';
            preview.textContent = conv.last_message ? conv.last_message.body : 'No messages yet';

            var trail = document.createElement('span');
            trail.style.display = 'inline-flex';
            trail.style.alignItems = 'center';
            trail.style.gap = '6px';
            trail.style.flex = '0 0 auto';

            if (conv.unread_count) {
                var unread = document.createElement('span');
                unread.className = 'support-unread';
                unread.textContent = conv.unread_count;
                trail.appendChild(unread);
            } else {
                var dot = document.createElement('span');
                dot.className = 'support-conv-dot is-' + info.key;
                dot.title = info.label;
                trail.appendChild(dot);
            }

            row.appendChild(preview);
            row.appendChild(trail);

            main.appendChild(top);
            main.appendChild(row);

            btn.appendChild(avatar);
            btn.appendChild(main);
            btn.addEventListener('click', function () { selectConversation(conv.id); });
            listEl.appendChild(btn);
        });
    }

    function updateHeader(conv) {
        var info = statusInfo(conv);
        var label = conv.user_label || conv.user_id || 'Conversation';
        titleEl.textContent = label;

        threadAvatarEl.classList.remove('is-empty');
        threadAvatarEl.textContent = initials(label);

        threadStatusEl.hidden = false;
        threadStatusEl.className = 'support-status-pill is-' + info.key;
        threadStatusEl.textContent = info.label;

        var sub = info.label;
        if (conv.source_page_url) sub += ' · ' + conv.source_page_url;
        else if (conv.last_message_at) sub += ' · last message ' + fmt(conv.last_message_at);
        subtitleEl.textContent = sub;

        closeBtn.disabled = conv.status === 'closed';
        reopenBtn.disabled = conv.status !== 'closed';
        reopenBtn.hidden = conv.status !== 'closed';
        closeBtn.hidden = conv.status === 'closed';
        deleteBtn.disabled = false;
        deleteBtn.hidden = false;
        input.disabled = conv.status === 'closed';
        sendBtn.disabled = conv.status === 'closed';
    }

    function resetThread() {
        selectedId = null;
        renderedMessages = [];
        seenMessageIds = {};
        titleEl.textContent = 'Select a conversation';
        subtitleEl.textContent = 'Live user messages appear in the inbox.';
        threadAvatarEl.classList.add('is-empty');
        threadAvatarEl.innerHTML = '<i class="fas fa-comment-dots"></i>';
        threadStatusEl.hidden = true;
        threadStatusEl.textContent = '';
        threadStatusEl.className = 'support-status-pill';
        messagesEl.innerHTML = '<div class="support-empty">' +
            '<i class="fas fa-comments"></i>' +
            '<strong>No conversation selected</strong>' +
            '<span>Choose a user from the inbox to read and respond to their messages.</span>' +
            '</div>';
        contextEl.innerHTML = '<div class="support-empty"><i class="fas fa-user"></i><span>No conversation selected.</span></div>';
        input.value = '';
        autosize();
        input.disabled = true;
        sendBtn.disabled = true;
        closeBtn.disabled = true;
        reopenBtn.disabled = true;
        reopenBtn.hidden = true;
        closeBtn.hidden = true;
        deleteBtn.disabled = true;
        deleteBtn.hidden = true;
    }

    function selectConversation(id) {
        selectedId = id;
        if (socket && socket.connected) socket.emit('support:conversation:join', { conversation_id: id });
        requestJson('/api/admin/support/conversations/' + encodeURIComponent(id))
            .then(function (body) {
                conversations[id] = body.conversation;
                updateHeader(body.conversation);
                renderMessages(body.messages || []);
                renderContext(body.context || {}, body.conversation);
                markRead(id);
                renderList(currentConversationRows());
                input.focus();
            })
            .catch(function () {
                messagesEl.innerHTML = '<div class="support-empty"><i class="fas fa-triangle-exclamation"></i><span>Could not load conversation.</span></div>';
            });
    }

    function renderMessages(messages) {
        messagesEl.innerHTML = '';
        seenMessageIds = {};
        renderedMessages = [];
        if (!messages.length) {
            messagesEl.innerHTML = '<div class="support-empty">' +
                '<i class="fas fa-feather-pointed"></i>' +
                '<strong>No messages yet</strong>' +
                '<span>Be the first to reach out — your reply starts the thread.</span>' +
                '</div>';
            return;
        }
        messages.forEach(renderMessage);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function renderMessage(msg) {
        if (!msg || seenMessageIds[msg.id]) return;
        seenMessageIds[msg.id] = true;
        var empty = messagesEl.querySelector('.support-empty');
        if (empty) empty.remove();

        var prev = renderedMessages[renderedMessages.length - 1];
        var prevDay = prev ? dayKey(prev.created_at) : '';
        var thisDay = dayKey(msg.created_at);
        if (thisDay && thisDay !== prevDay) {
            var divider = document.createElement('div');
            divider.className = 'support-day-divider';
            divider.textContent = dayLabel(msg.created_at);
            messagesEl.appendChild(divider);
        }

        var grouped = false;
        if (prev && prev.sender_type === msg.sender_type && thisDay === prevDay) {
            try {
                var dt = new Date(msg.created_at).getTime() - new Date(prev.created_at).getTime();
                if (!isNaN(dt) && dt >= 0 && dt < GROUP_WINDOW_MS) grouped = true;
            } catch (e) {}
        }

        var row = document.createElement('div');
        var classes = ['support-msg', msg.sender_type === 'admin' ? 'is-admin' : 'is-user'];
        classes.push(grouped ? 'is-grouped' : 'is-leading');
        row.className = classes.join(' ');

        var bubble = document.createElement('div');
        bubble.className = 'support-msg__bubble';
        bubble.textContent = msg.body || '';
        var time = fmtTime(msg.created_at);
        if (time) bubble.setAttribute('data-time', time);

        row.appendChild(bubble);
        messagesEl.appendChild(row);
        renderedMessages.push(msg);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function renderContext(ctx, conv) {
        var user = ctx.user || {};
        var exceptions = ctx.recent_exceptions || [];
        contextEl.innerHTML = '';

        var account = document.createElement('section');
        account.className = 'support-context-card';
        account.innerHTML =
            '<h4><i class="fas fa-user"></i> Account</h4>' +
            '<div class="support-kv">' +
                '<div><span>User ID</span><strong></strong></div>' +
                '<div><span>Name</span><strong></strong></div>' +
                '<div><span>Created</span><strong></strong></div>' +
                '<div><span>Last login</span><strong></strong></div>' +
            '</div>';
        var aStrongs = account.querySelectorAll('strong');
        setStrong(aStrongs[0], user.id || conv.user_id || '');
        setStrong(aStrongs[1], user.display_name || '');
        setStrong(aStrongs[2], fmt(user.created_at));
        setStrong(aStrongs[3], fmt(user.last_login));
        contextEl.appendChild(account);

        var convCard = document.createElement('section');
        convCard.className = 'support-context-card';
        convCard.innerHTML =
            '<h4><i class="fas fa-comments"></i> Conversation</h4>' +
            '<div class="support-kv">' +
                '<div><span>ID</span><strong></strong></div>' +
                '<div><span>App</span><strong></strong></div>' +
                '<div><span>Status</span><strong></strong></div>' +
                '<div><span>Started</span><strong></strong></div>' +
            '</div>';
        var cStrongs = convCard.querySelectorAll('strong');
        setStrong(cStrongs[0], conv.id || '');
        setStrong(cStrongs[1], conv.app_version || '');
        setStrong(cStrongs[2], statusInfo(conv).label);
        setStrong(cStrongs[3], fmt(conv.created_at));
        contextEl.appendChild(convCard);

        var exCard = document.createElement('section');
        exCard.className = 'support-context-card';
        exCard.innerHTML = '<h4><i class="fas fa-bug"></i> Recent Exceptions</h4>';
        if (!exceptions.length) {
            var noEx = document.createElement('div');
            noEx.className = 'support-empty';
            noEx.style.padding = '4px 0 0';
            noEx.style.textAlign = 'left';
            noEx.style.alignItems = 'flex-start';
            noEx.style.flexDirection = 'row';
            noEx.style.gap = '6px';
            noEx.innerHTML = '<span style="font-size:0.78rem;color:var(--fg-secondary,#6b7280);">None found.</span>';
            exCard.appendChild(noEx);
        } else {
            var exList = document.createElement('div');
            exList.className = 'support-context-list';
            exceptions.forEach(function (row) {
                var item = document.createElement('div');
                var label = document.createElement('span');
                label.textContent = row.type || 'Exception';
                var t = document.createElement('time');
                t.textContent = fmt(row.timestamp);
                item.appendChild(label);
                item.appendChild(t);
                exList.appendChild(item);
            });
            exCard.appendChild(exList);
        }
        contextEl.appendChild(exCard);
    }

    function setStrong(el, value) {
        if (!el) return;
        if (value === '' || value === null || typeof value === 'undefined') {
            el.textContent = '—';
            el.classList.add('is-empty');
        } else {
            el.textContent = value;
            el.classList.remove('is-empty');
        }
    }

    function markRead(id) {
        requestJson('/api/admin/support/conversations/' + encodeURIComponent(id) + '/read', { method: 'POST' })
            .then(function (body) {
                if (body.conversation) {
                    conversations[id] = body.conversation;
                    if (socket && socket.connected) socket.emit('support:read', { conversation_id: id });
                    renderList(currentConversationRows());
                }
            }).catch(function () {});
    }

    function sendMessage(body) {
        if (!selectedId || !body) return;
        if (socket && socket.connected) {
            socket.emit('support:message:send', {
                conversation_id: selectedId,
                body: body,
                client_nonce: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now())
            });
            return;
        }
        requestJson('/api/admin/support/conversations/' + encodeURIComponent(selectedId) + '/messages', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                body: body,
                client_nonce: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now())
            })
        }).then(function (payload) {
            if (payload.conversation) upsertConversation(payload.conversation);
            renderMessage(payload.message);
        }).catch(function () {
            input.value = body;
            autosize();
        });
    }

    function mutateConversation(action, payload) {
        if (!selectedId) return;
        return requestJson('/api/admin/support/conversations/' + encodeURIComponent(selectedId) + '/' + action, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {})
        }).then(function (body) {
            upsertConversation(body.conversation);
            if (body.conversation) updateHeader(body.conversation);
            if (action === 'close') setStatusFilter('closed');
            if (action === 'reopen') setStatusFilter('open');
        });
    }

    function deleteConversation() {
        if (!selectedId) return;
        var conv = conversations[selectedId] || {};
        var label = conv.user_label || conv.user_id || selectedId;
        if (!window.confirm('Delete this support conversation for ' + label + '? This removes the thread and its messages.')) {
            return;
        }
        var id = selectedId;
        return requestJson('/api/admin/support/conversations/' + encodeURIComponent(id), {
            method: 'DELETE'
        }).then(function () {
            delete conversations[id];
            resetThread();
            renderList(currentConversationRows());
        }).catch(function () {
            window.alert('Could not delete this conversation. Please try again.');
        });
    }

    function setStatusFilter(nextStatus) {
        statusFilter = nextStatus || 'open';
        document.querySelectorAll('[data-filter-status]').forEach(function (btn) {
            btn.classList.toggle('active', (btn.dataset.filterStatus || '') === statusFilter);
        });
        return loadConversations();
    }

    function autosize() {
        if (!input) return;
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 140) + 'px';
    }

    function notificationsSupported() {
        return typeof window.Notification !== 'undefined';
    }

    function updateNotifyButton() {
        if (!notifyToggleBtn) return;
        var icon = notifyToggleBtn.querySelector('i');
        notifyToggleBtn.classList.remove('is-on', 'is-blocked');
        if (!notificationsSupported()) {
            notifyToggleBtn.disabled = true;
            notifyToggleBtn.title = 'Notifications are not supported in this browser';
            notifyToggleBtn.setAttribute('aria-pressed', 'false');
            if (icon) icon.className = 'fas fa-bell-slash';
            return;
        }
        if (Notification.permission === 'denied') {
            notifyToggleBtn.classList.add('is-blocked');
            notifyToggleBtn.title = 'Notifications are blocked in browser settings';
            notifyToggleBtn.setAttribute('aria-pressed', 'false');
            if (icon) icon.className = 'fas fa-bell-slash';
            return;
        }
        if (notifyEnabled && Notification.permission === 'granted') {
            notifyToggleBtn.classList.add('is-on');
            notifyToggleBtn.title = 'Browser notifications on — click to mute';
            notifyToggleBtn.setAttribute('aria-pressed', 'true');
            if (icon) icon.className = 'fas fa-bell';
        } else {
            notifyToggleBtn.title = 'Enable browser notifications for new messages';
            notifyToggleBtn.setAttribute('aria-pressed', 'false');
            if (icon) icon.className = 'fas fa-bell-slash';
        }
    }

    function setNotifyEnabled(value) {
        notifyEnabled = !!value;
        try { window.localStorage.setItem(NOTIFY_LS_KEY, notifyEnabled ? '1' : '0'); } catch (e) {}
        updateNotifyButton();
    }

    function initNotifyState() {
        if (!notificationsSupported()) {
            updateNotifyButton();
            return;
        }
        var stored = '0';
        try { stored = window.localStorage.getItem(NOTIFY_LS_KEY) || '0'; } catch (e) {}
        notifyEnabled = stored === '1' && Notification.permission === 'granted';
        updateNotifyButton();
    }

    function toggleNotifications() {
        if (!notificationsSupported()) return;
        if (Notification.permission === 'denied') return;
        if (notifyEnabled) {
            setNotifyEnabled(false);
            return;
        }
        if (Notification.permission === 'granted') {
            setNotifyEnabled(true);
            return;
        }
        try {
            var p = Notification.requestPermission(function (perm) {
                setNotifyEnabled(perm === 'granted');
            });
            if (p && typeof p.then === 'function') {
                p.then(function (perm) { setNotifyEnabled(perm === 'granted'); });
            }
        } catch (e) {
            updateNotifyButton();
        }
    }

    function maybeShowNotification(payload) {
        if (!notifyEnabled || !notificationsSupported()) return;
        if (Notification.permission !== 'granted') return;
        if (!document.hidden) return;
        var msg = payload && payload.message;
        var conv = payload && payload.conversation;
        if (!msg || !conv) return;
        if (msg.sender_type !== 'user') return;

        var label = conv.user_label || conv.user_id || 'New support message';
        var body = (msg.body || '').slice(0, 140);
        var tag = 'scx-support-' + conv.id;
        try {
            if (activeNotifications[tag]) {
                try { activeNotifications[tag].close(); } catch (e) {}
            }
            var n = new Notification(label, {
                body: body,
                tag: tag,
                renotify: true
            });
            activeNotifications[tag] = n;
            n.onclick = function () {
                try { window.focus(); } catch (e) {}
                if (conv.id && conv.id !== selectedId) selectConversation(conv.id);
                try { n.close(); } catch (e) {}
            };
            n.onclose = function () {
                if (activeNotifications[tag] === n) delete activeNotifications[tag];
            };
        } catch (e) {}
    }

    function initSocket() {
        if (typeof io === 'undefined') return;
        socket = io('/support', { transports: ['websocket', 'polling'] });
        socket.on('support:conversation:new', function (payload) { upsertConversation(payload.conversation); });
        socket.on('support:conversation:update', function (payload) { upsertConversation(payload.conversation); });
        socket.on('support:message:new', function (payload) {
            if (payload.conversation) upsertConversation(payload.conversation);
            if (payload.conversation && payload.conversation.id === selectedId) {
                renderMessage(payload.message);
                markRead(selectedId);
            }
            maybeShowNotification(payload);
        });
    }

    document.querySelectorAll('[data-filter-status]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            setStatusFilter(btn.dataset.filterStatus || 'open');
        });
    });
    searchEl.addEventListener('input', function () {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(loadConversations, 250);
    });
    refreshBtn.addEventListener('click', loadConversations);
    if (notifyToggleBtn) notifyToggleBtn.addEventListener('click', toggleNotifications);
    composer.addEventListener('submit', function (e) {
        e.preventDefault();
        var body = input.value.trim();
        if (!body) return;
        input.value = '';
        autosize();
        sendMessage(body);
    });
    input.addEventListener('input', autosize);
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            composer.dispatchEvent(new Event('submit', { cancelable: true }));
        }
    });
    closeBtn.addEventListener('click', function () { mutateConversation('close'); });
    reopenBtn.addEventListener('click', function () { mutateConversation('reopen'); });
    deleteBtn.addEventListener('click', deleteConversation);

    initNotifyState();
    initSocket();
    resetThread();
    loadConversations();
})();
