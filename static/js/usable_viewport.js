// Keyboards can resize/pan the visual viewport while layout viewport stays put.
(function () {
    'use strict';
    const viewport = window.visualViewport;
    let frame = null;
    function update() {
        frame = null;
        const height = viewport ? viewport.height : window.innerHeight;
        const width = viewport ? viewport.width : window.innerWidth;
        const top = viewport ? viewport.offsetTop : 0;
        const left = viewport ? viewport.offsetLeft : 0;
        const bottom = Math.max(0, window.innerHeight - top - height);
        const right = Math.max(0, window.innerWidth - left - width);
        const root = document.documentElement;
        for (const [key, value] of Object.entries({ height, width, top, left, bottom, right })) {
            root.style.setProperty('--scx-vv-' + key, value + 'px');
        }
        root.toggleAttribute('data-compact-viewport', height < 500 || bottom > 120);
    }
    function schedule() {
        if (frame === null) frame = window.requestAnimationFrame(update);
    }
    if (viewport) {
        viewport.addEventListener('resize', schedule);
        viewport.addEventListener('scroll', schedule);
    }
    window.addEventListener('resize', schedule);
    update();
})();
