(() => {
    const panel = document.getElementById('tour-settings');
    const opener = document.getElementById('td-manage-open');
    let previousOverflow = '';
    opener.addEventListener('click', () => {
        previousOverflow = document.body.style.overflow;
        panel.showModal();
        document.body.style.overflow = 'hidden';
        document.getElementById('td-manage-close').focus();
    });
    ['td-manage-close', 'td-manage-done'].forEach(id => {
        document.getElementById(id).addEventListener('click', () => panel.close());
    });
    panel.addEventListener('close', () => {
        document.body.style.overflow = previousOverflow;
        opener.focus();
    });
    panel.addEventListener('click', event => {
        if (event.target !== panel) return;
        const bounds = panel.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right ||
            event.clientY < bounds.top || event.clientY > bounds.bottom) panel.close();
    });
    const list = document.getElementById('td-order-list');
    if (!list) return;
    function refresh() {
        const rows = [...list.children];
        rows.forEach((row, index) => row.querySelectorAll('[data-direction]').forEach(button => {
            const neighbor = rows[index + Number(button.dataset.direction)];
            button.disabled = !neighbor || neighbor.dataset.fixed === 'true';
        }));
    }
    list.addEventListener('click', event => {
        const button = event.target.closest('[data-direction]');
        if (!button || button.disabled) return;
        const row = button.closest('li'), direction = Number(button.dataset.direction);
        const neighbor = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
        if (!neighbor || neighbor.dataset.fixed === 'true') return;
        if (direction < 0) list.insertBefore(row, neighbor); else list.insertBefore(neighbor, row);
        document.getElementById('td-order-confirm').checked = false;
        refresh();
        (row.querySelector('button:not(:disabled)') || row).focus();
    });
    refresh();
})();
