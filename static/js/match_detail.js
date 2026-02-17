/**
 * SimCricketX Match Detail Logic
 * Handles match simulation, impact players, scorecards, and UI updates.
 * Expects 'matchData' and 'html2canvas' to be available in the global scope.
 */

// --- Global State ---
let impactPlayerState = {
    home: {
        originalXI: [], originalSubs: [], currentXI: [], currentSubs: [], swapped: false, swapInfo: null
    },
    away: {
        originalXI: [], originalSubs: [], currentXI: [], currentSubs: [], swapped: false, swapInfo: null
    },
    reorderingEnabled: false
};

let matchOver = false;
let currentInningsNumber = null;
let delay = 300; // default 1x pace
const MIN_BALL_DELAY_MS = 200; // Enforce 0.2s minimum gap between balls
let logLineCount = 1; // Track line numbers for commentary display
let isFinalScoreboard = false;
let simTimerId = null; // F4: track simulation timer to prevent overlapping loops
let archiveSaved = false; // F7: guard against double archive saves
let simulationMode = (typeof matchData !== 'undefined' && matchData.simulation_mode) ? matchData.simulation_mode : 'auto';
let pendingManualDecision = null;
let pendingDecisionSelection = null;
let decisionModalVisible = false;

// Global variable to store first innings scorecard image
let firstInningsImageBlob = null;

// Dashboard data stores
let ballHistory = [];          // Array of ball_data objects for current innings
let overRuns = [];             // Runs per completed over [8, 12, 5, ...]
let currentOverBalls = [];     // Balls in the current over (for timeline)
let innings1Data = null;       // Saved {ballHistory, overRuns} from 1st innings for worm overlay
let dashboardActive = false;   // Which view is showing
let currentMainView = 'commentary'; // 'commentary' | 'matchcenter'

// Hard-lock code window height to available space in main panel.
function syncCodeWindowHeight() {
    const mainPanel = document.querySelector('.main-panel');
    const scoreBanner = document.querySelector('.score-banner');
    const controlsBar = document.querySelector('.controls-bar');
    const codeWindow = document.querySelector('.code-window');

    if (!mainPanel || !scoreBanner || !controlsBar || !codeWindow) return;

    const panelHeight = mainPanel.clientHeight;
    if (!panelHeight) return;

    const panelStyle = getComputedStyle(mainPanel);
    const rowGap = parseFloat(panelStyle.rowGap || panelStyle.gap || '0') || 0;
    const available = panelHeight - scoreBanner.offsetHeight - controlsBar.offsetHeight - (rowGap * 2);

    if (available > 120) {
        const px = `${Math.floor(available)}px`;
        codeWindow.style.height = px;
        codeWindow.style.maxHeight = px;
    }
}

// C1: HTML escaping utility to prevent XSS via innerHTML
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}


// Dashboard: track over completions for Manhattan chart
function updateCurrentOverBalls(bd) {
    currentOverBalls.push(bd);
    if (ballHistory.length >= 2) {
        const prev = ballHistory[ballHistory.length - 2];
        if (bd.over > prev.over) {
            // Previous over completed — sum its runs
            const prevOverBalls = ballHistory.filter(b => b.over === prev.over);
            overRuns[prev.over] = prevOverBalls.reduce((sum, b) => sum + b.runs, 0);
            currentOverBalls = [bd];
        }
    }
}

// --- Initialization ---

// Track this-over ball results for the banner strip
let thisOverBallResults = [];

document.addEventListener('DOMContentLoaded', () => {
    // --- Pill toggle: Simulation Mode (Auto / Manual) ---
    const pillAuto = document.getElementById('pill-auto');
    const pillManual = document.getElementById('pill-manual');
    const modeToggle = document.getElementById('simulation-mode-toggle');

    function syncModePills() {
        if (!pillAuto || !pillManual) return;
        pillAuto.classList.toggle('active', simulationMode === 'auto');
        pillManual.classList.toggle('active', simulationMode === 'manual');
        if (modeToggle) modeToggle.checked = simulationMode === 'manual';
    }

    if (pillAuto) pillAuto.addEventListener('click', async () => {
        if (simulationMode === 'auto') return;
        const ok = await setSimulationMode('auto', true);
        if (ok) syncModePills();
    });
    if (pillManual) pillManual.addEventListener('click', async () => {
        if (simulationMode === 'manual') return;
        const ok = await setSimulationMode('manual', true);
        if (ok) syncModePills();
    });
    syncModePills();

    // --- Pill toggle: Commentary / Match Center ---
    const pillCommentary = document.getElementById('pill-commentary');
    const pillMatchCenter = document.getElementById('pill-matchcenter');
    const viewToggle = document.getElementById('view-toggle');

    function setView(view) {
        currentMainView = view;
        dashboardActive = view === 'matchcenter';

        const panes = document.querySelectorAll('.view-pane[data-view-pane]');
        panes.forEach((pane) => {
            pane.classList.toggle('is-active', pane.dataset.viewPane === view);
        });

        if (pillCommentary) pillCommentary.classList.toggle('active', view === 'commentary');
        if (pillMatchCenter) pillMatchCenter.classList.toggle('active', view === 'matchcenter');
        if (viewToggle) viewToggle.checked = view === 'matchcenter';

        if (view === 'matchcenter' && typeof refreshDashboard === 'function') {
            refreshDashboard(ballHistory, overRuns, innings1Data);
        }
        requestAnimationFrame(syncCodeWindowHeight);
    }

    if (pillCommentary) pillCommentary.addEventListener('click', () => setView('commentary'));
    if (pillMatchCenter) pillMatchCenter.addEventListener('click', () => setView('matchcenter'));

    // Keep hidden checkbox in sync (for any legacy code)
    if (viewToggle) {
        viewToggle.addEventListener('change', () => setView(viewToggle.checked ? 'matchcenter' : 'commentary'));
    }
    if (modeToggle) {
        modeToggle.addEventListener('change', async () => {
            const requestedMode = modeToggle.checked ? 'manual' : 'auto';
            const ok = await setSimulationMode(requestedMode, true);
            if (ok) syncModePills();
            else if (modeToggle) modeToggle.checked = simulationMode === 'manual';
        });
    }

    const decisionSubmitBtn = document.getElementById('decision-submit-btn');
    if (decisionSubmitBtn) decisionSubmitBtn.addEventListener('click', submitManualDecision);
    const decisionCloseBtn = document.getElementById('decision-close-btn');
    if (decisionCloseBtn) {
        decisionCloseBtn.addEventListener('click', closeDecisionModalOnly);
    }
    const decisionAutoBtn = document.getElementById('decision-auto-btn');
    if (decisionAutoBtn) decisionAutoBtn.addEventListener('click', async () => {
        await setSimulationMode('auto', true);
    });
    const resumeDecisionBtn = document.getElementById('resume-decision-btn');
    if (resumeDecisionBtn) resumeDecisionBtn.addEventListener('click', () => showDecisionModal());
    updateDecisionResumeButton();

    // Spin Toss Button
    const spinBtn = document.getElementById('spin-toss');
    if (spinBtn) {
        spinBtn.onclick = spinTossAndStartMatch;
    }

    // Reset Swap Buttons
    const homeReset = document.getElementById('home-reset-btn');
    if (homeReset) homeReset.addEventListener('click', () => resetTeamSwap('home'));

    const awayReset = document.getElementById('away-reset-btn');
    if (awayReset) awayReset.addEventListener('click', () => resetTeamSwap('away'));

    // Confirm Swap Button
    const confirmBtn = document.getElementById('confirm-swaps');
    if (confirmBtn) confirmBtn.addEventListener('click', handleConfirmSwaps);

    // Initialize Impact Player State if matchData is present
    if (typeof matchData !== 'undefined') {
        initializeImpactPlayerState();
    }

    setView('commentary');

    // Initial and responsive lock for commentary panel height.
    requestAnimationFrame(syncCodeWindowHeight);
    setTimeout(syncCodeWindowHeight, 50);
    window.addEventListener('resize', syncCodeWindowHeight);
});


// --- Impact Player Logic ---

