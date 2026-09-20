(function () {
    'use strict';

    var shellEl = document.getElementById('support-shell');
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
    var contextToggleBtn = document.getElementById('support-context-toggle');
    var contextCloseBtn = document.getElementById('support-context-close');
    var userLinkBtn = document.getElementById('support-user-link');
    var composer = document.getElementById('support-admin-composer');
    var input = document.getElementById('support-admin-input');
    var sendBtn = document.getElementById('support-admin-send');
    var closeBtn = document.getElementById('support-close');
    var reopenBtn = document.getElementById('support-reopen');
    var deleteBtn = document.getElementById('support-delete');

    var socket = null;
    var selectedId = null;
    // A selected inbox row is not a safe reply target until its view has loaded.
    // The generation also distinguishes A -> B -> A from the original A request.
    var conversationRequest = 0;
    var conversationReady = false;
    var composerDrafts = {};
    var outbox = {};
    var sendStatus = document.getElementById('support-send-status');
    var pendingThreadMessages = [];
    var conversations = {};
    var statusFilter = 'open';
    var searchTimer = null;
    var seenMessageIds = {};
    var renderedMessages = [];

    var GROUP_WINDOW_MS = 3 * 60 * 1000;
    var COMPOSER_MAX_HEIGHT = 320;

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
            if (diff < 60 * 1000) return 'just now';
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

    function escapeHtml(text) {
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function linkify(text) {
        var escaped = escapeHtml(text);
        var urlRegex = /https?:\/\/[^\s<]+/g;
        return escaped.replace(urlRegex, function (url) {
            return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
        });
    }

    function copyToClipboard(text, btn) {
        if (!text) return;
        if (navigator && navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () {
                if (btn) {
                    var origHtml = btn.innerHTML;
                    btn.innerHTML = '<i class="fas fa-check" style="color:#10b981;"></i>';
                    setTimeout(function () { btn.innerHTML = origHtml; }, 1400);
                }
            }).catch(function () {});
        }
    }

    function toggleContextPanel(force) {
        if (!shellEl) return;
        var shouldCollapse = force !== undefined ? !force : !shellEl.classList.contains('context-collapsed');
        shellEl.classList.toggle('context-collapsed', shouldCollapse);
        if (contextToggleBtn) {
            contextToggleBtn.setAttribute('aria-expanded', String(!shouldCollapse));
            contextToggleBtn.classList.toggle('is-on', !shouldCollapse);
        }
    }

    function loadConversations() {
        var params = new URLSearchParams();
        if (statusFilter) params.set('status', statusFilter);
        if (searchEl && searchEl.value.trim()) params.set('q', searchEl.value.trim());
        if (listEl) {
            listEl.innerHTML = '<div class="support-empty"><div class="support-empty__icon"><i class="fas fa-spinner fa-spin"></i></div><span>Loading conversations…</span></div>';
        }
        return requestJson('/api/admin/support/conversations?' + params.toString())
            .then(function (body) {
                conversations = {};
                (body.conversations || []).forEach(function (conv) {
                    conversations[conv.id] = conv;
                });
                renderList(currentConversationRows());
            })
            .catch(function () {
                if (listEl) {
                    listEl.innerHTML = '<div class="support-empty"><div class="support-empty__icon"><i class="fas fa-triangle-exclamation"></i></div><span>Could not load conversations.</span></div>';
                }
            });
    }

    function upsertConversation(conv) {
        if (!conv) return;
        conversations[conv.id] = conv;
        renderList(currentConversationRows());
        if (conversationReady && selectedId === conv.id) updateHeader(conv);
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
        if (!listEl) return;
        if (!rows.length) {
            listEl.innerHTML = '<div class="support-empty"><div class="support-empty__icon"><i class="fas fa-inbox"></i></div><strong>No conversations</strong><span>No messages match the current filter.</span></div>';
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
            nameEl.title = nameEl.textContent;
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
            preview.title = preview.textContent;

            var trail = document.createElement('span');
            trail.style.display = 'inline-flex';
            trail.style.alignItems = 'center';
            trail.style.gap = '6px';
            trail.style.flexShrink = '0';

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
        if (conv.source_page_url) sub += ' · started from ' + conv.source_page_url;
        else if (conv.last_message_at) sub += ' · last active ' + fmt(conv.last_message_at);
        subtitleEl.textContent = sub;
        subtitleEl.title = sub;

        closeBtn.disabled = conv.status === 'closed';
        reopenBtn.disabled = conv.status !== 'closed';
        reopenBtn.hidden = conv.status !== 'closed';
        closeBtn.hidden = conv.status === 'closed';
        deleteBtn.disabled = false;
        deleteBtn.hidden = false;

        if (typeof userLinkBtn !== 'undefined' && userLinkBtn) {
            var uid = conv.user_id;
            if (uid) {
                userLinkBtn.href = '/admin/users?q=' + encodeURIComponent(uid);
                userLinkBtn.hidden = false;
            } else {
                userLinkBtn.hidden = true;
            }
        }

        input.disabled = conv.status === 'closed';
        sendBtn.disabled = conv.status === 'closed';
        updateSendState();
    }

    function resetThread() {
        conversationRequest++;
        conversationReady = false;
        pendingThreadMessages = [];
        selectedId = null;
        if (sendStatus) sendStatus.textContent = '';
        renderedMessages = [];
        seenMessageIds = {};
        titleEl.textContent = 'Select a conversation';
        subtitleEl.textContent = 'Live user messages appear in the inbox.';
        threadAvatarEl.classList.add('is-empty');
        threadAvatarEl.innerHTML = '<i class="fas fa-comment-dots"></i>';
        threadStatusEl.hidden = true;
        threadStatusEl.textContent = '';
        threadStatusEl.className = 'support-status-pill';
        if (typeof userLinkBtn !== 'undefined' && userLinkBtn) userLinkBtn.hidden = true;
        messagesEl.innerHTML = '<div class="support-empty">' +
            '<div class="support-empty__icon"><i class="fas fa-comments"></i></div>' +
            '<strong>No conversation selected</strong>' +
            '<span>Choose a user from the inbox on the left to read and respond to their messages.</span>' +
            '</div>';
        contextEl.innerHTML = '<div class="support-empty"><div class="support-empty__icon"><i class="fas fa-user-gear"></i></div><strong>No user selected</strong><span>Select a conversation to view account details and diagnostic context.</span></div>';
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
        if (selectedId && conversationReady) composerDrafts[selectedId] = input.value;
        resetThread();
        selectedId = id;
        var request = conversationRequest;
        titleEl.textContent = 'Loading conversation…';
        subtitleEl.textContent = '';
        messagesEl.innerHTML = '<div class="support-empty"><div class="support-empty__icon"><i class="fas fa-spinner fa-spin"></i></div><span>Loading messages…</span></div>';
        renderList(currentConversationRows());
        if (socket && socket.connected) socket.emit('support:conversation:join', { conversation_id: id });
        return requestJson('/api/admin/support/conversations/' + encodeURIComponent(id))
            .then(function (body) {
                if (request !== conversationRequest || selectedId !== id) return;
                if (!body.conversation || body.conversation.id !== id) throw new Error('conversation_mismatch');
                conversations[id] = body.conversation;
                renderMessages(body.messages || []);
                pendingThreadMessages.forEach(renderMessage);
                pendingThreadMessages = [];
                renderContext(body.context || {}, body.conversation);
                input.value = composerDrafts[id] || '';
                autosize();
                conversationReady = true;
                updateHeader(body.conversation);
                markRead(id);
                renderList(currentConversationRows());
                if (!input.disabled) input.focus();
            })
            .catch(function () {
                if (request !== conversationRequest || selectedId !== id) return;
                titleEl.textContent = 'Conversation unavailable';
                messagesEl.innerHTML = '<div class="support-empty"><div class="support-empty__icon"><i class="fas fa-triangle-exclamation"></i></div><span>Could not load conversation. Select it again to retry.</span></div>';
            });
    }

    function renderMessages(messages) {
        messagesEl.innerHTML = '';
        seenMessageIds = {};
        renderedMessages = [];
        if (!messages.length) {
            messagesEl.innerHTML = '<div class="support-empty">' +
                '<div class="support-empty__icon"><i class="fas fa-feather-pointed"></i></div>' +
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
        bubble.innerHTML = linkify(msg.body || '');
        var time = fmtTime(msg.created_at);
        if (time) bubble.setAttribute('data-time', time);

        var meta = document.createElement('div');
        meta.className = 'support-msg__meta';
        var senderLabel = msg.sender_type === 'admin' ? 'Support' : (conversations[selectedId] ? (conversations[selectedId].user_label || 'User') : 'User');
        meta.innerHTML = '<span class="support-msg__sender">' + escapeHtml(senderLabel) + '</span>' +
            (time ? '<span class="support-msg__dot">·</span><time class="support-msg__time">' + escapeHtml(time) + '</time>' : '');

        row.appendChild(bubble);
        row.appendChild(meta);
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
            '<h4><span><i class="fas fa-user"></i> Account</span></h4>' +
            '<div class="support-kv">' +
                '<div><span>User ID</span><strong id="ctx-user-id"></strong></div>' +
                '<div><span>Name</span><strong></strong></div>' +
                '<div><span>Role</span><strong></strong></div>' +
                '<div><span>Created</span><strong></strong></div>' +
                '<div><span>Last login</span><strong></strong></div>' +
            '</div>';
        var aStrongs = account.querySelectorAll('strong');
        var userIdVal = user.id || conv.user_id || '';
        setStrong(aStrongs[0], userIdVal);
        if (userIdVal) {
            var copyUserBtn = document.createElement('button');
            copyUserBtn.type = 'button';
            copyUserBtn.className = 'support-copy-btn';
            copyUserBtn.title = 'Copy User ID';
            copyUserBtn.innerHTML = '<i class="fas fa-copy"></i>';
            copyUserBtn.addEventListener('click', function () { copyToClipboard(userIdVal, copyUserBtn); });
            aStrongs[0].appendChild(document.createTextNode(' '));
            aStrongs[0].appendChild(copyUserBtn);
        }
        setStrong(aStrongs[1], user.display_name || '');
        setStrong(aStrongs[2], user.is_admin ? 'Administrator' : 'User');
        setStrong(aStrongs[3], fmt(user.created_at));
        setStrong(aStrongs[4], fmt(user.last_login));
        contextEl.appendChild(account);

        var convCard = document.createElement('section');
        convCard.className = 'support-context-card';
        convCard.innerHTML =
            '<h4><span><i class="fas fa-comments"></i> Conversation</span></h4>' +
            '<div class="support-kv">' +
                '<div><span>ID</span><strong></strong></div>' +
                '<div><span>App</span><strong></strong></div>' +
                '<div><span>Status</span><strong></strong></div>' +
                '<div><span>Source</span><strong></strong></div>' +
                '<div><span>Started</span><strong></strong></div>' +
            '</div>';
        var cStrongs = convCard.querySelectorAll('strong');
        var convIdVal = conv.id || '';
        setStrong(cStrongs[0], convIdVal);
        if (convIdVal) {
            var copyConvBtn = document.createElement('button');
            copyConvBtn.type = 'button';
            copyConvBtn.className = 'support-copy-btn';
            copyConvBtn.title = 'Copy Conversation ID';
            copyConvBtn.innerHTML = '<i class="fas fa-copy"></i>';
            copyConvBtn.addEventListener('click', function () { copyToClipboard(convIdVal, copyConvBtn); });
            cStrongs[0].appendChild(document.createTextNode(' '));
            cStrongs[0].appendChild(copyConvBtn);
        }
        setStrong(cStrongs[1], conv.app_version || '');
        setStrong(cStrongs[2], statusInfo(conv).label);
        if (conv.source_page_url) {
            cStrongs[3].innerHTML = '<a href="' + escapeHtml(conv.source_page_url) + '" target="_blank" rel="noopener" style="color:var(--sup-primary);text-decoration:underline;">' + escapeHtml(conv.source_page_url) + '</a>';
            cStrongs[3].classList.remove('is-empty');
        } else {
            setStrong(cStrongs[3], '');
        }
        setStrong(cStrongs[4], fmt(conv.created_at));
        contextEl.appendChild(convCard);

        var exCard = document.createElement('section');
        exCard.className = 'support-context-card';
        exCard.innerHTML = '<h4><span><i class="fas fa-bug"></i> Recent Exceptions</span></h4>';
        if (!exceptions.length) {
            var noEx = document.createElement('div');
            noEx.className = 'support-empty';
            noEx.style.padding = '8px 0 0';
            noEx.style.textAlign = 'left';
            noEx.style.alignItems = 'flex-start';
            noEx.style.flexDirection = 'row';
            noEx.style.gap = '6px';
            noEx.innerHTML = '<span style="font-size:0.78rem;color:var(--fg-secondary,#64748b);">No recent exceptions logged for this user.</span>';
            exCard.appendChild(noEx);
        } else {
            var exList = document.createElement('div');
            exList.className = 'support-context-list';
            exceptions.forEach(function (row) {
                var item = document.createElement('div');
                var label = document.createElement('span');
                label.textContent = row.type || 'Exception';
                if (row.message) label.title = row.message;
                var t = document.createElement('time');
                t.textContent = fmtRelative(row.timestamp) || fmt(row.timestamp);
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

    function updateSendState() {
        var entry = outbox[selectedId];
        if (sendStatus) sendStatus.textContent = entry ? (entry.sending
            ? 'Sending… Your reply is kept until delivery is confirmed.'
            : 'Delivery not confirmed. Your reply is kept. Press Send to retry.') : '';
        if (entry && entry.sending) {
            input.disabled = true;
            sendBtn.disabled = true;
        }
    }

    function sendMessage(body) {
        if (!conversationReady || !selectedId || input.disabled || !body) return;
        var id = selectedId;
        var entry = outbox[id];
        if (entry && entry.sending) return;
        if (!entry || entry.body !== body) {
            entry = outbox[id] = {
                body: body,
                nonce: (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID()
                    : String(Date.now()) + '-' + Math.random().toString(16).slice(2)
            };
        }
        composerDrafts[id] = body;
        entry.sending = true;
        updateSendState();
        var timeout;
        return Promise.race([
            requestJson('/api/admin/support/conversations/' + encodeURIComponent(id) + '/messages', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ body: entry.body, client_nonce: entry.nonce })
            }),
            new Promise(function (_, reject) {
                timeout = window.setTimeout(function () { reject(new Error('timeout')); }, 15000);
            })
        ]).then(function (payload) {
            if (!payload.message) throw new Error('missing_acknowledgement');
            delete outbox[id];
            composerDrafts[id] = '';
            if (conversationReady && selectedId === id) {
                input.value = '';
                autosize();
                renderMessage(payload.message);
            }
            if (payload.conversation) upsertConversation(payload.conversation);
        }).catch(function () {
            composerDrafts[id] = body;
        }).finally(function () {
            window.clearTimeout(timeout);
            entry.sending = false;
            if (conversationReady && selectedId === id) updateHeader(conversations[id]);
        });
    }

    function mutateConversation(action, payload) {
        if (!conversationReady || !selectedId) return;
        var id = selectedId;
        var request = conversationRequest;
        return requestJson('/api/admin/support/conversations/' + encodeURIComponent(id) + '/' + action, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {})
        }).then(function (body) {
            upsertConversation(body.conversation);
            if (selectedId !== id || request !== conversationRequest) return;
            if (action === 'close') setStatusFilter('closed');
            if (action === 'reopen') setStatusFilter('open');
        });
    }

    function deleteConversation() {
        if (!conversationReady || !selectedId) return;
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
            delete composerDrafts[id];
            if (selectedId === id) resetThread();
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
        var nextHeight = Math.min(input.scrollHeight, COMPOSER_MAX_HEIGHT);
        input.style.height = (nextHeight || 42) + 'px';
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
                if (conversationReady) {
                    renderMessage(payload.message);
                    markRead(selectedId);
                } else {
                    pendingThreadMessages.push(payload.message);
                }
            }
            maybeShowNotification(payload);
        });
    }

    document.querySelectorAll('[data-filter-status]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            setStatusFilter(btn.dataset.filterStatus || 'open');
        });
    });

    // Canned response quick insert
    document.querySelectorAll('.support-canned-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            if (!conversationReady || !selectedId || input.disabled) return;
            var text = btn.getAttribute('data-reply') || btn.textContent.trim();
            if (!text) return;
            if (input.value && !input.value.endsWith('\n') && !input.value.endsWith(' ')) {
                input.value += ' ' + text;
            } else {
                input.value += text;
            }
            autosize();
            input.focus();
        });
    });

    if (contextToggleBtn) {
        contextToggleBtn.addEventListener('click', function () {
            toggleContextPanel();
        });
    }

    if (contextCloseBtn) {
        contextCloseBtn.addEventListener('click', function () {
            toggleContextPanel(false);
        });
    }

    searchEl.addEventListener('input', function () {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(loadConversations, 250);
    });
    refreshBtn.addEventListener('click', loadConversations);
    if (notifyToggleBtn) notifyToggleBtn.addEventListener('click', toggleNotifications);
    composer.addEventListener('submit', function (e) {
        e.preventDefault();
        if (!conversationReady || !selectedId || input.disabled) return;
        var body = input.value.trim();
        if (!body) return;
        sendMessage(body);
    });
    input.addEventListener('input', autosize);
    input.addEventListener('keydown', function (e) {
        if (e.isComposing || e.keyCode === 229) return;
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
