// Shared focus lifecycle for dialogs and modal drawers. Visibility and business
// decisions remain owned by the page; Escape invokes only its explicit close action.
(function () {
    'use strict';
    const stack = [];
    const blocked = new Map();
    const closedDialogs = new Set();
    const selector = 'a[href], button, input, select, textarea, [tabindex], [contenteditable="true"]';
    const visible = el => el && el.isConnected && !el.closest('[inert]') &&
        getComputedStyle(el).visibility !== 'hidden' && el.getClientRects().length > 0;
    const focusable = root => Array.from(root.querySelectorAll(selector))
        .filter(el => !el.disabled && el.tabIndex >= 0 && visible(el));
    const top = () => stack[stack.length - 1];

    function updateBackground() {
        blocked.forEach((inert, el) => { el.inert = inert; });
        blocked.clear();
        closedDialogs.forEach(element => { element.inert = true; });
        if (!top()) return;
        top().element.inert = false;
        let branch = top().element;
        // Block siblings at every ancestor level without blocking the dialog.
        while (branch && branch !== document.body) {
            const parent = branch.parentElement;
            if (!parent) break;
            Array.from(parent.children).forEach(el => {
                if (el !== branch) { blocked.set(el, el.inert); el.inert = true; }
            });
            branch = parent;
        }
    }

    function focusInside(entry) {
        const initial = entry.element.querySelector('[data-dialog-initial]');
        const target = visible(initial) && !initial.disabled ? initial : focusable(entry.element)[0];
        (target || entry.element).focus({ preventScroll: true });
    }

    function open(element, options = {}) {
        if (stack.some(entry => entry.element === element)) return;
        const entry = { element, close: options.close, opener: document.activeElement,
            role: element.getAttribute('role'), modal: element.getAttribute('aria-modal') };
        element.inert = false;
        element.setAttribute('role', 'dialog');
        element.setAttribute('aria-modal', 'true');
        element.setAttribute('aria-hidden', 'false');
        if (!element.hasAttribute('tabindex')) element.setAttribute('tabindex', '-1');
        stack.push(entry);
        updateBackground();
        focusInside(entry);
    }

    function close(element) {
        const index = stack.findIndex(entry => entry.element === element);
        if (index < 0) return;
        const wasTop = index === stack.length - 1;
        const [entry] = stack.splice(index, 1);
        for (const [name, value] of [['role', entry.role], ['aria-modal', entry.modal]]) {
            if (value === null) element.removeAttribute(name); else element.setAttribute(name, value);
        }
        updateBackground();
        if (!wasTop) return;
        if (entry.opener !== document.body && visible(entry.opener) && !entry.opener.disabled && (!top() || top().element.contains(entry.opener))) {
            entry.opener.focus({ preventScroll: true });
        } else if (top()) focusInside(top());
        else {
            const preferred = document.querySelector('[data-dialog-return]');
            const fallback = visible(preferred) ? preferred : document.querySelector('main') || document.body;
            if (!fallback.hasAttribute('tabindex')) fallback.setAttribute('tabindex', '-1');
            fallback.focus({ preventScroll: true });
        }
    }

    document.addEventListener('keydown', event => {
        const entry = top();
        if (!entry || event.isComposing) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopImmediatePropagation();
            if (entry.close) entry.close();
        } else if (event.key === 'Tab') {
            const items = focusable(entry.element);
            const index = items.indexOf(document.activeElement);
            if (index < 0 || (!event.shiftKey && index === items.length - 1) || (event.shiftKey && index === 0)) {
                event.preventDefault();
                (items[event.shiftKey ? items.length - 1 : 0] || entry.element).focus();
            }
        }
    }, true);
    document.addEventListener('focusin', event => {
        if (top() && !top().element.contains(event.target)) focusInside(top());
    }, true);

    function bind(element) {
        const sync = () => {
            const openClass = element.dataset.dialogOpenClass;
            const isOpen = openClass ? element.classList.contains(openClass) :
                getComputedStyle(element).display !== 'none';
            if (isOpen) {
                closedDialogs.delete(element);
                open(element, { close: element.dataset.dialogClose ? () => {
                    const button = element.querySelector(element.dataset.dialogClose);
                    if (button && !button.disabled) button.click();
                } : null });
                if (top()?.element === element &&
                    (!element.contains(document.activeElement) || !visible(document.activeElement))) focusInside(top());
            } else {
                closedDialogs.add(element);
                close(element);
                element.inert = true;
                element.setAttribute('aria-hidden', 'true');
            }
        };
        new MutationObserver(sync).observe(element, { attributes: true,
            attributeFilter: ['class', 'style', 'hidden', 'disabled'], childList: true, subtree: true });
        sync();
    }
    window.scxDialogs = { open, close };
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[data-focus-dialog]').forEach(bind);
    });
})();