function showImpactPlayerModal() {
    const modal = document.getElementById('impact-modal');
    if (!modal) return;

    const homeTeamName = matchData.team_home.split('_')[0];
    const awayTeamName = matchData.team_away.split('_')[0];

    const homeTitleIdx = document.getElementById('home-team-title');
    if (homeTitleIdx) homeTitleIdx.textContent = homeTeamName;

    const awayTitleIdx = document.getElementById('away-team-title');
    if (awayTitleIdx) awayTitleIdx.textContent = awayTeamName;

    // Reset state to current matchData
    initializeImpactPlayerState();
    renderTeam('home');
    renderTeam('away');

    modal.style.display = 'block';
}

function initializeImpactPlayerState() {
    // Deep copy matchData to local state
    impactPlayerState.home.originalXI = JSON.parse(JSON.stringify(matchData.playing_xi.home));
    impactPlayerState.home.originalSubs = JSON.parse(JSON.stringify(matchData.substitutes.home));
    impactPlayerState.home.currentXI = JSON.parse(JSON.stringify(matchData.playing_xi.home));
    impactPlayerState.home.currentSubs = JSON.parse(JSON.stringify(matchData.substitutes.home));
    impactPlayerState.home.swapped = false;
    impactPlayerState.home.swapInfo = null;

    impactPlayerState.away.originalXI = JSON.parse(JSON.stringify(matchData.playing_xi.away));
    impactPlayerState.away.originalSubs = JSON.parse(JSON.stringify(matchData.substitutes.away));
    impactPlayerState.away.currentXI = JSON.parse(JSON.stringify(matchData.playing_xi.away));
    impactPlayerState.away.currentSubs = JSON.parse(JSON.stringify(matchData.substitutes.away));
    impactPlayerState.away.swapped = false;
    impactPlayerState.away.swapInfo = null;

    impactPlayerState.reorderingEnabled = false;
}

function renderTeam(team) {
    const playingList = document.getElementById(`${team}-playing-list`);
    const subsList = document.getElementById(`${team}-subs-list`);

    if (!playingList || !subsList) return;

    playingList.innerHTML = '';
    subsList.innerHTML = '';

    // Render Playing XI
    impactPlayerState[team].currentXI.forEach((player, index) => {
        const card = createPlayerCard(player, team, 'xi', index);
        playingList.appendChild(card);
    });

    // Render Substitutes
    impactPlayerState[team].currentSubs.forEach((player, index) => {
        const card = createPlayerCard(player, team, 'sub', index);
        subsList.appendChild(card);
    });

    updateSwapStatus(team);
    setupDragAndDrop(team);
}

function createPlayerCard(player, team, type, index) {
    const card = document.createElement('div');
    card.className = 'player-card';
    card.draggable = true;
    card.dataset.team = team;
    card.dataset.type = type;
    card.dataset.index = index;
    card.dataset.playerName = player.name;

    const state = impactPlayerState[team];
    const isSwapped = state.swapInfo &&
        ((type === 'xi' && player.name === state.swapInfo.inPlayer) ||
            (type === 'sub' && player.name === state.swapInfo.outPlayer));

    if (isSwapped) {
        card.classList.add('swapped');
    }

    const isOriginalOut = type === 'sub' &&
        state.swapInfo &&
        player.name === state.swapInfo.outPlayer;

    if (isOriginalOut) {
        card.classList.add('original');
    }

    card.innerHTML = `
        <div class="player-info">
            <div>
                <div class="player-name">${escapeHtml(player.name)}</div>
                <div class="player-role">${escapeHtml(player.role)}</div>
            </div>
            ${impactPlayerState.reorderingEnabled && type === 'xi' ?
            '<div style="font-size: 0.8rem; color: #666;">↕️ Drag to reorder</div>' : ''}
        </div>
        ${isSwapped && type === 'xi' ? '<div class="swap-badge">IMPACT</div>' : ''}
    `;

    return card;
}

function updateSwapStatus(team) {
    const statusEl = document.getElementById(`${team}-swap-status`);
    const state = impactPlayerState[team];

    if (state.swapped && state.swapInfo) {
        statusEl.textContent = `Swapped: ${state.swapInfo.outPlayer} ↔ ${state.swapInfo.inPlayer}`;
        statusEl.className = 'team-swap-status active';
    } else {
        statusEl.textContent = 'No swap made';
        statusEl.className = 'team-swap-status inactive';
    }
}

function resetTeamSwap(team) {
    const state = impactPlayerState[team];

    state.currentXI = JSON.parse(JSON.stringify(state.originalXI));
    state.currentSubs = JSON.parse(JSON.stringify(state.originalSubs));
    state.swapped = false;
    state.swapInfo = null;

    renderTeam(team);
    const btn = document.getElementById(`${team}-reset-btn`);
    if (btn) btn.disabled = true;
}


// --- Drag & Drop Logic ---

function setupDragAndDrop(team) {
    const playingList = document.getElementById(`${team}-playing-list`);
    const subsList = document.getElementById(`${team}-subs-list`);

    // Clone to remove old listeners
    const newPlayingList = playingList.cloneNode(true);
    const newSubsList = subsList.cloneNode(true);
    playingList.parentNode.replaceChild(newPlayingList, playingList);
    subsList.parentNode.replaceChild(newSubsList, subsList);

    // Re-select
    const updatedPlayingList = document.getElementById(`${team}-playing-list`);
    const updatedSubsList = document.getElementById(`${team}-subs-list`);

    const addListeners = (list) => {
        list.querySelectorAll('.player-card').forEach(card => {
            card.addEventListener('dragstart', handleDragStart);
            card.addEventListener('dragend', handleDragEnd);
        });

        list.addEventListener('dragover', handleDragOver);
        list.addEventListener('drop', handleDrop);
        list.addEventListener('dragenter', handleDragEnter);
        list.addEventListener('dragleave', handleDragLeave);
    };

    addListeners(updatedPlayingList);
    addListeners(updatedSubsList);
}

function handleDragStart(e) {
    e.target.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    const data = {
        team: e.target.dataset.team,
        type: e.target.dataset.type,
        index: parseInt(e.target.dataset.index),
        playerName: e.target.dataset.playerName
    };
    e.dataTransfer.setData('text/plain', JSON.stringify(data));
}

function handleDragEnd(e) {
    e.target.classList.remove('dragging');
}

function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
}

function handleDragEnter(e) {
    if (e.target.classList.contains('player-list')) {
        e.target.style.background = 'rgba(74, 144, 226, 0.1)';
    }
}

function handleDragLeave(e) {
    if (e.target.classList.contains('player-list')) {
        e.target.style.background = '';
    }
}

function handleDrop(e) {
    e.preventDefault();
    e.target.style.background = '';

    const rawData = e.dataTransfer.getData('text/plain');
    if (!rawData) return;

    const data = JSON.parse(rawData);
    const dropZone = e.target.closest('.player-list');

    if (!dropZone) return;

    const dropZoneId = dropZone.id;
    const dropTeam = dropZoneId.includes('home') ? 'home' : 'away';
    const dropType = dropZoneId.includes('playing') ? 'xi' : 'sub';

    if (data.team !== dropTeam) return;

    // Reordering within XI
    if (impactPlayerState.reorderingEnabled && data.type === 'xi' && dropType === 'xi') {
        performReorder(data.team, data.index, e, dropZone);
        return;
    }

    // Impact Player Swap
    if (!impactPlayerState.reorderingEnabled && data.type !== dropType) {
        performSwap(data.team, data.type, data.index, dropType, e, dropZone);
    }
}

