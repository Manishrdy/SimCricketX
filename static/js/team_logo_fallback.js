// Team logo display with a text fallback.
//
// Markup contract: a container marked [data-logo-slot] holds the short-code
// text and, when the team has a logo, an <img data-logo-fallback>. The
// container carries the "has-logo" class while the logo is usable; CSS uses
// it to hide the text. If the image fails to load, the image is removed and
// "has-logo" is dropped, so the original short-code display comes back.
(function () {
    function fail(img) {
        const slot = img.closest('[data-logo-slot]');
        img.remove();
        if (slot) slot.classList.remove('has-logo');
    }

    function watch(img) {
        if (!img || img.dataset.logoWatched) return;
        img.dataset.logoWatched = '1';
        if (!img.getAttribute('src')) { fail(img); return; }
        img.addEventListener('error', () => fail(img), { once: true });
        // The error may already have fired before this script ran.
        if (img.complete && img.naturalWidth === 0) fail(img);
    }

    function scan(root) {
        (root || document).querySelectorAll('img[data-logo-fallback]').forEach(watch);
    }

    window.scxLogoFallback = { watch, scan };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => scan());
    } else {
        scan();
    }
})();
