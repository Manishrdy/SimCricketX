/* SimCricketX issue reporting widget — PLAN-IR-001 Phase 1
 *
 * Wires up the floating "Report an issue" button:
 *   - opens the modal
 *   - submits the form to /api/issues/report (CSRF token is auto-injected
 *     by the global fetch wrapper in layout.html)
 *   - shows success / error feedback inline
 *
 * Pure vanilla JS, no framework deps.
 */
(function () {
    'use strict';

    var trigger = document.getElementById('scx-report-issue-trigger');
    var modal = document.getElementById('scx-report-issue-modal');
    if (!trigger || !modal) {
        return;
    }

    var form = document.getElementById('scx-report-issue-form');
    var statusEl = document.getElementById('scx-report-status');
    var submitBtn = document.getElementById('scx-report-submit');
    var titleInput = document.getElementById('scx-report-title');
    var descInput = document.getElementById('scx-report-description');
    var categoryInput = document.getElementById('scx-report-category');

    // Prefill context can come from the crash page (500.html sets
    // window.scxReportIssuePrefill before clicking the trigger). We snapshot
    // it into a local var on modal-open so it survives even if the caller
    // mutates the global afterwards.
    var activePrefill = null;

    function setStatus(text, kind) {
        if (!statusEl) return;
        statusEl.textContent = text || '';
        statusEl.classList.remove('is-success', 'is-error');
        if (kind === 'success') statusEl.classList.add('is-success');
        if (kind === 'error') statusEl.classList.add('is-error');
    }

    function openModal() {
        modal.classList.add('is-open');
        modal.setAttribute('aria-hidden', 'false');
        setStatus('');

        // Snapshot any pending prefill set by the 500 page or another caller.
        activePrefill = null;
        if (window.scxReportIssuePrefill && typeof window.scxReportIssuePrefill === 'object') {
            activePrefill = window.scxReportIssuePrefill;
            if (activePrefill.category && categoryInput) {
                categoryInput.value = activePrefill.category;
            }
            if (activePrefill.title && titleInput && !titleInput.value) {
                titleInput.value = activePrefill.title;
            }
            if (activePrefill.description && descInput && !descInput.value) {
                descInput.value = activePrefill.description;
            }
            // One-shot: consume the prefill so the next manual open is blank.
            try { delete window.scxReportIssuePrefill; } catch (e) { window.scxReportIssuePrefill = undefined; }
        }

        // Focus the first empty interactive element after the open animation.
        setTimeout(function () {
            if (descInput && descInput.value && titleInput && !titleInput.value) {
                titleInput.focus();
            } else if (descInput && descInput.value) {
                descInput.focus();
                try { descInput.setSelectionRange(descInput.value.length, descInput.value.length); } catch (e) {}
            } else if (titleInput) {
                titleInput.focus();
            }
        }, 30);
    }

    function closeModal() {
        modal.classList.remove('is-open');
        modal.setAttribute('aria-hidden', 'true');
        activePrefill = null;
    }

    trigger.addEventListener('click', openModal);

    // Any element with [data-scx-close] dismisses the modal.
    modal.querySelectorAll('[data-scx-close]').forEach(function (el) {
        el.addEventListener('click', closeModal);
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && modal.classList.contains('is-open')) {
            closeModal();
        }
    });

    function readAppVersion() {
        var meta = document.querySelector('meta[name="scx-app-version"]');
        if (meta && meta.content) return meta.content;
        return '';
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();

        var title = (titleInput.value || '').trim();
        var description = (descInput.value || '').trim();

        if (!title) {
            setStatus('Please enter a title.', 'error');
            titleInput.focus();
            return;
        }
        if (!description) {
            setStatus('Please describe the issue.', 'error');
            descInput.focus();
            return;
        }

        submitBtn.disabled = true;
        setStatus('Submitting…');

        var payload = {
            category: categoryInput.value || 'other',
            title: title,
            description: description,
            page_url: window.location.href,
            app_version: readAppVersion()
        };
        if (activePrefill && activePrefill.exception_log_id) {
            payload.exception_log_id = activePrefill.exception_log_id;
        }

        fetch('/api/issues/report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            credentials: 'same-origin'
        })
            .then(function (resp) {
                return resp.json().then(function (body) {
                    return { status: resp.status, body: body };
                }).catch(function () {
                    return { status: resp.status, body: {} };
                });
            })
            .then(function (result) {
                submitBtn.disabled = false;
                if (result.status >= 200 && result.status < 300) {
                    var ref = (result.body && result.body.public_id) ? ' (' + result.body.public_id + ')' : '';
                    setStatus('Thanks — your report was received' + ref + '.', 'success');
                    titleInput.value = '';
                    descInput.value = '';
                    setTimeout(closeModal, 1800);
                } else if (result.status === 429) {
                    var msg = (result.body && result.body.error) || 'Rate limit reached. Please try again later.';
                    setStatus(msg, 'error');
                } else {
                    var err = (result.body && result.body.error) || 'Failed to submit report.';
                    setStatus(err, 'error');
                }
            })
            .catch(function () {
                submitBtn.disabled = false;
                setStatus('Network error — please try again.', 'error');
            });
    });
})();
