(function () {
    'use strict';

    var copyButton = document.getElementById('copy-log-btn');
    var commentaryLog = document.getElementById('commentary-log');
    var fallback = document.getElementById('copy-log-fallback');
    var fallbackText = document.getElementById('copy-log-text');
    var status = document.getElementById('copy-log-status');
    var selectButton = document.getElementById('select-log-text');
    var closeButton = document.getElementById('close-copy-fallback');

    if (!copyButton || !commentaryLog || !fallback || !fallbackText) return;

    function selectFallbackText() {
        fallbackText.focus();
        fallbackText.select();
        if (fallbackText.setSelectionRange) {
            fallbackText.setSelectionRange(0, fallbackText.value.length);
        }
    }

    function showManualFallback(logContent) {
        fallbackText.value = logContent;
        fallback.hidden = false;
        copyButton.setAttribute('aria-expanded', 'true');
        status.textContent = 'Automatic copy is unavailable. The log is selected; use your device’s Copy command.';
        selectFallbackText();
    }

    function showCopiedFeedback() {
        var originalHTML = copyButton.innerHTML;
        fallback.hidden = true;
        copyButton.setAttribute('aria-expanded', 'false');
        copyButton.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
        copyButton.style.borderColor = '#10b981';
        copyButton.style.color = '#10b981';
        setTimeout(function () {
            copyButton.innerHTML = originalHTML;
            copyButton.style.borderColor = '';
            copyButton.style.color = '';
        }, 2000);
    }

    async function tryClipboardCopy(logContent) {
        if (typeof navigator === 'undefined'
                || !navigator.clipboard
                || typeof navigator.clipboard.writeText !== 'function') {
            return false;
        }
        try {
            await navigator.clipboard.writeText(logContent);
            return true;
        } catch (_) {
            return false;
        }
    }

    copyButton.addEventListener('click', async function () {
        var logContent = commentaryLog.innerText || '';
        copyButton.disabled = true;
        var copied = await tryClipboardCopy(logContent);
        copyButton.disabled = false;
        if (copied) showCopiedFeedback();
        else showManualFallback(logContent);
    });

    selectButton.addEventListener('click', function () {
        selectFallbackText();
        status.textContent = 'Log selected. Use your device’s Copy command.';
    });

    closeButton.addEventListener('click', function () {
        fallback.hidden = true;
        copyButton.setAttribute('aria-expanded', 'false');
        copyButton.focus();
    });
})();
