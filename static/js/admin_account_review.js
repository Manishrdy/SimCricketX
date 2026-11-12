(() => {
    const find = (part) => document.getElementById('identity-review-' + part);
    const form = find('run');
    if (!form || !window.fetch || !window.ReadableStream || !window.TextDecoder) return;

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
        results.querySelectorAll('time.utc-time').forEach((time) => {
            const formatted = formatUtcTime(time.dateTime);
            if (formatted) time.textContent = formatted.short;
        });
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
            }
        } catch {
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