function performSwap(team, fromType, fromIndex, toType, dropEvent, dropZone) {
    const state = impactPlayerState[team];

    if (state.swapped) resetTeamSwap(team);

    const fromList = fromType === 'xi' ? state.currentXI : state.currentSubs;
    const toList = toType === 'xi' ? state.currentXI : state.currentSubs;
    const draggedPlayer = fromList[fromIndex];

    const afterElement = getDragAfterElement(dropZone, dropEvent.clientY);
    let swapWithIndex = afterElement ?
        [...dropZone.querySelectorAll('.player-card')].indexOf(afterElement) - 1 :
        toList.length - 1;

    swapWithIndex = Math.max(0, Math.min(swapWithIndex, toList.length - 1));

    if (toList.length > 0) {
        const swapWithPlayer = toList[swapWithIndex];

        fromList[fromIndex] = swapWithPlayer;
        toList[swapWithIndex] = draggedPlayer;

        state.swapped = true;
        state.swapInfo = {
            outPlayer: fromType === 'xi' ? draggedPlayer.name : swapWithPlayer.name,
            inPlayer: fromType === 'sub' ? draggedPlayer.name : swapWithPlayer.name,
            outIndex: fromType === 'xi' ? fromIndex : swapWithIndex,
            inIndex: fromType === 'sub' ? fromIndex : swapWithIndex
        };

        renderTeam(team);
        const btn = document.getElementById(`${team}-reset-btn`);
        if (btn) btn.disabled = false;
    }
}

function performReorder(team, fromIndex, dropEvent, dropZone) {
    const state = impactPlayerState[team];
    const afterElement = getDragAfterElement(dropZone, dropEvent.clientY);

    let toIndex = afterElement ?
        [...dropZone.querySelectorAll('.player-card:not(.dragging)')].indexOf(afterElement) :
        state.currentXI.length;

    if (toIndex > fromIndex) toIndex--;

    const player = state.currentXI.splice(fromIndex, 1)[0];
    state.currentXI.splice(toIndex, 0, player);

    renderTeam(team);
}

function getDragAfterElement(container, y) {
    const draggableElements = [...container.querySelectorAll('.player-card:not(.dragging)')];

    return draggableElements.reduce((closest, child) => {
        const box = child.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) {
            return { offset: offset, element: child };
        } else {
            return closest;
        }
    }, { offset: Number.NEGATIVE_INFINITY }).element;
}


// --- Match Flow ---

async function handleConfirmSwaps() {
    // Check Home Swap
    let homeSwap = null;
    if (impactPlayerState.home.swapped && impactPlayerState.home.swapInfo) {
        const hState = impactPlayerState.home;
        homeSwap = {
            out_player_index: hState.originalXI.findIndex(p => p.name === hState.swapInfo.outPlayer),
            in_player_index: hState.originalSubs.findIndex(p => p.name === hState.swapInfo.inPlayer)
        };
    }

    // Check Away Swap
    let awaySwap = null;
    if (impactPlayerState.away.swapped && impactPlayerState.away.swapInfo) {
        const aState = impactPlayerState.away;
        awaySwap = {
            out_player_index: aState.originalXI.findIndex(p => p.name === aState.swapInfo.outPlayer),
            in_player_index: aState.originalSubs.findIndex(p => p.name === aState.swapInfo.inPlayer)
        };
    }

    try {
        if (homeSwap || awaySwap) {
            const res = await fetch(`/match/${matchData.match_id}/impact-player-swap`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ home_swap: homeSwap, away_swap: awaySwap })
            });

            if (res.ok) {
                const result = await res.json();
                matchData = result.updated_match_data;
                enableReorderingMode();
            } else {
                alert('Failed to save swaps.');
            }
        } else {
            // No swaps, verify if we proceed
            matchData.impact_players_swapped = true;
            document.getElementById('impact-modal').style.display = 'none';
            scheduleNextBall(delay);
        }
    } catch (err) {
        console.error('Error saving swaps:', err);
        alert('An error occurred.');
    }
}

function enableReorderingMode() {
    impactPlayerState.reorderingEnabled = true;

    // UI Updates
    document.querySelector('.impact-header h2').textContent = 'Adjust Batting Order';
    document.querySelector('.impact-header p').textContent = 'Drag players within Playing XI to set the batting order.';

    const confirmBtn = document.getElementById('confirm-swaps');
    confirmBtn.textContent = 'Finalize Order & Start Match';

    // Replace button to clear listeners
    const newBtn = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newBtn, confirmBtn);

    newBtn.addEventListener('click', async () => {
        const finalLineups = {
            home_final_xi: impactPlayerState.home.currentXI,
            away_final_xi: impactPlayerState.away.currentXI
        };

        try {
            const res = await fetch(`/match/${matchData.match_id}/update-final-lineups`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(finalLineups)
            });

            if (res.ok) {
                document.getElementById('impact-modal').style.display = 'none';
                scheduleNextBall(delay);
            } else {
                alert('Failed to save final lineups.');
            }
        } catch (err) {
            console.error(err);
            alert('Error sending lineups.');
        }
    });

    renderTeam('home');
    renderTeam('away');
}

function hideDecisionModal() {
    const overlay = document.getElementById('decision-overlay');
    if (!overlay) return;
    overlay.style.display = 'none';
    decisionModalVisible = false;
    const autoBtn = document.getElementById('decision-auto-btn');
    if (autoBtn) autoBtn.style.display = simulationMode === 'manual' ? 'inline-flex' : 'none';
    updateDecisionResumeButton();
}

function closeDecisionModalOnly() {
    hideDecisionModal();
    if (pendingManualDecision) {
        appendLog('[MANUAL] Selection window closed. Use "Resume Selection" to continue.', 'comment');
    }
}

function clearPendingDecisionState() {
    pendingManualDecision = null;
    pendingDecisionSelection = null;
    updateDecisionResumeButton();
}

function updateDecisionResumeButton() {
    const btn = document.getElementById('resume-decision-btn');
    if (!btn) return;
    const shouldShow = simulationMode === 'manual' && !!pendingManualDecision && !decisionModalVisible;
    btn.style.display = shouldShow ? 'inline-flex' : 'none';
}

async function setSimulationMode(requestedMode, autoContinue = false) {
    try {
        const res = await fetch(`${window.location.pathname}/set-simulation-mode`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: requestedMode })
        });
        const data = await res.json();
        if (!res.ok || !data.success) throw new Error(data.error || 'Failed to set simulation mode');

        simulationMode = data.mode;
        matchData.simulation_mode = data.mode;
        const toggle = document.getElementById('simulation-mode-toggle');
        if (toggle) toggle.checked = simulationMode === 'manual';

        appendLog(`[MODE] Simulation set to ${data.mode.toUpperCase()}`, 'comment');

        if (simulationMode === 'auto' && pendingManualDecision && autoContinue) {
            hideDecisionModal();
            clearPendingDecisionState();
            startMatch();
        } else {
            updateDecisionResumeButton();
        }
        return true;
    } catch (err) {
        appendLog(`[ERROR] ${err.message || err}`, 'error');
        return false;
    }
}

function showDecisionModal(data) {
    const overlay = document.getElementById('decision-overlay');
    const title = document.getElementById('decision-title');
    const context = document.getElementById('decision-context');
    const optionsWrap = document.getElementById('decision-options');
    const submitBtn = document.getElementById('decision-submit-btn');
    const autoBtn = document.getElementById('decision-auto-btn');
    if (!overlay || !title || !context || !optionsWrap || !submitBtn) return;

    if (data) {
        pendingManualDecision = data;
        pendingDecisionSelection = null;
    }
    if (!pendingManualDecision) return;
    data = pendingManualDecision;
    submitBtn.disabled = true;

    const type = data.decision_type;
    const ctx = data.decision_context || {};
    const options = Array.isArray(data.decision_options) ? data.decision_options : [];

    if (type === 'next_batter') {
        title.textContent = 'Select Next Batter';
        context.textContent = `${ctx.dismissed_batter || 'Batter'} is out at ${data.score}/${data.wickets}.`;
    } else {
        title.textContent = 'Select Next Bowler';
        context.textContent = `Choose bowler for over ${ctx.upcoming_over || ((data.over || 0) + 1)}.`;
    }

    optionsWrap.innerHTML = '';
    options.forEach(opt => {
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'decision-option';
        const meta = type === 'next_bowler'
            ? `${opt.bowling_type || ''} | ${opt.overs_bowled || 0} ov done | ${opt.overs_remaining || 0} ov left`
            : `${opt.role || ''} | Bat ${opt.batting_rating || 0}`;
        card.innerHTML = `
            <span><strong>${escapeHtml(opt.name)}</strong></span>
            <span class="decision-meta">${escapeHtml(meta)}</span>
        `;
        card.addEventListener('click', () => {
            optionsWrap.querySelectorAll('.decision-option').forEach(n => n.classList.remove('selected'));
            card.classList.add('selected');
            pendingDecisionSelection = opt.index;
            submitBtn.disabled = false;
        });
        optionsWrap.appendChild(card);
    });

    overlay.style.display = 'flex';
    decisionModalVisible = true;
    if (autoBtn) autoBtn.style.display = simulationMode === 'manual' ? 'inline-flex' : 'none';
    updateDecisionResumeButton();
}

