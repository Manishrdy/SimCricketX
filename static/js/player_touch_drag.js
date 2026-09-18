/* Touch/pen handles reuse the same validated drop paths as native mouse drags.
 * Only handles disable touch scrolling; the rest of each card remains scrollable.
 */
(function () {
    'use strict';
    let active = null;
    let frame = null;
    let lastFrame = null;
    const EDGE = 56;
    const MAX_SPEED = 480; // CSS pixels per second, independent of refresh rate.

    function updateTarget(point) {
        const target = document.elementFromPoint(point.clientX, point.clientY);
        if (active.target && active.target !== target) emit(active.target, 'dragleave', point, target);
        active.target = target;
        if (target) emit(target, 'dragover', point);
        return target;
    }

    function edgeSpeed(y, top, bottom) {
        const band = Math.min(EDGE, (bottom - top) / 3);
        if (band <= 0 || y < top || y > bottom) return 0;
        if (y < top + band) return -MAX_SPEED * (1 - (y - top) / band);
        if (y > bottom - band) return MAX_SPEED * (1 - (bottom - y) / band);
        return 0;
    }

    function autoScroll(time) {
        frame = null;
        if (!active) return;
        const dt = lastFrame === null ? 0 : Math.min(32, time - lastFrame) / 1000;
        lastFrame = time;
        const point = active.point;
        const target = updateTarget(point);
        const viewport = window.visualViewport;
        const top = viewport ? viewport.offsetTop : 0;
        const bottom = top + (viewport ? viewport.height : window.innerHeight);
        const page = document.scrollingElement;
        let node = target;
        // Scroll the nearest list first. At its boundary, allow an outer list
        // or the page to take over so stacked panels remain reachable.
        while (node) {
            if (node === page || node === document.body) break;
            if (node.scrollHeight > node.clientHeight && /auto|scroll/.test(window.getComputedStyle(node).overflowY)) {
                const rect = node.getBoundingClientRect();
                const speed = edgeSpeed(point.clientY, Math.max(top, rect.top), Math.min(bottom, rect.bottom));
                const before = node.scrollTop;
                node.scrollTop += speed * dt;
                if (node.scrollTop !== before) break;
            }
            node = node.parentElement;
        }
        if ((!node || node === page || node === document.body) && page) {
            page.scrollTop += edgeSpeed(point.clientY, top, bottom) * dt;
        }
        // Re-hit-test after scrolling even if the finger has not moved.
        updateTarget(point);
        frame = window.requestAnimationFrame(autoScroll);
    }
    function emit(target, type, point, relatedTarget) {
        const event = new Event(type, { bubbles: true, cancelable: true });
        Object.defineProperties(event, {
            dataTransfer: { value: active.transfer },
            clientX: { value: point.clientX },
            clientY: { value: point.clientY },
            relatedTarget: { value: relatedTarget || null }
        });
        target.dispatchEvent(event);
        return event.defaultPrevented;
    }
    function finish(point, drop) {
        if (!active) return;
        if (frame !== null) window.cancelAnimationFrame(frame);
        frame = null;
        lastFrame = null;
        const source = active.source;
        try {
            const target = document.elementFromPoint(point.clientX, point.clientY);
            if (drop && target && emit(target, 'dragover', point)) emit(target, 'drop', point);
            if (active.target) emit(active.target, 'dragleave', point);
            emit(source, 'dragend', point);
        } finally {
            source.classList.remove('dragging');
            active = null;
        }
    }
    document.addEventListener('pointerdown', function (event) {
        if (event.pointerType === 'mouse' || !event.isPrimary || active) return;
        const handle = event.target.closest('.player-touch-handle');
        const source = handle && handle.closest('[draggable="true"]');
        if (!source) return;
        event.preventDefault();
        const data = {};
        active = {
            source: source, id: event.pointerId, target: null,
            point: { clientX: event.clientX, clientY: event.clientY },
            transfer: {
                effectAllowed: 'all', dropEffect: 'none',
                setData: function (type, value) { data[type] = String(value); },
                getData: function (type) { return data[type] || ''; }
            }
        };
        // Release implicit touch capture so hit testing follows the finger.
        if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
        emit(source, 'dragstart', event);
        frame = window.requestAnimationFrame(autoScroll);
    }, { passive: false });
    document.addEventListener('pointermove', function (event) {
        if (!active || active.id !== event.pointerId) return;
        event.preventDefault();
        active.point = { clientX: event.clientX, clientY: event.clientY };
        updateTarget(active.point);
    }, { passive: false });
    document.addEventListener('pointerup', function (event) {
        if (active && active.id === event.pointerId) finish(event, true);
    });
    document.addEventListener('pointercancel', function (event) {
        if (active && active.id === event.pointerId) finish(event, false);
    });
    document.addEventListener('keydown', function (event) {
        if (active && event.key === 'Escape') finish(active.point, false);
    });
    document.addEventListener('visibilitychange', function () {
        if (active && document.hidden) finish(active.point, false);
    });
    window.addEventListener('blur', function () {
        if (active) finish({ clientX: -1, clientY: -1 }, false);
    });
})();
