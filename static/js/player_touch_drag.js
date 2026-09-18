/* Touch/pen handles reuse the same validated drop paths as native mouse drags.
 * Only handles disable touch scrolling; the rest of each card remains scrollable.
 */
(function () {
    'use strict';
    let active = null;
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
            transfer: {
                effectAllowed: 'all', dropEffect: 'none',
                setData: function (type, value) { data[type] = String(value); },
                getData: function (type) { return data[type] || ''; }
            }
        };
        // Release implicit touch capture so hit testing follows the finger.
        if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
        emit(source, 'dragstart', event);
    }, { passive: false });
    document.addEventListener('pointermove', function (event) {
        if (!active || active.id !== event.pointerId) return;
        event.preventDefault();
        const target = document.elementFromPoint(event.clientX, event.clientY);
        if (active.target && active.target !== target) emit(active.target, 'dragleave', event, target);
        active.target = target;
        if (target) emit(target, 'dragover', event);
    }, { passive: false });
    document.addEventListener('pointerup', function (event) {
        if (active && active.id === event.pointerId) finish(event, true);
    });
    document.addEventListener('pointercancel', function (event) {
        if (active && active.id === event.pointerId) finish(event, false);
    });
    window.addEventListener('blur', function () {
        if (active) finish({ clientX: -1, clientY: -1 }, false);
    });
})();