async function submitManualDecision() {
    if (!pendingManualDecision || pendingDecisionSelection === null || pendingDecisionSelection === undefined) return;
    const btn = document.getElementById('decision-submit-btn');
    if (btn) btn.disabled = true;
    try {
        const res = await fetch(`${window.location.pathname}/submit-decision`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: pendingManualDecision.decision_type,
                selected_index: pendingDecisionSelection
            })
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            throw new Error(data.error || 'Failed to submit decision');
        }

        appendLog(`[MANUAL] ${data.applied.type === 'next_bowler' ? 'Bowler' : 'Batter'} selected: ${data.applied.name}`, 'comment');
        hideDecisionModal();
        clearPendingDecisionState();
        startMatch();
    } catch (err) {
        appendLog(`[ERROR] ${err.message || err}`, 'error');
        if (btn) btn.disabled = false;
    }
}


// --- Simulation Loop ---

function appendLog(message, type = 'normal') {
    const logContainer = document.getElementById('commentary-log');
    const div = document.createElement('div');
    div.className = 'code-line';

    // Determine token class based on message content regex or type
    let tokenClass = 'token-string'; // Default orange
    if (message.includes('OUT') || message.includes('Wicket')) tokenClass = 'token-error';
    else if (message.includes('FOUR') || message.includes('SIX')) tokenClass = 'token-keyword';
    else if (message.includes('End of Over') || message.includes('Innings')) tokenClass = 'token-comment';

    div.innerHTML = `
        <span class="${tokenClass}">${message}</span>
    `;

    logContainer.appendChild(div);
}

function spinTossAndStartMatch() {
    const resultEl = document.getElementById('toss-result');
    const spinBtn = document.getElementById('spin-toss');
    if (spinBtn) {
        spinBtn.disabled = true;
        spinBtn.textContent = 'Toss in progress...';
    }

    fetch(`${window.location.pathname}/spin-toss`, { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            resultEl.textContent = `${d.toss_winner} chose to ${d.toss_decision}`;
            appendLog(`[TOSS] ${d.toss_commentary}`, 'comment');

            // Set batting team name in banner based on toss result
            const homeTeam = matchData.team_home.split('_')[0];
            const awayTeam = matchData.team_away.split('_')[0];
            const batFirst = (d.toss_winner === homeTeam && d.toss_decision === 'Bat') ||
                             (d.toss_winner === awayTeam && d.toss_decision === 'Bowl')
                             ? homeTeam : awayTeam;
            const batNameEl = document.getElementById('sb-bat-name');
            if (batNameEl) batNameEl.textContent = batFirst;

            setTimeout(startMatch, 1000);
        })
        .catch(err => {
            appendLog(`[ERROR] Toss failed: ${err}`, 'error');
            if (spinBtn) {
                spinBtn.disabled = false;
                spinBtn.textContent = 'Spin Toss';
            }
        });
}

function scheduleNextBall(delayMs) {
    if (simTimerId) clearTimeout(simTimerId);
    simTimerId = setTimeout(startMatch, Math.max(MIN_BALL_DELAY_MS, delayMs));
}

// Track completed-over run totals for the over-flow display
let completedOverTotals = [];   // [{over: 0, runs: 8}, {over: 1, runs: 12}, ...]
let currentOverRunsAccum = 0;   // runs accumulated in the current (incomplete) over

function updateScoreBanner(data) {
    // Main score
    const scoreEl = document.getElementById('sb-score');
    if (scoreEl) scoreEl.textContent = `${data.score}/${data.wickets}`;

    // Overs
    const oversEl = document.getElementById('sb-overs');
    if (oversEl) oversEl.textContent = `${data.over}.${data.ball} ov`;

    // Current Run Rate
    const totalBalls = data.over * 6 + data.ball;
    const crr = totalBalls > 0 ? ((data.score / totalBalls) * 6).toFixed(2) : '0.00';
    const crrEl = document.getElementById('sb-crr');
    if (crrEl) crrEl.textContent = crr;

    // Required Run Rate (2nd innings)
    const rrrWrap = document.getElementById('sb-rrr-wrap');
    const rrrEl = document.getElementById('sb-rrr');
    if (data.target && data.innings_number === 2) {
        const remaining = data.target - data.score;
        const totalOvers = data.total_overs || 20;
        const ballsLeft = (totalOvers * 6) - totalBalls;
        if (ballsLeft > 0 && remaining > 0) {
            const rrr = ((remaining / ballsLeft) * 6).toFixed(2);
            if (rrrEl) rrrEl.textContent = rrr;
            if (rrrWrap) rrrWrap.style.display = '';
        }
    }

    // Match phase
    const phaseEl = document.getElementById('sb-phase');
    if (phaseEl) {
        const totalOvers = data.total_overs || 20;
        if (data.over < 6) phaseEl.textContent = 'POWERPLAY';
        else if (data.over >= totalOvers - 4) phaseEl.textContent = 'DEATH OVERS';
        else phaseEl.textContent = '';
    }

    // Target info
    const targetEl = document.getElementById('sb-target');
    if (targetEl && data.target && data.innings_number === 2) {
        const need = data.target - data.score;
        const ballsLeft = ((data.total_overs || 20) * 6) - totalBalls;
        if (need > 0) {
            targetEl.textContent = `Need ${need} off ${ballsLeft}b`;
            targetEl.style.display = '';
        } else {
            targetEl.style.display = 'none';
        }
    }

    // --- Top Row: Batsmen ---
    const strikerEl = document.getElementById('sb-striker');
    if (strikerEl && data.striker) {
        strikerEl.className = 'sb-bat-row sb-on-strike';
        strikerEl.innerHTML = `
            <span class="sb-bat-name">${escapeHtml(data.striker)}*</span>
            <span class="sb-bat-fig">${data.striker_runs ?? 0} (${data.striker_balls ?? 0})</span>
        `;
    }

    const nsEl = document.getElementById('sb-nonstriker');
    if (nsEl && data.non_striker) {
        nsEl.className = 'sb-bat-row';
        nsEl.innerHTML = `
            <span class="sb-bat-name">${escapeHtml(data.non_striker)}</span>
            <span class="sb-bat-fig">${data.nonstriker_runs ?? 0} (${data.nonstriker_balls ?? 0})</span>
        `;
    }

    // --- Strip: Bowler ---
    const bowlerEl = document.getElementById('sb-bowler');
    if (bowlerEl && data.bowler) {
        bowlerEl.innerHTML = `
            <span class="sb-strip-label">BOWL</span>
            <span class="sb-player-name">${escapeHtml(data.bowler)}</span>
            <span class="sb-player-stat">${data.bowler_wickets ?? 0}/${data.bowler_runs ?? 0} (${data.bowler_overs ?? '0.0'})</span>
        `;
    }

    // --- Strip: Partnership ---
    const partEl = document.getElementById('sb-partnership');
    if (partEl) {
        const pRuns = data.partnership_runs ?? (data.ball_data ? data.ball_data.partnership_runs : 0) ?? 0;
        const pBalls = data.partnership_balls ?? (data.ball_data ? data.ball_data.partnership_balls : 0) ?? 0;
        partEl.innerHTML = `<span class="sb-strip-label">P'SHIP</span><span>${pRuns} (${pBalls})</span>`;
    }

    // --- Strip: Over flow + This Over ball dots ---
    if (data.ball_data) {
        const bd = data.ball_data;

        // Detect new over: finalize previous over total, reset current
        if (thisOverBallResults.length > 0 && bd.over !== thisOverBallResults[0].over) {
            const prevOverRuns = thisOverBallResults.reduce((s, b) => s + b.runs, 0);
            completedOverTotals.push({ over: thisOverBallResults[0].over, runs: prevOverRuns });
            thisOverBallResults = [];
            currentOverRunsAccum = 0;
        }

        thisOverBallResults.push(bd);
        currentOverRunsAccum += bd.runs;

        renderOverFlow();
        renderThisOverBalls();
    }
}

