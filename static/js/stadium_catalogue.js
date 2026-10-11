(() => {
    const collection = document.querySelector('.grounds-collection');
    if (!collection) return;
    let pending;
    let requestId = 0;
    history.replaceState({ ...history.state, stadiumCatalogue: true, scrollY: window.scrollY }, '', location.href);

    async function update(url, push = true) {
        pending?.abort();
        pending = new AbortController();
        const id = ++requestId;
        let scrollY = window.scrollY;
        collection.setAttribute('aria-busy', 'true');
        collection.querySelector('.grounds-update-status').classList.add('sr-only');
        collection.querySelector('.grounds-update-status').textContent = 'Updating stadiums…';
        try {
            const response = await fetch(url, { signal: pending.signal, headers: { 'X-Requested-With': 'XMLHttpRequest' } });
            if (!response.ok) throw new Error('Unable to load stadiums');
            const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
            const next = doc.querySelector('.grounds-collection');
            if (!next) throw new Error('Unable to load stadiums. Please refresh and sign in again.');
            if (id !== requestId) return;
            scrollY = window.scrollY;
            const focused = document.activeElement;
            if (push && new URL(url, location.href).href !== location.href) {
                history.replaceState({ ...history.state, scrollY }, '', location.href);
                history.pushState({ stadiumCatalogue: true, scrollY }, '', url);
            }
            const tabIndex = Array.from(collection.querySelectorAll('.grounds-tabs a')).indexOf(focused);
            const focusId = focused?.id;
            const wasSubmit = focused?.matches('.grounds-search button');
            collection.innerHTML = next.innerHTML;
            const target = tabIndex >= 0 ? collection.querySelectorAll('.grounds-tabs a')[tabIndex] : focusId ? document.getElementById(focusId) : wasSubmit ? collection.querySelector('.grounds-search button') : null;
            target?.focus({ preventScroll: true });
            window.scrollTo({ top: push ? scrollY : history.state?.scrollY ?? scrollY, behavior: 'instant' });
            collection.querySelector('.grounds-update-status').textContent = collection.querySelector('.grounds-results p').textContent.trim();
        } catch (error) {
            if (error.name !== 'AbortError' && id === requestId) {
                const status = collection.querySelector('.grounds-update-status');
                status.classList.remove('sr-only');
                status.textContent = 'Could not update stadiums. Please try again or refresh the page.';
            }
        } finally {
            if (id === requestId) collection.removeAttribute('aria-busy');
        }
    }
    collection.addEventListener('click', event => {
        const link = event.target.closest('.grounds-tabs a, .grounds-reset');
        if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        const url = new URL(link.href);
        url.hash = '';
        if (url.href !== location.href || collection.hasAttribute('aria-busy')) update(url.href);
    });
    collection.addEventListener('submit', async event => {
        if (event.target.matches('.ground-add-form')) {
            event.preventDefault();
            const form = event.target;
            const button = form.querySelector('button');
            const status = form.querySelector('.ground-add-status');
            if (button.disabled) return;
            button.disabled = true;
            status.classList.remove('sr-only');
            status.textContent = 'Adding stadium…';
            const fields = new FormData(form);
            try {
                const response = await fetch(form.dataset.adoptUrl, {method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': fields.get('csrf_token')},
                    body: JSON.stringify({revision: Number(fields.get('revision'))})});
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Unable to add stadium.');
                button.textContent = 'Added to My stadiums';
                status.textContent = 'Stadium added to My stadiums.';
                await update(location.href, false);
                collection.querySelector('.grounds-update-status').textContent = 'Stadium added to My stadiums.';
            } catch (error) {
                status.textContent = error.message || 'Unable to add stadium. Please try again.';
                button.disabled = false;
            }
            return;
        }
        if (!event.target.matches('.grounds-search')) return;
        event.preventDefault();
        const url = new URL(location.pathname, location.origin);
        url.search = new URLSearchParams(new FormData(event.target)).toString();
        update(url.href);
    });
    window.addEventListener('popstate', () => update(location.href, false));
})();
