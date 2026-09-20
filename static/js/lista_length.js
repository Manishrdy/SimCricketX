window.withListALength = function (url) {
    const target = new URL(url, window.location.href);
    const length = new URLSearchParams(window.location.search).get('scheduled_overs');
    if (length) target.searchParams.set('scheduled_overs', length);
    else target.searchParams.delete('scheduled_overs');
    return target.pathname + target.search + target.hash;
};
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('a[href]').forEach(link => {
        const url = new URL(link.href, location.href);
        if (url.origin === location.origin && /^\/(statistics|player\/|team-stats|compare-players|head-to-head|my-matches)/.test(url.pathname)) {
            link.href = window.withListALength(link.href);
        }
    });
    document.querySelectorAll('form[method="get" i]').forEach(form => {
        const length = new URLSearchParams(location.search).get('scheduled_overs');
        if (length && !form.querySelector('[name="scheduled_overs"]')) {
            const input = document.createElement('input');
            input.type = 'hidden'; input.name = 'scheduled_overs'; input.value = length; form.append(input);
        }
    });
});

window.setListALength = function (length) {
    const target = new URL(location.href);
    if (length) target.searchParams.set('scheduled_overs', length);
    else target.searchParams.delete('scheduled_overs');
    target.searchParams.delete('page');
    if (location.pathname === '/compare-players' || location.pathname === '/head-to-head') {
        history.replaceState(null, '', target.toString());
        if (location.pathname === '/compare-players') document.getElementById('cmp-refresh')?.click();
        else {
            const first = document.getElementById('h2h-team1')?.value;
            const second = document.getElementById('h2h-team2')?.value;
            if (first && second && first !== second) document.getElementById('h2h-compare-btn')?.click();
        }
        document.querySelectorAll('a[href]').forEach(link => {
            if (new URL(link.href, location.href).origin === location.origin &&
                    /[?&]scheduled_overs=/.test(link.href)) link.href = window.withListALength(link.href);
        });
    } else location.href = target.toString();
};