function renderOverFlow() {
    const container = document.getElementById('sb-over-flow');
    if (!container) return;

    // Show last 5 completed overs + current in-progress
    const show = completedOverTotals.slice(-5);
    let html = '<span class="sb-strip-label">OVERS</span>';

    show.forEach(ov => {
        let cls = 'sb-over-box';
        if (ov.runs >= 10) cls += ' sb-over-high';
        else if (ov.runs <= 4) cls += ' sb-over-low';
        html += `<span class="${cls}">${ov.runs}</span>`;
    });

    // Current incomplete over
    if (thisOverBallResults.length > 0) {
        html += `<span class="sb-over-box sb-over-current">${currentOverRunsAccum}*</span>`;
    }

    container.innerHTML = html;
}

function renderThisOverBalls() {
    const container = document.getElementById('sb-this-over');
    if (!container) return;
    container.innerHTML = '';
    thisOverBallResults.forEach(bd => {
        const span = document.createElement('span');
        let label = String(bd.runs);
        let cls = 'sb-ball-dot ball-' + bd.runs;
        if (bd.batter_out) { label = 'W'; cls = 'sb-ball-dot ball-w'; }
        else if (bd.extra_type === 'Wide') { label = 'Wd'; cls = 'sb-ball-dot ball-wd'; }
        else if (bd.extra_type === 'NoBall') { label = 'Nb'; cls = 'sb-ball-dot ball-nb'; }
        else if (bd.runs === 4) { cls = 'sb-ball-dot ball-4'; }
        else if (bd.runs === 6) { cls = 'sb-ball-dot ball-6'; }
        span.className = cls;
        span.textContent = label;
        container.appendChild(span);
    });
}

function startMatch() {
    simTimerId = null;
    if (matchOver) return;

    fetch(window.location.pathname + "/next-ball", { method: 'POST' })
        .then(res => res.json())
        .then(data => {
            if (data.error) {
                appendLog(`[ERROR] ${data.error}`, 'error');
                return;
            }

            // Update broadcast score banner
            if (data.score !== undefined) {
                updateScoreBanner(data);
            }

            // Dashboard: process ball_data for every ball (runs in background regardless of view)
            if (data.ball_data) {
                ballHistory.push(data.ball_data);
                updateCurrentOverBalls(data.ball_data);
                if (typeof updateDashboard === 'function') {
                    updateDashboard(data.ball_data, ballHistory, overRuns, innings1Data);
                }
                if (typeof updateMatchAnimation === 'function') {
                    updateMatchAnimation(data.ball_data, {
                        commentary: data.commentary || '',
                        inningsNumber: data.innings_number,
                        score: data.score,
                        wickets: data.wickets
                    });
                }
            }

            if (data.decision_required) {
                if (data.commentary) appendLog(data.commentary, 'comment');
                showDecisionModal(data);
                return;
            }

            // End of First Innings
            if (data.innings_end && data.innings_number === 1) {
                // Dashboard: save 1st innings data and reset for 2nd
                innings1Data = { ballHistory: [...ballHistory], overRuns: [...overRuns] };
                ballHistory = [];
                overRuns = [];
                currentOverBalls = [];
                thisOverBallResults = [];
                completedOverTotals = [];
                currentOverRunsAccum = 0;
                if (typeof resetDashboardForNewInnings === 'function') {
                    resetDashboardForNewInnings();
                }
                if (typeof resetMatchAnimationForNewInnings === 'function') {
                    resetMatchAnimationForNewInnings();
                }

                // Swap batting team name in banner for 2nd innings
                const batNameEl = document.getElementById('sb-bat-name');
                if (batNameEl) {
                    const homeTeam = matchData.team_home.split('_')[0];
                    const awayTeam = matchData.team_away.split('_')[0];
                    batNameEl.textContent = batNameEl.textContent === homeTeam ? awayTeam : homeTeam;
                }
                // Reset score display
                // Reset banner displays for 2nd innings
                const sbScoreEl = document.getElementById('sb-score');
                if (sbScoreEl) sbScoreEl.textContent = '0/0';
                const sbOversEl = document.getElementById('sb-overs');
                if (sbOversEl) sbOversEl.textContent = '0.0 ov';
                const rrrWrap = document.getElementById('sb-rrr-wrap');
                if (rrrWrap) rrrWrap.style.display = '';
                const phaseEl = document.getElementById('sb-phase');
                if (phaseEl) phaseEl.textContent = '';
                const overFlowEl = document.getElementById('sb-over-flow');
                if (overFlowEl) overFlowEl.innerHTML = '<span class="sb-strip-label">OVERS</span>';
                const thisOverEl = document.getElementById('sb-this-over');
                if (thisOverEl) thisOverEl.innerHTML = '';

                if (data.commentary) appendLog(data.commentary, 'comment');

                if (data.scorecard_data) {
                    showScorecard(data.scorecard_data, data);

                    const closeBtn = document.querySelector('.close-scorecard');
                    // One-time listener for closing 1st innings scorecard
                    closeBtn.onclick = async () => {
                        await captureCurrentScorecardImage(); // Save 1st innings image

                        if (!matchData.impact_players_swapped) {
                            document.getElementById('scorecard-overlay').style.display = 'none';
                            showImpactPlayerModal(); // Trigger Impact Player Phase
                            return;
                        }

                        // Just close and continue if already swapped or some other state
                        document.getElementById('scorecard-overlay').style.display = 'none';
                        scheduleNextBall(delay);
                    };
                    return; // Pause simulation
                }
            }

            // End of Match (Generic Catch-all)
            if (data.match_over) {
                if (data.scorecard_data) {
                    isFinalScoreboard = true;
                    showScorecard(data.scorecard_data, data);
                }
                if (!archiveSaved) {
                    archiveSaved = true;
                    saveMatchArchive();
                }
                appendLog(data.commentary || "Match Concluded.", 'comment');
                matchOver = true;
                return;
            }

            // End of Match (2nd Innings) - legacy fallback
            if (data.innings_end && data.innings_number === 2) {
                if (data.scorecard_data) {
                    isFinalScoreboard = true;
                    showScorecard(data.scorecard_data, data);
                }
                if (!archiveSaved) {
                    archiveSaved = true;
                    saveMatchArchive();
                }
                appendLog(data.commentary || "Match Concluded.", 'comment');
                matchOver = true;
                return;
            }

            // Match Tied / Super Over
            if (data.match_tied) {
                appendLog("MATCH TIED! Super Over Required!");
                if (data.scorecard_data) {
                    setTimeout(() => {
                        showScorecard(data.scorecard_data, data);
                        const closeBtn = document.querySelector('.close-scorecard');
                        const oldOnClick = closeBtn.onclick;
                        closeBtn.onclick = async () => {
                            if (oldOnClick) await oldOnClick();
                            else document.getElementById('scorecard-overlay').style.display = 'none';
                            // Launch super over modal
                            setTimeout(() => {
                                soOpenModal({
                                    home_team: data.home_team,
                                    away_team: data.away_team,
                                    home_players: data.home_players,
                                    away_players: data.away_players,
                                }, 1);
                            }, 400);
                        };
                    }, 1500);
                }
                matchOver = true;
                return;
            }

            // Normal Ball
            appendLog(data.commentary);
            scheduleNextBall(delay);
        })
        .catch(err => appendLog(`[system_error] ${err}`, 'error'));
}


// --- Scorecard Logic ---

