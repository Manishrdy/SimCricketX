/* Server-authoritative impact preview. Native dialog supplies focus containment. */
(function () {
    'use strict';
    const csrf = document.currentScript.dataset.csrf;
    const dialog = document.getElementById('replay-dialog');
    const status = document.getElementById('replay-status');
    const content = document.getElementById('replay-content');
    const confirm = document.getElementById('replay-confirm');
    const refresh = document.getElementById('replay-refresh');
    let opener, token, submitting = false, sequence = 0;
    const closeButtons = [...dialog.querySelectorAll('[data-replay-close]')];
    function close() {
        if (submitting) return;
        sequence++;
        dialog.close();
        if (opener) opener.focus();
    }
    closeButtons.forEach(button => button.addEventListener('click', close));
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    async function read(response) {
        const data = await response.json().catch(() => ({error: 'Could not load the replay preview. Refresh the page and try again.'}));
        if (!response.ok || data.error) throw new Error(data.error || 'The request failed. Try again.');
        return data;
    }
    async function preview() {
        const request = ++sequence;
        token = null;
        confirm.disabled = true;
        content.hidden = true;
        refresh.hidden = true;
        status.textContent = 'Checking affected fixtures…';
        try {
            const plan = await read(await fetch(opener.dataset.replayPreview, {headers: {'Accept': 'application/json'}}));
            if (request !== sequence || !dialog.open) return;
            const selected = plan.fixtures[0];
            document.getElementById('replay-title').textContent = `Replay ${selected.home} vs ${selected.away}?`;
            const completed = plan.fixtures.filter(row => row.kind === 'completed').length;
            const running = plan.fixtures.filter(row => row.kind === 'in_progress').length;
            const unplayed = plan.fixtures.length - completed - running;
            const plural = (count, one, many = one + 's') => `${count} ${count === 1 ? one : many}`;
            document.getElementById('replay-summary').textContent = [
                `${plural(plan.fixtures.length, 'fixture')} affected.`,
                completed ? `${plural(completed, 'recorded result')} will be removed.` : '',
                running ? `${plural(running, 'unfinished match', 'unfinished matches')} will be discarded.` : '',
                unplayed ? `${plural(unplayed, 'unplayed fixture')} will wait for qualification.` : ''
            ].filter(Boolean).join(' ');
            const list = document.getElementById('replay-fixtures');
            list.replaceChildren();
            plan.fixtures.forEach(row => {
                const item = document.createElement('li');
                const title = document.createElement('strong');
                title.textContent = `${row.stage}: ${row.home} vs ${row.away}`;
                const detail = document.createElement('small');
                detail.textContent = row.kind === 'completed' ? `Remove result: ${row.result || 'Recorded match'}` : row.kind === 'in_progress' ? 'Discard unfinished match and reset qualification' : 'Reset qualification; no recorded result to remove';
                item.append(title, detail);
                list.append(item);
            });
            document.getElementById('replay-effects').textContent = [
                plan.standings_change ? 'League standings will be updated.' : '',
                plan.fixtures.length > 1 ? 'Dependent fixtures will wait for qualification again.' : '',
                plan.reopens ? 'The tournament will reopen and its winner will be undecided.' : '',
                plan.tour_change ? 'Tour results will be refreshed.' : ''
            ].filter(Boolean).join(' ');
            const blockers = document.getElementById('replay-blockers');
            blockers.replaceChildren();
            plan.blockers.forEach(problem => {
                const paragraph = document.createElement('p');
                paragraph.append(document.createTextNode(`${problem.team}: ${problem.reason} `));
                const link = document.createElement('a');
                link.href = problem.url;
                link.textContent = 'Fix squad';
                paragraph.append(link);
                blockers.append(paragraph);
            });
            token = plan.token;
            confirm.textContent = `Reset ${plan.fixtures.length} fixture${plan.fixtures.length === 1 ? '' : 's'} and continue`;
            confirm.disabled = plan.blockers.length > 0;
            content.hidden = false;
            status.textContent = plan.blockers.length ? 'Fix these squad problems before removing any results.' : '';
        } catch (error) {
            if (request !== sequence) return;
            status.textContent = error.message;
            refresh.hidden = false;
        }
    }
    document.querySelectorAll('[data-replay-preview]').forEach(button => {
        button.addEventListener('click', () => {
            opener = button;
            document.getElementById('replay-title').textContent = 'Replay fixture';
            dialog.showModal();
            preview();
        });
    });
    refresh.addEventListener('click', preview);
    confirm.addEventListener('click', async () => {
        if (!token || submitting || confirm.disabled) return;
        submitting = true;
        confirm.disabled = true;
        refresh.hidden = true;
        closeButtons.forEach(button => { button.disabled = true; });
        status.textContent = 'Resetting fixtures…';
        try {
            const result = await read(await fetch(opener.dataset.replaySubmit, {
                method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
                body: JSON.stringify({token})
            }));
            status.textContent = result.message;
            window.location.assign(result.redirect);
        } catch (error) {
            status.textContent = error.message;
            refresh.hidden = false;
            // Do not retry a destructive operation without reviewing current state.
        } finally {
            submitting = false;
            closeButtons.forEach(button => { button.disabled = false; });
        }
    });
})();
