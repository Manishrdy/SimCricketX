// Client-side pre-check for a team logo, shared by the team create/edit form
// and Manage Teams. It mirrors utils/team_logo.py so a bad file is caught
// before upload, but the server remains the authority.
(function () {
    'use strict';
    const MAX_BYTES = 1024 * 1024;
    const MIN_EDGE = 64;
    const MAX_EDGE = 2048;
    const SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
    const HINT = 'PNG only, up to 1 MB, 64 to 2048 px per side. ' +
        'Non-square images are padded, and animated PNGs keep their first frame.';

    // Resolves { ok: true, url } with an object URL for previewing, or
    // { ok: false, error }. The caller revokes the URL when done with it.
    async function check(file) {
        if (!file) return { ok: false, error: 'Choose a PNG file.' };
        if (file.size > MAX_BYTES) return { ok: false, error: 'Logo is too large. The limit is 1 MB.' };
        let head;
        try {
            head = new Uint8Array(await file.slice(0, 8).arrayBuffer());
        } catch (err) {
            return { ok: false, error: 'That file could not be read.' };
        }
        if (head.length < 8 || SIGNATURE.some((b, i) => head[i] !== b)) {
            return { ok: false, error: 'Logo must be a PNG image.' };
        }
        const url = URL.createObjectURL(file);
        const size = await new Promise(resolve => {
            const img = new Image();
            img.onload = () => resolve({ w: img.naturalWidth, h: img.naturalHeight });
            img.onerror = () => resolve(null);
            img.src = url;
        });
        let error = null;
        if (!size) error = 'That file could not be read as a PNG image.';
        else if (size.w > MAX_EDGE || size.h > MAX_EDGE) error = `Logo is too big. The limit is ${MAX_EDGE}x${MAX_EDGE} pixels.`;
        else if (size.w < MIN_EDGE || size.h < MIN_EDGE) error = `Logo is too small. It must be at least ${MIN_EDGE}x${MIN_EDGE} pixels.`;
        if (error) {
            URL.revokeObjectURL(url);
            return { ok: false, error };
        }
        return { ok: true, url };
    }

    window.scxTeamLogo = { check, HINT };
})();