function showScorecard(data, completeData) {
    currentInningsNumber = completeData.innings_number;
    const overlay = document.getElementById('scorecard-overlay');
    overlay.style.display = 'flex';

    document.getElementById('scorecard-title').textContent = `${data.innings} INNINGS SCORECARD`;

    // Batsmen - Compact format
    const tbody = document.getElementById('scorecard-tbody');
    tbody.innerHTML = '';
    const displayValue = (value) => (value === null || value === undefined || value === '') ? '' : value;
    data.players.forEach(player => {
        const row = tbody.insertRow();
        row.innerHTML = `
            <td>${escapeHtml(displayValue(player.name))}</td>
            <td style="font-size:0.7rem;color:var(--fg-secondary)">${escapeHtml(displayValue(player.status))}</td>
            <td><strong>${displayValue(player.runs)}</strong></td>
            <td>${displayValue(player.balls)}</td>
            <td>${displayValue(player.fours)}</td>
            <td>${displayValue(player.sixes)}</td>
            <td>${displayValue(player.strike_rate)}</td>
        `;
    });

    // Bowlers - Compact format
    const bTbody = document.getElementById('bowling-tbody');
    bTbody.innerHTML = '';
    data.bowlers.forEach(bowler => {
        const row = bTbody.insertRow();
        row.innerHTML = `
            <td>${escapeHtml(bowler.name)}</td>
            <td>${bowler.overs}</td>
            <td>${bowler.maidens}</td>
            <td>${bowler.runs}</td>
            <td><strong>${bowler.wickets}</strong></td>
            <td>${bowler.economy}</td>
        `;
    });

    // Summary
    document.getElementById('scorecard-summary').textContent =
        `Total: ${data.total_score}/${data.wickets} | Overs: ${data.overs} | Run Rate: ${data.run_rate} | Extras: ${data.extras}`;

    // Target Info
    const targetInfo = document.getElementById('target-info');
    if (data.target_info && data.innings_number !== 2) {
        targetInfo.style.display = 'block';
        targetInfo.textContent = data.target_info;
    } else if (data.wickets === 10 || isFinalScoreboard) {
        targetInfo.style.display = 'block';
        targetInfo.textContent = completeData.result || "Innings Complete";
    } else {
        targetInfo.style.display = 'none';
    }
}

function closeScorecard() {
    // This is the default close action. 
    // Specific flows (like 1st innings end) override the onclick handler.
    // If we are here, it's likely a manual view or end of match simple close.
    captureCurrentScorecardImage().then(() => {
        document.getElementById('scorecard-overlay').style.display = 'none';
    });
}
// Make globally available for the default onclick
window.closeScorecard = closeScorecard;

// --- Image Capture & Saving ---

async function captureCurrentScorecardImage() {
    try {
        const panel = document.querySelector('.scorecard-panel');
        const titleElement = document.getElementById('scorecard-title');

        if (!panel || !titleElement) return false;

        const originalPanelStyle = panel.style.cssText;
        // Simplified styling for capture
        titleElement.style.background = 'none';
        titleElement.style.color = '#3b82f6'; // blue accent

        panel.style.maxHeight = 'none';
        panel.style.overflow = 'visible';
        panel.scrollTop = 0;

        await new Promise(res => setTimeout(res, 150)); // rendering wait

        const canvas = await html2canvas(panel, {
            backgroundColor: null, scale: 2, useCORS: true, logging: false
        });

        // Restore
        titleElement.style = ''; // Reset inline styles
        panel.style.cssText = originalPanelStyle;

        return new Promise(resolve => {
            canvas.toBlob(blob => {
                const title = titleElement.textContent || '';
                if (title.includes('1st INNINGS')) {
                    firstInningsImageBlob = blob;
                } else if (title.includes('2nd INNINGS')) {
                    sendScorecardImagesToBackend(firstInningsImageBlob, blob);
                }
                resolve(true);
            }, 'image/png');
        });
    } catch (e) {
        console.error("Capture failed", e);
        return false;
    }
}

async function sendScorecardImagesToBackend(firstBlob, secondBlob) {
    if (!firstBlob && !secondBlob) return;

    const formData = new FormData();
    const teams = document.querySelector('h1').textContent;
    // We assume h1 exists as per layout
    const safeTeams = teams.replace(/[^a-zA-Z0-9]/g, '_');

    if (firstBlob) formData.append('first_innings_image', firstBlob, `${safeTeams}_1st.png`);
    if (secondBlob) formData.append('second_innings_image', secondBlob, `${safeTeams}_2nd.png`);

    try {
        await fetch(`${window.location.pathname}/save-scorecard-images`, {
            method: 'POST', body: formData
        });
    } catch (e) {
        console.error("Failed to send images", e);
    }
}

async function saveMatchArchive() {
    // Saves Webpage + Commentary + triggers backend archiving + downloads ZIP
    try {
        console.log("📦 Starting match archive process...");

        const fullCommentary = document.getElementById('commentary-log').innerHTML;

        // 1. Save commentary
        await fetch(`${window.location.pathname}/save-commentary`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                commentary_html: fullCommentary,
                match_id: matchData.match_id
            })
        });
        console.log("✅ Commentary saved");

        // 2. Trigger ZIP download
        console.log("📥 Triggering ZIP download...");
        const downloadResponse = await fetch(`${window.location.pathname}/download-archive`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                // C8: Send only the match content, not the full page (avoids leaking CSRF tokens/session data)
                html_content: (document.querySelector('.match-layout') || document.body).outerHTML
            })
        });

        if (downloadResponse.ok) {
            // Get the blob and create download
            const blob = await downloadResponse.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;

            // Extract filename from Content-Disposition header or generate one
            const contentDisposition = downloadResponse.headers.get('Content-Disposition');
            let filename = `match_${matchData.match_id}_archive.zip`;
            if (contentDisposition) {
                const filenameMatch = contentDisposition.match(/filename=(.+)/);
                if (filenameMatch) {
                    filename = filenameMatch[1].replace(/"/g, '');
                }
            }

            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);
            console.log(`✅ ZIP downloaded: ${filename}`);
        } else {
            const errorText = await downloadResponse.text();
            console.error("❌ Failed to download archive:", downloadResponse.status, errorText);
            alert(`Failed to download match archive: ${downloadResponse.status}`);
        }

    } catch (e) {
        console.error("❌ Archive save failed:", e);
    }
}


// --- Super Over Utils ---

// ===================================================================
// SUPER OVER MODAL SYSTEM
// ===================================================================

// State
let soState = {
    round: 1,
    innings: 1,
    battingTeam: null,  // "home" or "away"
    selectedBatsmen: [],
    selectedBowler: null,
    teamData: null,
    target: null,
    ballResults: [],
    innings1Scorecard: null,
};

function soResetState(round) {
    soState = {
        round: round || 1, innings: 1, battingTeam: null,
        selectedBatsmen: [], selectedBowler: null, teamData: null,
        target: null, ballResults: [], innings1Scorecard: null,
    };
}

// --- Open Modal ---
function soOpenModal(teamData, round) {
    soResetState(round);
    soState.teamData = teamData;

    const overlay = document.getElementById('super-over-overlay');
    overlay.style.display = 'flex';

    document.getElementById('so-title').textContent = 'SUPER OVER';
    document.getElementById('so-round-badge').textContent = `Round ${round}`;
    document.getElementById('so-innings-badge').textContent = 'Innings 1';

    // Show team pick, hide others
    document.getElementById('so-team-pick').style.display = '';
    document.getElementById('so-selection').style.display = 'none';
    document.getElementById('so-simulation').style.display = 'none';
    document.getElementById('so-scorecard').style.display = 'none';

    // Add round divider to commentary if continuing from a previous round
    const soComm = document.getElementById('so-commentary');
    if (round > 1 && soComm.children.length > 0) {
        const divider = document.createElement('div');
        divider.className = 'code-line';
        divider.innerHTML = `<span class="token-comment" style="border-top:2px solid var(--accent-color);display:block;margin:10px 0;padding-top:10px;font-weight:bold;">══ Super Over Round ${round} ══</span>`;
        soComm.appendChild(divider);
    } else {
        soComm.innerHTML = '';
    }

    document.getElementById('so-pick-home').textContent = teamData.home_team;
    document.getElementById('so-pick-away').textContent = teamData.away_team;
}
window.soOpenModal = soOpenModal;

