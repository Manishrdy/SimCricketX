/* Shared guidance; limits come from the server's canonical squad rules. */
(function () {
    'use strict';
    function checks(players, captain, keeper, limits) {
        const n = players.length;
        const wk = players.filter(p => p.role === 'Wicketkeeper').length;
        const bowl = players.filter(p => ['Bowler', 'All-rounder'].includes(p.role)).length;
        const missing = Math.max(0, limits.min - n);
        const roleMissing = Math.max(0, limits.wk - wk) + Math.max(0, limits.bowl - bowl);
        const replacements = Math.max(0, roleMissing - Math.max(0, limits.max - n));
        const rows = [
            {ok: n >= limits.min && n <= limits.max, text: n > limits.max ? `Remove ${n - limits.max} players (maximum ${limits.max}).` : missing ? `Add at least ${missing} more player${missing === 1 ? '' : 's'}.` : `${n} players selected (${limits.min}–${limits.max}).`, action: n > limits.max ? 'roster' : 'all', label: n > limits.max ? 'Review squad' : 'Add players'},
            {ok: wk >= limits.wk, text: `${wk}/${limits.wk} wicketkeepers`, action: 'Wicketkeeper', label: 'Find a wicketkeeper'},
            {ok: bowl >= limits.bowl, text: `${bowl}/${limits.bowl} bowling options (bowlers / all-rounders)`, action: 'Bowling options', label: 'Find bowling options'},
            {ok: !!captain, text: captain ? 'Captain selected' : 'Choose a captain.', action: n ? 'captain' : 'all', label: n ? 'Choose captain' : 'Add players'},
            {ok: !!keeper, text: keeper ? 'Wicketkeeper designated' : 'Designate a wicketkeeper.', action: wk ? 'keeper' : 'Wicketkeeper', label: wk ? 'Choose keeper' : 'Find a wicketkeeper'}
        ];
        return {rows, ready: rows.every(r => r.ok), note: replacements ? `Replace at least ${replacements} player${replacements === 1 ? '' : 's'} to fit the missing roles within the ${limits.max}-player limit.` : missing && roleMissing ? 'Missing roles count toward the players you still need to add.' : ''};
    }
    function render(element, players, captain, keeper, limits, onAction) {
        const result = checks(players, captain, keeper, limits);
        element.replaceChildren();
        const title = document.createElement('strong');
        title.textContent = result.ready ? 'Squad readiness — Ready to publish' : 'Squad readiness';
        element.append(title);
        const list = document.createElement('ul');
        result.rows.forEach(row => {
            const item = document.createElement('li');
            item.append(document.createTextNode(`${row.ok ? '✓ ' : ''}${row.text} `));
            if (!row.ok) {
                const button = document.createElement('button');
                button.type = 'button';
                button.textContent = row.label;
                button.addEventListener('click', () => onAction(row.action));
                item.append(button);
            }
            list.append(item);
        });
        element.append(list);
        if (result.note) {
            const note = document.createElement('p');
            note.textContent = result.note;
            element.append(note);
        }
        if (result.rows.slice(0, 3).some(row => !row.ok)) {
            const help = document.createElement('p');
            help.append(document.createTextNode('Need more available players? '));
            const link = document.createElement('a');
            link.href = '/player-pool';
            link.textContent = 'Create or import players';
            help.append(link);
            element.append(help);
        }
        return result.ready;
    }
    window.SquadReadiness = {checks, render};
})();
