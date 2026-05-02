(function () {
    'use strict';

    var trigger = document.getElementById('scx-support-trigger');
    var panel = document.getElementById('scx-support-panel');
    if (!trigger || !panel) return;

    var closeBtn = document.getElementById('scx-support-close');
    var messagesEl = document.getElementById('scx-support-messages');
    var form = document.getElementById('scx-support-form');
    var input = document.getElementById('scx-support-input');
    var sendBtn = document.getElementById('scx-support-send');
    var statusEl = document.getElementById('scx-support-status');
    var connectionEl = document.getElementById('scx-support-connection');
    var unreadEl = document.getElementById('scx-support-unread');

    var socket = null;
    var conversationId = null;
    var seenMessageIds = {};
    var cooldownTimer = null;
    var cooldownUntil = 0;
    var lockedUntilAdminReply = false;
    var isOpen = false;
    var currentStatus = 'open';

    function setStatus(text, kind) {
        statusEl.textContent = text || '';
        statusEl.classList.toggle('is-error', kind === 'error');
    }

    function setConnection(text) {
        connectionEl.textContent = text || '';
    }

    function setUnread(count) {
        count = Number(count || 0);
        if (!unreadEl) return;
        if (count > 0) {
            unreadEl.hidden = false;
            unreadEl.textContent = count > 99 ? '99+' : String(count);
        } else {
            unreadEl.hidden = true;
            unreadEl.textContent = '0';
        }
    }

    function applyConversation(conv) {
        if (!conv) return;
        conversationId = conv.id;
        currentStatus = conv.status || 'open';
        renderConversationStatus();
    }

    function renderConversationStatus() {
        if (cooldownUntil && cooldownUntil > Date.now()) return;
        if (lockedUntilAdminReply) {
            setStatus('Message limit reached. You can send again after admin replies.');
            return;
        }
        if (currentStatus === 'closed') {
            setStatus('This conversation was closed. Send a message to reopen it.');
        } else if (statusEl.textContent === 'This conversation was closed. Send a message to reopen it.') {
            setStatus('');
        }
    }

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function ensureNotEmpty() {
        var empty = messagesEl.querySelector('.scx-support-empty');
        if (empty) empty.remove();
    }

    function formatMessageTime(value) {
        if (!value) return '';
        var date = new Date(value);
        if (Number.isNaN(date.getTime())) return '';
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function renderMessage(message) {
        if (!message || seenMessageIds[message.id]) return;
        seenMessageIds[message.id] = true;
        ensureNotEmpty();
        var isUser = message.sender_type === 'user';
        var row = document.createElement('div');
        row.className = 'scx-support-msg ' + (isUser ? 'is-user' : 'is-admin');
        var avatar = document.createElement('div');
        avatar.className = 'scx-support-msg__avatar';
        avatar.innerHTML = isUser ? '<i class="fa-solid fa-user"></i>' : '<i class="fa-solid fa-headset"></i>';
        var stack = document.createElement('div');
        stack.className = 'scx-support-msg__stack';
        var bubble = document.createElement('div');
        bubble.className = 'scx-support-msg__bubble';
        bubble.textContent = message.body || '';
        var meta = document.createElement('div');
        meta.className = 'scx-support-msg__meta';
        var who = document.createElement('span');
        who.textContent = isUser ? 'You' : 'Admin';
        var time = document.createElement('span');
        time.textContent = formatMessageTime(message.created_at);
        meta.appendChild(who);
        if (time.textContent) {
            meta.appendChild(document.createTextNode('•'));
            meta.appendChild(time);
        }
        stack.appendChild(bubble);
        stack.appendChild(meta);
        row.appendChild(avatar);
        row.appendChild(stack);
        messagesEl.appendChild(row);
        scrollToBottom();
    }

    function renderMessages(messages) {
        messagesEl.innerHTML = '';
        seenMessageIds = {};
        if (!messages || !messages.length) {
            messagesEl.innerHTML = '<div class="scx-support-empty"><div class="scx-support-empty__icon"><i class="fa-regular fa-comments"></i></div><strong>Need help?</strong><span>Send a message and an admin will reply here.</span></div>';
            return;
        }
        messages.forEach(renderMessage);
    }

    function markRead() {
        if (!conversationId || !isOpen) return;
        fetch('/api/support/conversations/' + encodeURIComponent(conversationId) + '/read', {
            method: 'POST',
            credentials: 'same-origin'
        }).then(function () {
            setUnread(0);
            if (socket && socket.connected) {
                socket.emit('support:read', { conversation_id: conversationId });
            }
        }).catch(function () {});
    }

    function loadCurrent() {
        return fetch('/api/support/current', { credentials: 'same-origin' })
            .then(function (resp) { return resp.json(); })
            .then(function (body) {
                if (body.conversation) {
                    applyConversation(body.conversation);
                    setUnread(body.conversation.unread_count || 0);
                    if (socket && socket.connected) {
                        socket.emit('support:conversation:join', { conversation_id: conversationId });
                    }
                }
                renderMessages(body.messages || []);
                applyRateState(body.rate);
                if (isOpen) markRead();
            })
            .catch(function () {
                setStatus('Could not load support messages.', 'error');
            });
    }

    function openPanel() {
        isOpen = true;
        panel.classList.add('is-open');
        panel.setAttribute('aria-hidden', 'false');
        loadCurrent().then(function () {
            setTimeout(function () { input.focus(); }, 30);
        });
    }

    function closePanel() {
        isOpen = false;
        panel.classList.remove('is-open');
        panel.setAttribute('aria-hidden', 'true');
    }

    function formatRemaining(ms) {
        var total = Math.max(1, Math.ceil(ms / 1000));
        var minutes = Math.floor(total / 60);
        var seconds = total % 60;
        return minutes + ':' + String(seconds).padStart(2, '0');
    }

    function clearCooldown() {
        cooldownUntil = 0;
        if (cooldownTimer) window.clearInterval(cooldownTimer);
        cooldownTimer = null;
        input.disabled = lockedUntilAdminReply;
        sendBtn.disabled = lockedUntilAdminReply;
        if (statusEl.textContent.indexOf('another message in') !== -1) {
            setStatus('');
        }
        renderConversationStatus();
    }

    function clearRateLock() {
        lockedUntilAdminReply = false;
        clearCooldown();
    }

    function startCooldown(seconds) {
        cooldownUntil = Date.now() + (Number(seconds || 60) * 1000);
        input.disabled = true;
        sendBtn.disabled = true;
        if (cooldownTimer) window.clearInterval(cooldownTimer);
        function tick() {
            var remaining = cooldownUntil - Date.now();
            if (remaining <= 0) {
                clearCooldown();
                return;
            }
            setStatus('You can send another message in ' + formatRemaining(remaining) + '.');
        }
        tick();
        cooldownTimer = window.setInterval(tick, 500);
    }

    function applyRateState(rate) {
        if (!rate) return;
        if (rate.allowed !== false) {
            clearRateLock();
            return;
        }
        if (rate.blocked_until_admin_reply || rate.mode === 'until_admin_reply') {
            lockedUntilAdminReply = true;
            clearCooldown();
            input.disabled = true;
            sendBtn.disabled = true;
            setStatus('Message limit reached. You can send again after admin replies.');
            return;
        }
        startCooldown(rate.retry_after || 60);
    }

    function makeNonce() {
        if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
        return String(Date.now()) + '-' + Math.random().toString(16).slice(2);
    }

    function sendViaHttp(body, nonce) {
        return fetch('/api/support/messages', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({
                body: body,
                client_nonce: nonce,
                page_url: window.location.href,
                app_version: panel.getAttribute('data-app-version') || ''
            })
        }).then(function (resp) {
            return resp.json().then(function (payload) {
                return { status: resp.status, payload: payload };
            });
        }).then(function (result) {
            if (result.status === 429) {
                applyRateState(result.payload.rate || { allowed: false, retry_after: result.payload.retry_after || 60 });
                return { blocked: true };
            }
            if (result.status >= 300) {
                throw new Error(result.payload.error || 'send_failed');
            }
            conversationId = result.payload.conversation.id;
            renderMessage(result.payload.message);
            applyRateState(result.payload.rate);
            markRead();
            return { blocked: result.payload.rate && result.payload.rate.allowed === false };
        });
    }

    function sendMessage(body) {
        var nonce = makeNonce();
        setStatus('Sending...');
        sendBtn.disabled = true;
        var payload = {
            body: body,
            client_nonce: nonce,
            page_url: window.location.href,
            app_version: panel.getAttribute('data-app-version') || ''
        };
        if (conversationId) payload.conversation_id = conversationId;

        if (socket && socket.connected) {
            socket.emit('support:message:send', payload);
            setStatus('');
            sendBtn.disabled = false;
            return Promise.resolve();
        }

        return sendViaHttp(body, nonce).then(function (result) {
            if (!result || !result.blocked) setStatus('');
        }).catch(function () {
            setStatus('Could not send message. Please try again.', 'error');
        }).finally(function () {
            if (!cooldownUntil && !lockedUntilAdminReply) sendBtn.disabled = false;
        });
    }

    function initSocket() {
        if (typeof io === 'undefined') {
            setConnection('Realtime unavailable');
            return;
        }
        socket = io('/support', { transports: ['websocket', 'polling'] });
        socket.on('connect', function () {
            setConnection('Connected');
            if (conversationId) socket.emit('support:conversation:join', { conversation_id: conversationId });
        });
        socket.on('disconnect', function () {
            setConnection('Reconnecting...');
        });
        socket.on('support:hello', function () {
            setConnection('Connected');
        });
        socket.on('support:message:new', function (payload) {
            if (payload.conversation) {
                applyConversation(payload.conversation);
                if (!isOpen) setUnread(payload.conversation.unread_count || 0);
                if (socket && socket.connected) {
                    socket.emit('support:conversation:join', { conversation_id: conversationId });
                }
            }
            renderMessage(payload.message);
            if (payload.message && payload.message.sender_type === 'admin') {
                clearRateLock();
            }
            applyRateState(payload.rate);
            if (isOpen) markRead();
        });
        socket.on('support:error', function (payload) {
            if (payload && payload.error === 'rate_limited') {
                applyRateState(payload.rate || { allowed: false, retry_after: payload.retry_after || 60 });
                return;
            }
            setStatus('Could not send message. Please try again.', 'error');
            if (!cooldownUntil) sendBtn.disabled = false;
        });
        socket.on('support:conversation:update', function (payload) {
            if (payload && payload.conversation) {
                applyConversation(payload.conversation);
                if (!isOpen) setUnread(payload.conversation.unread_count || 0);
            }
        });
    }

    trigger.addEventListener('click', function () {
        if (panel.classList.contains('is-open')) closePanel();
        else openPanel();
    });

    closeBtn.addEventListener('click', closePanel);

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var body = (input.value || '').trim();
        if (!body || cooldownUntil || lockedUntilAdminReply) return;
        input.value = '';
        sendMessage(body);
    });

    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            form.dispatchEvent(new Event('submit', { cancelable: true }));
        }
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && panel.classList.contains('is-open')) closePanel();
    });

    initSocket();
    loadCurrent();
})();
