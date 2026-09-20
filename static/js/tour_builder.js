(function (root) {
    'use strict';
    const FORMATS = typeof window !== 'undefined' && window.CRICKET_TOUR_FORMATS || ['FC', 'ListA', 'T20', 'T10'];
    const label = fmt => fmt === 'ListA' ? 'List A' : fmt;
    function availability(teams, host, visitor) {
        const selected = Boolean(teams[host] && teams[visitor] && host !== visitor);
        return Object.fromEntries(FORMATS.map(fmt => [fmt, selected &&
            Boolean(teams[host].formats?.[fmt]?.available && teams[visitor].formats?.[fmt]?.available)]));
    }
    function syncTeamChoices(home, away, changed) {
        // Also repair restored/stale form state before calculating availability.
        if (home.value && home.value === away.value) {
            (changed === away ? home : away).value = '';
        }
        for (const [select, other] of [[home, away], [away, home]]) {
            for (const option of select.options) {
                option.disabled = Boolean(option.value && option.value === other.value);
            }
        }
    }
    function activeOrder(order, counts) {
        return [...new Set([...order, ...FORMATS])].filter(fmt => FORMATS.includes(fmt) &&
            /^\d+$/.test(String(counts[fmt])) && Number.isSafeInteger(Number(counts[fmt])) && Number(counts[fmt]) > 0);
    }
    function move(order, from, to) {
        const next = [...order];
        if (from < 0 || to < 0 || from >= next.length || to >= next.length) return next;
        next.splice(to, 0, next.splice(from, 1)[0]);
        return next;
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = { availability, activeOrder, move, syncTeamChoices };
    if (!root.document) return;
    const doc = root.document, form = doc.getElementById('tour-create-form');
    if (!form) return;
    const teams = JSON.parse(doc.getElementById('tour-team-data').textContent);
    const host = doc.getElementById('host_team_id'), visitor = doc.getElementById('visiting_team_id');
    const name = doc.getElementById('tour-name'), confirm = doc.getElementById('confirm-schedule');
    const snapshot = doc.getElementById('schedule-confirmation'), create = doc.getElementById('create-tour');
    const list = doc.getElementById('series-order'), hidden = doc.getElementById('order-inputs');
    const inputs = Object.fromEntries(FORMATS.map(fmt => [fmt, doc.getElementById('count-' + fmt)]));
    let order = JSON.parse(doc.getElementById('tour-initial-order').textContent), dragged = null, pending = false;
    const counts = () => Object.fromEntries(FORMATS.map(fmt => [fmt, inputs[fmt].value]));
    const currentSnapshot = () => JSON.stringify({name: name.value.trim(), host: host.value,
        visitor: visitor.value, counts: counts(), order, scheduled_overs: doc.getElementById('tour-scheduled-overs').value});
    function validation() {
        if (!teams[host.value] || !teams[visitor.value]) return 'Select both teams to continue.';
        if (host.value === visitor.value) return 'Choose two different teams.';
        const allowed = availability(teams, host.value, visitor.value);
        if (!Object.values(allowed).some(Boolean)) return 'These teams have no playable formats in common. Choose another team or complete their squads.';
        if (!FORMATS.every(fmt => /^\d+$/.test(inputs[fmt].value) && Number.isSafeInteger(Number(inputs[fmt].value)))) return 'Enter non-negative whole numbers for all available formats.';
        if (!order.length) return 'Add at least one match in an available format.';
        if (!name.value.trim()) return 'Enter a tour name to confirm your schedule.';
        if (!form.checkValidity()) return 'Check the highlighted fields before confirming.';
        return '';
    }
    function resetConfirmation() {
        confirm.checked = false;
        snapshot.value = '';
        create.disabled = true;
    }
    function renderOrder() {
        list.replaceChildren(); hidden.replaceChildren();
        doc.getElementById('order-empty').hidden = order.length > 0;
        order.forEach((fmt, index) => {
            const row = doc.getElementById('series-order-row').content.firstElementChild.cloneNode(true);
            row.dataset.format = fmt;
            row.querySelector('.tb-sequence').textContent = ['FIRST', 'THEN', 'FINALLY'][index];
            row.querySelector('strong').textContent = label(fmt) + ' series';
            row.querySelector('small').textContent = inputs[fmt].value + ' matches';
            row.querySelectorAll('[data-move]').forEach(button => {
                const delta = Number(button.dataset.move);
                button.disabled = index + delta < 0 || index + delta >= order.length;
                button.setAttribute('aria-label', 'Move ' + label(fmt) + (delta < 0 ? ' up' : ' down'));
                button.addEventListener('click', () => {
                    order = move(order, index, index + delta); update();
                    doc.getElementById('order-announcement').textContent = label(fmt) + ' is now series ' + (order.indexOf(fmt) + 1) + '.';
                    const buttons = [...list.querySelectorAll('[data-format="' + fmt + '"] button')];
                    (buttons.find(b => !b.disabled) || list).focus();
                });
            });
            row.addEventListener('dragstart', event => {
                dragged = fmt; event.dataTransfer.setData('text/plain', fmt); event.dataTransfer.effectAllowed = 'move';
            });
            row.addEventListener('dragover', event => { if (dragged) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; row.classList.add('is-drop-target'); } });
            row.addEventListener('dragleave', () => row.classList.remove('is-drop-target'));
            row.addEventListener('dragend', () => { dragged = null; list.querySelectorAll('.is-drop-target').forEach(e => e.classList.remove('is-drop-target')); });
            row.addEventListener('drop', event => {
                event.preventDefault();
                if (!dragged || !order.includes(dragged)) return;
                const moved = dragged;
                order = move(order, order.indexOf(dragged), order.indexOf(fmt)); dragged = null; update();
                doc.getElementById('order-announcement').textContent = label(moved) + ' is now series ' + (order.indexOf(moved) + 1) + '.';
            });
            list.append(row);
            const input = doc.createElement('input'); input.type = 'hidden'; input.name = 'format_order'; input.value = fmt; hidden.append(input);
        });
    }
    function update(event) {
        syncTeamChoices(host, visitor, event && event.target);
        resetConfirmation();
        doc.getElementById('order-announcement').textContent = '';
        const allowed = availability(teams, host.value, visitor.value);
        FORMATS.forEach(fmt => {
            const field = inputs[fmt], card = field.closest('[data-format]');
            const cleared = !allowed[fmt] && field.value !== '0';
            field.disabled = !allowed[fmt]; if (field.disabled) field.value = '0';
            card.classList.toggle('is-unavailable', field.disabled);
            doc.getElementById('availability-' + fmt).textContent = allowed[fmt] ? 'Ready to play' : 'Unavailable';
            const statuses = doc.getElementById('squads-' + fmt); statuses.replaceChildren();
            const reasons = [];
            doc.getElementById('tour-scheduled-overs').addEventListener('change', update);
    [host, visitor].forEach(select => {
                const team = teams[select.value]; if (!team) return;
                const status = team.formats[fmt], item = doc.createElement('li');
                item.className = status.available ? 'is-ready' : 'is-missing';
                item.textContent = (status.available ? '✓ ' : '× ') + team.name + (status.available ? ' · Squad ready' : ' · ' + status.reason);
                statuses.append(item);
                if (!status.available) reasons.push(team.name);
            });
            doc.getElementById('reason-' + fmt).textContent = allowed[fmt] ? 'Both squads ready. Set your match count.' :
                (reasons.length ? 'Blocked until both squads are ready.' : 'Select two different teams.') + (cleared ? ' Count reset to 0.' : ' Count is 0.');
        });
        order = activeOrder(order, counts()); renderOrder();
        const namedTeams = teams[host.value] && teams[visitor.value];
        doc.getElementById('review-teams').textContent = namedTeams ? teams[visitor.value].name + ' visiting ' + teams[host.value].name : 'Your selected teams will appear here.';
        doc.getElementById('review-schedule').textContent = order.length ? order.map(fmt => label(fmt) + (fmt === 'ListA' ? ' · ' + doc.getElementById('tour-scheduled-overs').value + ' overs' : '') + ' (' + inputs[fmt].value + ' matches)').join(' → ') : 'No series selected.';
        doc.getElementById('review-total').textContent = order.reduce((sum, fmt) => sum + Number(inputs[fmt].value), 0) + ' matches · ' + order.length + ' series';
        // The required confirmation checkbox is deliberately unchecked here;
        // validate the schedule fields without making confirmation circular.
        confirm.required = false;
        const error = validation();
        confirm.required = true; confirm.disabled = Boolean(error);
        doc.getElementById('creation-validation').textContent = error || 'Review the schedule above, then confirm to enable creation.';
        doc.getElementById('team-validation').textContent = !namedTeams ? 'Choose both teams to see their shared formats.' :
            host.value === visitor.value ? 'Choose two different teams.' : Object.values(allowed).some(Boolean) ?
                'Only formats with ready squads on both teams are enabled.' : 'No playable formats in common. Complete a squad or choose another team.';
    }
    doc.getElementById('tour-scheduled-overs').addEventListener('change', update);
    [host, visitor].forEach(select => select.addEventListener('change', update));
    [name, ...Object.values(inputs)].forEach(input => input.addEventListener('input', update));
    confirm.addEventListener('change', () => {
        snapshot.value = confirm.checked ? currentSnapshot() : '';
        create.disabled = !confirm.checked || pending;
        doc.getElementById('creation-validation').textContent = confirm.checked ? 'Schedule confirmed. Ready to create.' : 'Confirm your schedule to continue.';
    });
    form.addEventListener('submit', event => {
        if (pending || validation() || !confirm.checked || snapshot.value !== currentSnapshot()) {
            event.preventDefault(); update(); form.reportValidity(); return;
        }
        pending = true; create.disabled = true; create.textContent = 'Creating tour…';
    });
    root.addEventListener('pageshow', () => { pending = false; create.textContent = 'Create Tour →'; update(); });
    update();
})(typeof window === 'undefined' ? globalThis : window);
