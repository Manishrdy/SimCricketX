// Bound raster allocation and avoid the extra base64 string used by data URLs.
(function () {
    'use strict';
    const MAX_PIXELS = 4_000_000;
    const MAX_SIDE = 8192;

    async function capturePng(element, options = {}) {
        if (typeof window.html2canvas !== 'function') {
            throw new Error('The image library did not load. Reload the page or use the text download.');
        }
        const width = Math.ceil(Math.max(element.scrollWidth, element.getBoundingClientRect().width));
        const height = Math.ceil(Math.max(element.scrollHeight, element.getBoundingClientRect().height));
        if (!Number.isFinite(width * height) || width <= 0 || height <= 0) {
            throw new Error('There is no scorecard content to capture.');
        }
        const scale = Math.min(options.scale || 2, Math.sqrt(MAX_PIXELS / (width * height)),
            MAX_SIDE / width, MAX_SIDE / height);
        let canvas;
        try {
            canvas = await window.html2canvas(element, {
                ...options, width, height, scale, useCORS: true, logging: false
            });
            return await new Promise((resolve, reject) => {
                canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('Image encoding failed.')), 'image/png');
            });
        } finally {
            // Release the backing bitmap promptly, including on encoding errors.
            if (canvas) { canvas.width = 0; canvas.height = 0; }
        }
    }

    function downloadBlob(blob, filename) {
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        let started = false;
        try {
            link.href = url;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            started = true;
        } finally {
            link.remove();
            // Give mobile browsers time to consume the URL after the click.
            if (started) window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
            else URL.revokeObjectURL(url);
        }
    }
    window.scxImageExport = { capturePng, downloadBlob };
})();