// --- Team Pick ---
function soPickBattingTeam(team) {
    soState.battingTeam = team;
    const td = soState.teamData;

    const battingPlayers = team === 'home' ? td.home_players : td.away_players;
    const bowlingPlayers = team === 'home' ? td.away_players : td.home_players;
    const battingName = team === 'home' ? td.home_team : td.away_team;

    soState.selectedBatsmen = [];
    soState.selectedBowler = null;

    document.getElementById('so-team-pick').style.display = 'none';
    document.getElementById('so-selection').style.display = '';
    document.getElementById('so-selection-title').textContent =
        `${battingName} batting — pick your players`;

    soPopulatePlayerCards(battingPlayers, bowlingPlayers);
    soUpdateSelectionCounts();
}
window.soPickBattingTeam = soPickBattingTeam;

function soPopulatePlayerCards(battingPlayers, bowlingPlayers) {
    const batList = document.getElementById('so-batsmen-list');
    const bowlList = document.getElementById('so-bowler-list');
    batList.innerHTML = '';
    bowlList.innerHTML = '';

    // Sort by rating
    const sortedBat = [...battingPlayers].sort((a, b) => b.batting_rating - a.batting_rating);
    const sortedBowl = [...bowlingPlayers].filter(p => p.will_bowl).sort((a, b) => b.bowling_rating - a.bowling_rating);

    sortedBat.forEach(p => {
        const card = document.createElement('div');
        card.className = 'so-player-card';
        card.innerHTML = `
            <span class="so-pc-name">${escapeHtml(p.name)}</span>
            <span class="so-pc-role">${escapeHtml(p.role || '')}</span>
            <span class="so-pc-rating">${p.batting_rating}</span>
        `;
        card.onclick = () => soToggleBatsman(p.name, card);
        batList.appendChild(card);
    });

    sortedBowl.forEach(p => {
        const card = document.createElement('div');
        card.className = 'so-player-card';
        card.innerHTML = `
            <span class="so-pc-name">${escapeHtml(p.name)}</span>
            <span class="so-pc-role">${escapeHtml(p.role || '')}</span>
            <span class="so-pc-rating">${p.bowling_rating}</span>
        `;
        card.onclick = () => soToggleBowler(p.name, card);
        bowlList.appendChild(card);
    });
}

function soToggleBatsman(name, card) {
    const idx = soState.selectedBatsmen.indexOf(name);
    if (idx >= 0) {
        soState.selectedBatsmen.splice(idx, 1);
        card.classList.remove('selected');
    } else if (soState.selectedBatsmen.length < 2) {
        soState.selectedBatsmen.push(name);
        card.classList.add('selected');
    }
    soUpdateSelectionCounts();
}

function soToggleBowler(name, card) {
    // Deselect previous
    const prev = document.querySelector('#so-bowler-list .so-player-card.selected');
    if (prev) prev.classList.remove('selected');

    if (soState.selectedBowler === name) {
        soState.selectedBowler = null;
    } else {
        soState.selectedBowler = name;
        card.classList.add('selected');
    }
    soUpdateSelectionCounts();
}

function soUpdateSelectionCounts() {
    const batCount = document.getElementById('so-bat-count');
    const bowlCount = document.getElementById('so-bowl-count');
    const btn = document.getElementById('so-start-btn');

    batCount.textContent = `Batsmen: ${soState.selectedBatsmen.length}/2`;
    bowlCount.textContent = `Bowler: ${soState.selectedBowler ? 1 : 0}/1`;

    batCount.classList.toggle('valid', soState.selectedBatsmen.length === 2);
    bowlCount.classList.toggle('valid', !!soState.selectedBowler);

    btn.disabled = !(soState.selectedBatsmen.length === 2 && soState.selectedBowler);
}

// --- Start Innings ---
function soStartInnings() {
    const isInnings2 = soState.innings === 2;
    const url = isInnings2
        ? `${window.location.pathname}/start-super-over-innings2`
        : `${window.location.pathname}/start-super-over`;

    const body = isInnings2
        ? { batsmen: soState.selectedBatsmen, bowler: soState.selectedBowler }
        : { first_batting_team: soState.battingTeam, batsmen: soState.selectedBatsmen, bowler: soState.selectedBowler };

    fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) { console.error(data.error); return; }

        if (data.target) soState.target = data.target;

        // Update header
        document.getElementById('so-innings-badge').textContent = `Innings ${soState.innings}`;

        // Switch to simulation view
        document.getElementById('so-selection').style.display = 'none';
        document.getElementById('so-scorecard').style.display = 'none';
        document.getElementById('so-simulation').style.display = '';

        // Init simulation UI
        const teamName = data.batting_team_name || (soState.battingTeam === 'home' ? soState.teamData.home_team : soState.teamData.away_team);
        document.getElementById('so-team-name').textContent = teamName;
        document.getElementById('so-score-display').textContent = '0/0';
        document.getElementById('so-balls-display').textContent = '(0.0)';
        document.getElementById('so-target-info').textContent =
            soState.target ? `Need ${soState.target} to win` : '';

        document.getElementById('so-striker-name').textContent = data.batsmen[0] + ' *';
        document.getElementById('so-striker-stat').textContent = '0 (0)';
        document.getElementById('so-nonstriker-name').textContent = data.batsmen[1];
        document.getElementById('so-nonstriker-stat').textContent = '0 (0)';
        document.getElementById('so-bowler-name').textContent = data.bowler;
        document.getElementById('so-bowler-stat').textContent = '0/0 (0.0)';

        document.getElementById('so-this-over').innerHTML = '';
        soState.ballResults = [];

        // Preserve innings 1 commentary with a divider instead of clearing
        const soComm = document.getElementById('so-commentary');
        if (soState.innings === 2 && soComm.children.length > 0) {
            const divider = document.createElement('div');
            divider.className = 'code-line';
            divider.innerHTML = '<span class="token-comment" style="border-top:1px solid var(--border-color);display:block;margin:8px 0;padding-top:8px;">── Innings 2 ──</span>';
            soComm.appendChild(divider);
        } else {
            soComm.innerHTML = '';
        }

        // Start simulation
        setTimeout(soSimulateBall, 600);
    });
}
window.soStartInnings = soStartInnings;

// --- Simulate Ball ---
function soSimulateBall() {
    fetch(`${window.location.pathname}/next-super-over-ball`, { method: 'POST' })
    .then(r => r.json())
    .then(data => {
        if (data.error) { console.error(data.error); return; }

        // Append commentary
        soAppendLog(data.commentary);

        // Update score
        document.getElementById('so-score-display').textContent = `${data.score}/${data.wickets}`;
        document.getElementById('so-balls-display').textContent = `(0.${data.ball})`;

        // Update target info
        if (soState.target) {
            const need = soState.target - data.score;
            const ballsLeft = 6 - data.ball;
            if (need > 0 && ballsLeft > 0) {
                document.getElementById('so-target-info').textContent = `Need ${need} off ${ballsLeft}b`;
            } else if (need <= 0) {
                document.getElementById('so-target-info').textContent = 'Target reached!';
            }
        }

        // Update players
        document.getElementById('so-striker-name').textContent = (data.striker || '') + ' *';
        document.getElementById('so-striker-stat').textContent = `${data.striker_runs ?? 0} (${data.striker_balls ?? 0})`;
        document.getElementById('so-nonstriker-name').textContent = data.non_striker || '';
        document.getElementById('so-nonstriker-stat').textContent = `${data.nonstriker_runs ?? 0} (${data.nonstriker_balls ?? 0})`;
        document.getElementById('so-bowler-stat').textContent =
            `${data.bowler_wickets ?? 0}/${data.bowler_runs ?? 0} (${data.bowler_overs || '0.0'})`;

        // Add ball indicator
        if (data.ball_data) {
            soState.ballResults.push(data.ball_data);
            soRenderBalls();
        }

        // --- Handle end states ---

        // Innings 1 complete
        if (data.super_over_innings_end) {
            soState.innings1Scorecard = data.innings_scorecard;
            setTimeout(() => {
                soShowMiniScorecard(data.innings_scorecard,
                    `Innings 1 — ${data.first_innings_score} runs`,
                    false, data);
            }, 800);
            return;
        }

        // Innings complete (2 wickets or 6 balls) — not explicitly innings_end
        if (data.innings_complete && !data.super_over_complete && !data.super_over_tied_again) {
            // This is the last ball of an innings; next call will get _end_super_over_innings
            setTimeout(soSimulateBall, 600);
            return;
        }

        // Match decided
        if (data.super_over_complete) {
            const result = data.result;
            appendLog(result); // Also log to main commentary
            setTimeout(() => {
                soShowFinalResult(data);
            }, 800);
            return;
        }

        // Tied again
        if (data.super_over_tied_again) {
            setTimeout(() => {
                soShowTiedAgain(data);
            }, 800);
            return;
        }

        // Normal ball — continue
        setTimeout(soSimulateBall, delay);
    });
}

