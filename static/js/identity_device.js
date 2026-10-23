/* First-party basic signals only. No storage or third-party fingerprint service. */
(function () {
    'use strict';
    var script = document.currentScript;
    function signals() {
        try {
            return {
                language: navigator.language,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                screen: [window.screen.width, window.screen.height],
                platform: navigator.platform
            };
        } catch (_) { return null; }
    }
    var value = signals();
    document.querySelectorAll('[data-device-fingerprint]').forEach(function (input) {
        input.value = value ? JSON.stringify(value) : '';
    });
    if (script && script.dataset.collectSession === 'true') {
        var csrf = document.querySelector('meta[name="csrf-token"]');
        if (!csrf) return;
        fetch('/api/identity/device', {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf.content},
            body: JSON.stringify({fingerprint: value})
        }).catch(function () { /* Retry on a later page; never interrupt the app. */ });
    }
}());
