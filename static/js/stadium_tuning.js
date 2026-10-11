/* Form-owned settings: format switches never discard another format's values. */
(function () {
    'use strict';
    function setPath(settings, path, value) {
        let block = settings;
        for (const key of path.slice(0, -1)) block = block[key];
        block[path[path.length - 1]] = value;
    }
    function setBlend(settings, format, percent) {
        settings[format].blending = {pitch_weight: percent / 100, skill_weight: 1 - percent / 100};
    }
    if (typeof module !== 'undefined') module.exports = {setPath, setBlend};
    if (typeof document === 'undefined') return;
    const data = document.getElementById('tuning-data');
    if (!data) return;
    let settings = JSON.parse(data.textContent);
    const ranges = JSON.parse(document.getElementById('tuning-ranges').textContent);
    const form = document.getElementById('stadium-editor');
    const hidden = document.getElementById('format-settings');
    const draftKey = form?.dataset.draftKey;
    let recovered = false;
    const pristine = JSON.stringify(settings);
    const status = document.getElementById('tuning-draft-status');
    if (hidden) {
        try { settings = JSON.parse(hidden.value); } catch (_) { /* Server error retains safe values. */ }
        try {
            const draft = JSON.parse(localStorage.getItem(draftKey));
            if (draft && draft.baseline === pristine) {
                settings = draft.settings;
                for (const [name, value] of Object.entries(draft.fields)) {
                    const control = form.elements.namedItem(name);
                    if (control) control.value = value;
                }
                recovered = true;
                status.textContent = 'Recovered your unsaved stadium changes.';
                document.getElementById('tuning-discard-draft').hidden = false;
            }
        } catch (_) { /* Storage is optional. */ }
    }
    function hydrate() {
        for (const input of document.querySelectorAll('[data-tuning-path]')) {
            const path = JSON.parse(input.dataset.tuningPath);
            input.value = path.reduce((value, key) => value[key], settings);
        }
        for (const input of document.querySelectorAll('[data-blend]')) {
            const fmt = input.dataset.blend;
            input.value = settings[fmt].blending.pitch_weight * 100;
            document.getElementById('blend-' + fmt).textContent =
                `${(settings[fmt].blending.pitch_weight * 100).toFixed(2)}% pitch / ${(settings[fmt].blending.skill_weight * 100).toFixed(2)}% skill`;
        }
        if (hidden) hidden.value = JSON.stringify(settings);
    }
    function visibility() {
        for (const panel of document.querySelectorAll('.tuning-format-panel')) {
            panel.hidden = panel.dataset.format !== document.getElementById('tuning-format').value;
            const pitch = panel.querySelector('.tuning-pitch-select').value;
            for (const row of panel.querySelectorAll('.tuning-pitch-panel')) row.hidden = row.dataset.pitch !== pitch;
        }
    }
    function displayRanges() {
        for (const label of document.querySelectorAll('[data-phase-range]')) {
            const [fmt, phase] = label.dataset.phaseRange.split(':');
            const key = fmt === 'ListA' && document.getElementById('tuning-lista-length').value === '40' ? 'ListA40' : fmt;
            const [start, end] = ranges[key][phase];
            label.textContent = fmt === 'Hundred' ? `Balls ${start + 1}–${end}` : `Overs ${start / 6 + 1}–${end / 6}`;
        }
    }
    function saveDraft() {
        if (!hidden) return;
        hidden.value = JSON.stringify(settings);
        const fields = {};
        for (const control of form.elements) {
            if (control.name && !['csrf_token', 'revision', 'format_settings'].includes(control.name)) fields[control.name] = control.value;
        }
        try { localStorage.setItem(draftKey, JSON.stringify({baseline: pristine, settings, fields})); } catch (_) {}
        status.textContent = recovered ? 'Recovered draft updated locally. Save stadium to apply it.' : 'Unsaved changes saved locally.';
    }
    document.getElementById('tuning-format').addEventListener('change', visibility);
    for (const select of document.querySelectorAll('.tuning-pitch-select')) select.addEventListener('change', visibility);
    document.getElementById('tuning-lista-length')?.addEventListener('change', displayRanges);
    for (const input of document.querySelectorAll('[data-tuning-path]')) input.addEventListener('input', () => {
        if (!input.validity.valid || input.value === '') return;
        setPath(settings, JSON.parse(input.dataset.tuningPath), Number(input.value));
        saveDraft();
    });
    for (const input of document.querySelectorAll('[data-blend]')) input.addEventListener('input', () => {
        setBlend(settings, input.dataset.blend, Number(input.value));
        hydrate(); saveDraft();
    });
    form?.addEventListener('input', event => {
        if (event.target.name && !['csrf_token', 'revision', 'format_settings'].includes(event.target.name)) saveDraft();
    });
    form?.addEventListener('submit', event => {
        // Hidden format/pitch panels can contain invalid inputs; reveal the first one.
        const invalid = form.querySelector('input:invalid');
        if (invalid) {
            event.preventDefault();
            const panel = invalid.closest('.tuning-format-panel');
            if (panel) {
                document.getElementById('tuning-format').value = panel.dataset.format;
                const pitch = invalid.closest('.tuning-pitch-panel');
                if (pitch) panel.querySelector('.tuning-pitch-select').value = pitch.dataset.pitch;
                visibility();
                const details = invalid.closest('details');
                if (details) details.open = true;
            }
            invalid.focus(); invalid.reportValidity(); return;
        }
        hidden.value = JSON.stringify(settings);
    });
    if (form && hidden) form.noValidate = true;
    document.getElementById('tuning-discard-draft')?.addEventListener('click', () => {
        try { localStorage.removeItem(draftKey); } catch (_) {}
        location.reload();
    });
    hydrate(); visibility(); displayRanges();
})();
