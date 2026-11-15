/* The presentation is a delivery gate, shared by automatic and manual tosses. */
let prematchPresentationOpen = false;
function showPrematchPresentation(toss) {
    const overlay = document.getElementById('prematch-overlay');
    const button = document.getElementById('prematch-close');
    if (!overlay || !button || prematchPresentationOpen) return;
    prematchPresentationOpen = true;
    const home = matchData.team_home.split('_')[0];
    const homeBats = (toss.toss_winner === home) === (toss.toss_decision === 'Bat');
    document.getElementById('pm-toss-result').textContent = `${toss.toss_winner} won the toss & elected to ${toss.toss_decision.toLowerCase()} first`;
    document.getElementById('pm-order-home').textContent = homeBats ? 'BATTING FIRST' : 'BOWLING FIRST';
    document.getElementById('pm-order-away').textContent = homeBats ? 'BOWLING FIRST' : 'BATTING FIRST';
    overlay.style.display = 'flex';
    button.disabled = false;
    button.focus();
    const capture = capturePrematchPresentation();
    let closing = false;
    button.onclick = async () => {
        if (closing) return;
        closing = true;
        button.disabled = true;
        button.textContent = 'Preparing match…';
        await capture;
        overlay.style.display = 'none';
        prematchPresentationOpen = false;
        startMatch();
    };
}

async function capturePrematchPresentation() {
    const status = document.getElementById('pm-save-status');
    try {
        if (typeof html2canvas !== 'function') throw new Error('Image renderer unavailable');
        await new Promise(resolve => requestAnimationFrame(resolve));
        const panel = document.getElementById('prematch-card');
        const canvas = await withScorecardTimeout(html2canvas(panel, {
            backgroundColor: null, scale: 2, useCORS: true, logging: false,
            imageTimeout: 5000,
            onclone: (_doc, clonedPanel) => {
                const shell = clonedPanel.parentElement;
                shell.style.maxHeight = 'none';
                shell.style.overflow = 'visible';
                shell.scrollTop = 0;
                shell.parentElement.style.height = 'auto';
                shell.parentElement.style.overflow = 'visible';
                shell.parentElement.scrollTop = 0;
            },
            ignoreElements: element => !['STYLE', 'LINK', 'HEAD'].includes(element.tagName)
                && !element.contains(panel) && !panel.contains(element),
        }), 'Pre-match rendering');
        const blob = await withScorecardTimeout(new Promise(resolve => canvas.toBlob(resolve, 'image/png')), 'Pre-match encoding');
        if (!blob) throw new Error('Image encoding failed');
        await uploadNamedScorecardImage(blob, 'prematch_presentation');
        status.textContent = 'Match presentation saved. It will be included in your match ZIP.';
    } catch (error) {
        status.textContent = 'Presentation image could not be saved. You can still start the match.';
        appendLog('[system_error] Pre-match presentation image could not be saved.', 'error');
    }
}
