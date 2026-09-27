document.querySelector('.workspace-tabs [aria-current="page"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });

/* Shared progressive enhancement for existing CSRF-protected admin actions. */
document.querySelectorAll('form.workspace-action').forEach(form => {
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
        if (form.dataset.confirmFinal && !window.confirm(form.dataset.confirmFinal)) return;
        const buttons = Array.from(form.querySelectorAll('button'));
        if (buttons.some(button => button.disabled)) return;
        const body = new FormData(form);
        buttons.forEach(button => { button.disabled = true; });
        let feedback = form.querySelector('.workspace-action-feedback');
        if (!feedback) {
            feedback = document.createElement('p');
            feedback.className = 'workspace-action-feedback';
            feedback.setAttribute('role', 'status');
            form.appendChild(feedback);
        }
        feedback.textContent = 'Working…';
        try {
            const response = await fetch(form.action, { method: 'POST', body, credentials: 'same-origin' });
            if (response.redirected) { window.location.assign(response.url); return; }
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'The action failed. Please retry.');
            feedback.textContent = data.message || 'Updated successfully.';
            let destination = form.dataset.redirect || window.location.href;
            if (form.dataset.newEmailUrl) destination = form.dataset.newEmailUrl.replace('__EMAIL__', encodeURIComponent(body.get('new_email').trim().toLowerCase()));
            window.setTimeout(() => window.location.assign(destination), 700);
        } catch (error) {
            feedback.textContent = error.message || 'Network error. Please retry.';
            buttons.forEach(button => { button.disabled = false; });
        }
    });
});
