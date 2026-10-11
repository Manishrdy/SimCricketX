/* Each card owns its selection; switching formats never opens the team editor. */
document.querySelectorAll('.format-tabs').forEach(tablist => {
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
    function selectTab(selected) {
        tabs.forEach(tab => {
            const active = tab === selected;
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
            document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
        });
    }
    tablist.addEventListener('click', event => {
        const tab = event.target.closest('[role="tab"]');
        if (tabs.includes(tab)) selectTab(tab);
    });
    tablist.addEventListener('keydown', event => {
        const index = tabs.indexOf(event.target);
        if (index < 0) return;
        let next;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabs.length - 1;
        else return;
        event.preventDefault();
        selectTab(tabs[next]);
        tabs[next].focus();
    });
});