function soAppendLog(message) {
    const container = document.getElementById('so-commentary');
    const div = document.createElement('div');
    div.className = 'code-line';

    let tokenClass = 'token-string';
    if (/OUT|WICKET|Wicket/i.test(message)) tokenClass = 'token-error';
    else if (/FOUR|SIX|BOUNDARY/i.test(message)) tokenClass = 'token-keyword';
    else if (/End of|Innings|Target/i.test(message)) tokenClass = 'token-comment';

    div.innerHTML = `<span class="${tokenClass}">${escapeHtml(message)}</span>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function soRenderBalls() {
    const container = document.getElementById('so-this-over');
    container.innerHTML = '';
    soState.ballResults.forEach(bd => {
        const span = document.createElement('span');
        span.className = 'sb-ball-dot';
        let label, cls;
        if (bd.batter_out) { label = 'W'; cls = 'ball-w'; }
        else if (bd.extra_type === 'Wide') { label = 'Wd'; cls = 'ball-wd'; }
        else if (bd.extra_type === 'No Ball') { label = 'Nb'; cls = 'ball-nb'; }
        else if (bd.runs === 4) { label = '4'; cls = 'ball-4'; }
        else if (bd.runs === 6) { label = '6'; cls = 'ball-6'; }
        else { label = String(bd.runs); cls = `ball-${bd.runs}`; }
        span.classList.add(cls);
        span.textContent = label;
        container.appendChild(span);
    });
}

// --- Mini Scorecard ---
function soShowMiniScorecard(sc, title, isFinal, responseData) {
    document.getElementById('so-simulation').style.display = 'none';
    document.getElementById('so-scorecard').style.display = '';

    document.getElementById('so-sc-title').textContent = title;

    // Batting table
    const tbody = document.getElementById('so-sc-batting');
    tbody.innerHTML = '';
    (sc.batting || []).forEach(b => {
        const tr = document.createElement('tr');
        if (b.out) tr.className = 'so-out';
        tr.innerHTML = `
            <td>${escapeHtml(b.name)} <small style="color:var(--fg-secondary)">${escapeHtml(b.status)}</small></td>
            <td>${b.runs}</td><td>${b.balls}</td>
            <td>${b.fours}</td><td>${b.sixes}</td><td>${b.sr}</td>
        `;
        tbody.appendChild(tr);
    });

    // Bowling line
    const bowl = sc.bowling || {};
    document.getElementById('so-sc-bowling').textContent =
        `${bowl.name || '?'}: ${bowl.overs || '0.0'} ov — ${bowl.runs || 0}/${bowl.wickets || 0}`;

    // Total
    document.getElementById('so-sc-total').textContent =
        `Total: ${sc.total || 0}/${sc.wickets || 0} (0.${sc.bowling?.balls || 0} ov)`;

    // Result text
    const resultEl = document.getElementById('so-result-text');
    resultEl.style.display = 'none';

    // Continue button
    const btn = document.getElementById('so-continue-btn');
    if (isFinal) {
        btn.textContent = 'Close';
        btn.onclick = () => soCloseModal();
    } else {
        btn.textContent = 'Continue to Innings 2 →';
        btn.onclick = () => soSetupInnings2(responseData);
    }
}

function soSetupInnings2(data) {
    soState.innings = 2;
    soState.selectedBatsmen = [];
    soState.selectedBowler = null;
    soState.target = data.target;
    soState.ballResults = [];

    document.getElementById('so-innings-badge').textContent = 'Innings 2';
    document.getElementById('so-scorecard').style.display = 'none';
    document.getElementById('so-selection').style.display = '';

    const battingName = data.batting_team_name || '';
    document.getElementById('so-selection-title').textContent =
        `${battingName} batting — pick your players`;

    soPopulatePlayerCards(data.batting_team_players, data.bowling_team_players);
    soUpdateSelectionCounts();
}

// --- Final Result ---
function soShowFinalResult(data) {
    document.getElementById('so-simulation').style.display = 'none';
    document.getElementById('so-scorecard').style.display = '';

    // Show innings 2 scorecard
    const sc2 = data.innings2_scorecard;
    if (sc2) {
        soShowMiniScorecard(sc2, 'Innings 2', true, data);
    }

    // Show result
    const resultEl = document.getElementById('so-result-text');
    resultEl.textContent = data.result || 'Match Complete';
    resultEl.style.display = '';

    const btn = document.getElementById('so-continue-btn');
    btn.textContent = 'Close';
    btn.onclick = () => {
        soCloseModal();
        // Show main match scorecard if available
        if (data.scorecard_data) {
            isFinalScoreboard = true;
            showScorecard(data.scorecard_data, { innings_number: 2, result: data.result });
            // Reset close button to default — prevent stale onclick from re-triggering super over
            const closeBtn = document.querySelector('.close-scorecard');
            if (closeBtn) closeBtn.onclick = () => closeScorecard();
        }
    };
}

// --- Tied Again ---
function soShowTiedAgain(data) {
    document.getElementById('so-simulation').style.display = 'none';
    document.getElementById('so-scorecard').style.display = '';

    const sc2 = data.innings2_scorecard;
    if (sc2) {
        document.getElementById('so-sc-title').textContent = 'SUPER OVER TIED!';
        const tbody = document.getElementById('so-sc-batting');
        tbody.innerHTML = '';
        (sc2.batting || []).forEach(b => {
            const tr = document.createElement('tr');
            if (b.out) tr.className = 'so-out';
            tr.innerHTML = `
                <td>${escapeHtml(b.name)} <small style="color:var(--fg-secondary)">${escapeHtml(b.status)}</small></td>
                <td>${b.runs}</td><td>${b.balls}</td>
                <td>${b.fours}</td><td>${b.sixes}</td><td>${b.sr}</td>
            `;
            tbody.appendChild(tr);
        });
        const bowl = sc2.bowling || {};
        document.getElementById('so-sc-bowling').textContent =
            `${bowl.name || '?'}: ${bowl.overs || '0.0'} ov — ${bowl.runs || 0}/${bowl.wickets || 0}`;
        document.getElementById('so-sc-total').textContent =
            `Total: ${sc2.total || 0}/${sc2.wickets || 0}`;
    }

    const resultEl = document.getElementById('so-result-text');
    resultEl.textContent = `TIED! ${data.home_team} ${data.home_score} - ${data.away_team} ${data.away_score}. Another Super Over!`;
    resultEl.style.display = '';

    const btn = document.getElementById('so-continue-btn');
    btn.textContent = 'Next Super Over →';
    btn.onclick = () => {
        soOpenModal({
            home_team: data.home_team,
            away_team: data.away_team,
            home_players: data.home_players,
            away_players: data.away_players,
        }, soState.round + 1);
    };
}

function soCloseModal() {
    document.getElementById('super-over-overlay').style.display = 'none';
    matchOver = true;
}

// Expose for inline onclick handlers
window.soPickBattingTeam = soPickBattingTeam;
window.soStartInnings = soStartInnings;
window.soContinue = function() {}; // placeholder, overridden dynamically

