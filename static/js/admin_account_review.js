(() => {
    const find = (part) => document.getElementById('identity-review-' + part);
    const form = find('run');
    if (!window.fetch || !window.DOMParser) return;

    function formatTimes(root) {
        if (!root || typeof formatUtcTime !== 'function') return;
        root.querySelectorAll('time.utc-time').forEach((time) => {
            const formatted = formatUtcTime(time.dateTime);
            if (formatted) time.textContent = formatted.short;
        });
    }

    // ── Real-time filter, search, group-by, chip, and pagination updates ──
    let filterController = null;
    let searchTimer = null;

    function buildFilterUrl(filterForm) {
        const url = new URL(filterForm.action || window.location.pathname, window.location.origin);
        const q = (filterForm.elements.q?.value || '').trim();
        const view = filterForm.elements.view?.value || 'findings';
        const finding = filterForm.elements.finding?.value || '';
        const groupBy = filterForm.elements.group_by?.value || '';
        if (q) url.searchParams.set('q', q);
        if (view && view !== 'findings') url.searchParams.set('view', view);
        if (finding) url.searchParams.set('finding', finding);
        if (groupBy) url.searchParams.set('group_by', groupBy);
        return url.toString();
    }

    function syncFormControls(targetUrl, forceSearchSync = false) {
        const filterForm = find('filter-form');
        if (!filterForm) return;
        const params = new URL(targetUrl, window.location.origin).searchParams;
        const qInput = filterForm.elements.q;
        if (qInput && (forceSearchSync || document.activeElement !== qInput)) {
            qInput.value = params.get('q') || '';
        }
        if (filterForm.elements.view) {
            filterForm.elements.view.value = params.get('view') || 'findings';
        }
        if (filterForm.elements.finding) {
            filterForm.elements.finding.value = params.get('finding') || '';
        }
        if (filterForm.elements.group_by) {
            filterForm.elements.group_by.value = params.get('group_by') || '';
        }
    }

    async function applyFilterUrl(targetUrl, { replaceHistory = false, skipHistory = false, forceSearchSync = false } = {}) {
        clearTimeout(searchTimer);
        if (filterController) filterController.abort();
        const controller = new AbortController();
        filterController = controller;

        syncFormControls(targetUrl, forceSearchSync);
        if (!skipHistory && window.history) {
            if (replaceHistory) {
                window.history.replaceState(null, '', targetUrl);
            } else if (window.location.href !== new URL(targetUrl, window.location.origin).href) {
                window.history.pushState(null, '', targetUrl);
            }
        }

        const body = find('body');
        if (body) body.classList.add('is-loading');

        try {
            const response = await fetch(targetUrl, {
                signal: controller.signal,
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            });
            if (!response.ok) return;
            const page = new DOMParser().parseFromString(await response.text(), 'text/html');
            for (const part of ['subtitle', 'group-tabs', 'filter-actions', 'body']) {
                const current = find(part);
                const next = page.getElementById('identity-review-' + part);
                if (current && next) {
                    current.replaceWith(next);
                }
            }
            formatTimes(find('body'));
        } catch (err) {
            if (err && err.name === 'AbortError') return;
        } finally {
            if (filterController === controller) {
                filterController = null;
                const currentBody = find('body');
                if (currentBody) currentBody.classList.remove('is-loading');
            }
        }
    }

    document.addEventListener('input', (event) => {
        const input = event.target;
        if (!(input instanceof HTMLInputElement) || input.name !== 'q') return;
        const filterForm = input.closest('#identity-review-filter-form');
        if (!filterForm) return;
        clearTimeout(searchTimer);
        const delay = input.value.trim() === '' ? 0 : 140;
        searchTimer = setTimeout(() => {
            applyFilterUrl(buildFilterUrl(filterForm), { replaceHistory: true });
        }, delay);
    });

    document.addEventListener('change', (event) => {
        const target = event.target;
        if (!(target instanceof HTMLSelectElement)) return;
        const filterForm = target.closest('#identity-review-filter-form');
        if (!filterForm) return;
        applyFilterUrl(buildFilterUrl(filterForm), { replaceHistory: false });
    });

    document.addEventListener('submit', (event) => {
        const filterForm = event.target;
        if (!(filterForm instanceof HTMLFormElement) || filterForm.id !== 'identity-review-filter-form') return;
        event.preventDefault();
        applyFilterUrl(buildFilterUrl(filterForm), { replaceHistory: false });
    });

    document.addEventListener('click', (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const link = event.target.closest(
            '#identity-review-group-tabs a, .review-chip-bar a, .review-group-more a, .account-review .identity-pagination a, .review-reset-link'
        );
        if (!link || !link.href) return;
        event.preventDefault();
        const isReset = link.classList.contains('review-reset-link');
        applyFilterUrl(link.href, { replaceHistory: false, forceSearchSync: isReset });
    });

    window.addEventListener('popstate', () => {
        if (find('filter-form')) {
            applyFilterUrl(window.location.href, { skipHistory: true, forceSearchSync: true });
        }
    });

    // ── Live scan execution stream ──
    if (!form || !window.ReadableStream || !window.TextDecoder) return;

    const button = form.querySelector('button');
    const buttonLabel = button.querySelector('span');
    const panel = find('live');
    const status = find('status');
    const stage = find('stage');
    const log = find('log');
    const progress = find('progress');
    const refresh = find('refresh');
    const elapsed = find('elapsed');
    const number = new Intl.NumberFormat();
    const seconds = (ms) => (ms / 1000).toFixed(1) + 's';

    function append(event) {
        const follow = log.scrollHeight - log.clientHeight - log.scrollTop < 12;
        const line = document.createElement('li');
        const time = document.createElement('time');
        time.textContent = '+' + seconds(event.elapsed_ms);
        const message = document.createElement('span');
        const details = [];
        if (event.processed !== undefined) details.push(find('processed').textContent + ' / ' + find('total').textContent);
        if (event.skipped_device_rows !== undefined) details.push(number.format(event.skipped_device_rows) + ' device rows skipped');
        if (event.device_accounts !== undefined) {
            details.push(number.format(event.device_accounts) + ' with device evidence');
            details.push(number.format(event.network_accounts) + ' with network observations');
            details.push(number.format(event.blocked_signup_groups) + ' blocked signup groups');
        }
        message.textContent = [event.message, ...details].join(' · ');
        line.append(time, message);
        log.append(line);
        if (follow) log.scrollTop = log.scrollHeight;
    }

    async function refreshResults() {
        const response = await fetch(window.location.href);
        if (!response.ok) throw new Error('Report refresh failed');
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const results = page.getElementById('identity-review-results');
        if (!results) throw new Error('Report results unavailable');
        find('results').replaceWith(results);
        formatTimes(results);
    }

    form.addEventListener('submit', async (submit) => {
        submit.preventDefault();
        if (button.disabled) return;
        button.disabled = true;
        buttonLabel.textContent = 'Running review…';
        panel.hidden = false;
        panel.dataset.status = 'running';
        status.textContent = 'Running';
        stage.textContent = 'Connecting…';
        refresh.hidden = true;
        log.replaceChildren();
        progress.removeAttribute('value');
        progress.max = 1;
        find('processed').textContent = '0';
        find('total').textContent = '—';
        find('findings').textContent = '0';
        elapsed.textContent = '0.0s';

        const started = performance.now();
        const timer = setInterval(() => { elapsed.textContent = seconds(performance.now() - started); }, 100);
        let terminal = null;
        const update = (event) => {
            if (event.status === 'heartbeat') return;
            if (event.total !== undefined) {
                find('total').textContent = number.format(event.total);
                progress.max = Math.max(event.total, 1);
                progress.value = 0;
            }
            if (event.processed !== undefined) {
                find('processed').textContent = number.format(event.processed);
                find('findings').textContent = number.format(event.findings);
                progress.value = event.processed;
            }
            stage.textContent = event.message;
            append(event);
            if (event.status === 'complete' || event.status === 'failed') terminal = event;
        };

        try {
            const response = await fetch(form.action, {
                method: 'POST',
                headers: { Accept: 'application/x-ndjson' },
                body: new FormData(form),
            });
            if (!response.ok || !response.headers.get('Content-Type')?.startsWith('application/x-ndjson')) {
                throw new Error('Progress stream unavailable');
            }
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let pending = '';
            while (!terminal) {
                const { value, done } = await reader.read();
                if (done) break;
                pending += decoder.decode(value, { stream: true });
                const lines = pending.split('\n');
                pending = lines.pop();
                for (const line of lines) {
                    if (line) update(JSON.parse(line));
                }
            }
            if (!terminal) throw new Error('Progress stream ended early');
            clearInterval(timer);
            elapsed.textContent = seconds(terminal.elapsed_ms);
            panel.dataset.status = terminal.status;
            status.textContent = terminal.status === 'complete' ? 'Complete' : 'Stopped';
            if (terminal.status === 'complete') {
                progress.value = progress.max;
                try {
                    await refreshResults();
                } catch {
                    refresh.hidden = false;
                }
            } else if (!progress.hasAttribute('value')) {
                progress.value = 0;
            }
        } catch {
            if (!progress.hasAttribute('value')) progress.value = 0;
            panel.dataset.status = 'failed';
            status.textContent = 'Connection lost';
            stage.textContent = 'Live status unavailable. Refresh the page to check for a saved report.';
            append({ message: stage.textContent, elapsed_ms: performance.now() - started });
            refresh.hidden = false;
        } finally {
            clearInterval(timer);
            button.disabled = false;
            buttonLabel.textContent = 'Run identity review';
        }
    });
})();
