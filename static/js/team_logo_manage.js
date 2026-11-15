// Manage Teams: the logo dialog (choose, preview, save, remove) for a team card.
(function () {
    const modal = document.getElementById('logoModal');
    if (!modal || !window.scxTeamLogo) return;
    const CSRF = modal.getAttribute('data-csrf');
    const preview = document.getElementById('logo-preview');
    const fileInput = document.getElementById('logo-file');
    const chooseBtn = document.getElementById('logo-choose');
    const removeBtn = document.getElementById('logo-remove');
    const saveBtn = document.getElementById('logo-save');
    const errorEl = document.getElementById('logo-error');
    let card = null, picked = null, pickedUrl = null, busy = false;

    document.getElementById('logo-hint').textContent = window.scxTeamLogo.HINT;
    document.querySelectorAll('[data-team-logo]').forEach(img => {
        img.style.borderColor = img.getAttribute('data-ring') || '';
    });

    const cardLogo = () => card.querySelector('[data-team-logo]');
    const hasLogo = () => !cardLogo().hidden;

    function showPreview(url) {
        preview.textContent = '';
        if (url) {
            const img = document.createElement('img');
            img.src = url;
            img.alt = '';
            preview.appendChild(img);
        } else {
            const icon = document.createElement('i');
            icon.className = 'fa-solid fa-image';
            preview.appendChild(icon);
        }
    }

    function clearPicked() {
        if (pickedUrl) URL.revokeObjectURL(pickedUrl);
        picked = pickedUrl = null;
        fileInput.value = '';
    }

    function refresh() {
        showPreview(pickedUrl || (hasLogo() ? cardLogo().src : null));
        chooseBtn.textContent = hasLogo() || picked ? 'Choose another PNG' : 'Choose PNG';
        removeBtn.hidden = !hasLogo();
        saveBtn.disabled = busy || !picked;
        chooseBtn.disabled = removeBtn.disabled = busy;
    }

    function setCardLogo(url) {
        const img = cardLogo();
        const dot = card.querySelector('.team-color-dot');
        if (url) img.src = url; else img.removeAttribute('src');
        img.hidden = !url;
        dot.hidden = !!url;
    }

    function open(button) {
        card = button.closest('.glass-card');
        document.getElementById('logo-title').textContent = 'Logo for ' + button.getAttribute('data-team-name');
        errorEl.textContent = '';
        clearPicked();
        refresh();
        modal.classList.add('active');
        document.body.style.overflow = 'hidden';
    }

    function close() {
        if (busy) return;
        modal.classList.remove('active');
        document.body.style.overflow = '';
        clearPicked();
    }

    async function send(method, body) {
        busy = true;
        errorEl.textContent = '';
        refresh();
        try {
            const res = await fetch('/team/' + card.getAttribute('data-team-id') + '/logo', {
                method, body, headers: { 'X-CSRFToken': CSRF }, credentials: 'same-origin',
            });
            let data = {};
            try { data = await res.json(); } catch (err) { /* non-JSON error page */ }
            if (!res.ok) throw new Error(data.error || (res.status === 429
                ? 'Too many logo changes. Wait a minute and try again.'
                : 'The logo could not be saved. Please try again.'));
            return data;
        } finally {
            busy = false;
        }
    }

    document.querySelectorAll('[data-logo-open]').forEach(btn => {
        btn.addEventListener('click', () => open(btn));
    });
    modal.querySelector('[data-logo-close]').addEventListener('click', close);
    modal.addEventListener('click', e => { if (e.target === modal) close(); });
    chooseBtn.addEventListener('click', () => fileInput.click());

    fileInput.addEventListener('change', async () => {
        const file = fileInput.files[0];
        if (!file) return;
        const result = await window.scxTeamLogo.check(file);
        clearPicked();
        if (result.ok) {
            picked = file;
            pickedUrl = result.url;
            errorEl.textContent = '';
        } else {
            errorEl.textContent = result.error;
        }
        refresh();
    });

    saveBtn.addEventListener('click', async () => {
        if (!picked || busy) return;
        const body = new FormData();
        body.append('logo', picked, picked.name);
        try {
            const data = await send('POST', body);
            setCardLogo(data.url);
            close();
        } catch (err) {
            errorEl.textContent = err.message;
            refresh();
        }
    });

    removeBtn.addEventListener('click', async () => {
        if (busy) return;
        try {
            await send('DELETE');
            setCardLogo(null);
            clearPicked();
        } catch (err) {
            errorEl.textContent = err.message;
        }
        refresh();
    });
})();
