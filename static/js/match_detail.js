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

// Global variable to store first innings scorecard image
let firstInningsImageBlob = null;

// Dashboard data stores
let ballHistory = [];          // Array of ball_data objects for current innings
let overRuns = [];             // Runs per completed over [8, 12, 5, ...]
let currentOverBalls = [];     // Balls in the current over (for timeline)
let innings1Data = null;       // Saved {ballHistory, overRuns} from 1st innings for worm overlay
let dashboardActive = false;   // Which view is showing

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

document.addEventListener('DOMContentLoaded', () => {
    // Pace buttons
    document.querySelectorAll('.pace-btn').forEach(btn => {
        btn.onclick = () => {
            delay = parseInt(btn.dataset.pace);
            document.querySelectorAll('.pace-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
        };
    });

    // View toggle (Commentary / Match Center)
    const viewToggle = document.getElementById('view-toggle');
    if (viewToggle) {
        viewToggle.addEventListener('change', () => {
            dashboardActive = viewToggle.checked;
            document.querySelector('.code-window').style.display = dashboardActive ? 'none' : 'flex';
            document.getElementById('dashboard-container').style.display = dashboardActive ? 'grid' : 'none';
            // Update toggle label styling
            document.getElementById('label-commentary').classList.toggle('active-label', !dashboardActive);
            document.getElementById('label-matchcenter').classList.toggle('active-label', dashboardActive);
            if (dashboardActive && typeof refreshDashboard === 'function') {
                refreshDashboard(ballHistory, overRuns, innings1Data);
            }
            requestAnimationFrame(syncCodeWindowHeight);
        });
    }

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

function startMatch() {
    simTimerId = null;
    if (matchOver) return;

    const scoreElem = document.getElementById('score');
    const overInfoElem = document.getElementById('over-info');

    fetch(window.location.pathname + "/next-ball", { method: 'POST' })
        .then(res => res.json())
        .then(data => {
            if (data.error) {
                appendLog(`[ERROR] ${data.error}`, 'error');
                return;
            }

            // Update Scoreboard Header
            if (data.score !== undefined) {
                scoreElem.textContent = `Score: ${data.score}/${data.wickets}`;
                overInfoElem.textContent = `Over: ${data.over}.${data.ball}`;
            }

            // Dashboard: process ball_data for every ball (runs in background regardless of view)
            if (data.ball_data) {
                ballHistory.push(data.ball_data);
                updateCurrentOverBalls(data.ball_data);
                if (typeof updateDashboard === 'function') {
                    updateDashboard(data.ball_data, ballHistory, overRuns, innings1Data);
                }
            }

            // End of First Innings
            if (data.innings_end && data.innings_number === 1) {
                // Dashboard: save 1st innings data and reset for 2nd
                innings1Data = { ballHistory: [...ballHistory], overRuns: [...overRuns] };
                ballHistory = [];
                overRuns = [];
                currentOverBalls = [];
                if (typeof resetDashboardForNewInnings === 'function') {
                    resetDashboardForNewInnings();
                }

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
                appendLog("MATCH TIED! Super Over Required!", 'keyword');
                if (data.scorecard_data) {
                    setTimeout(() => {
                        showScorecard(data.scorecard_data, data);
                        const closeBtn = document.querySelector('.close-scorecard');
                        const oldOnClick = closeBtn.onclick;
                        closeBtn.onclick = async () => {
                            if (oldOnClick) await oldOnClick(); // standard close
                            else document.getElementById('scorecard-overlay').style.display = 'none';

                            // Show Super Over Options
                            setTimeout(() => {
                                appendLog("waiting for super over decision...", 'comment');
                                const logContainer = document.getElementById('commentary-log');
                                const div = document.createElement('div');
                                div.className = 'code-line';
                                div.innerHTML = `
                                    <span class="line-number">${logLineCount++}</span>
                                    <span class="token-keyword">
                                        <button onclick="startSuperOver('home')" class="impact-btn primary" style="font-size:0.7rem; padding:2px 8px;">Option 1: ${escapeHtml(data.home_team)}</button>
                                        <button onclick="startSuperOver('away')" class="impact-btn primary" style="font-size:0.7rem; padding:2px 8px;">Option 2: ${escapeHtml(data.away_team)}</button>
                                    </span>
                                `;
                                logContainer.appendChild(div);
                            }, 500);
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

function startSuperOver(firstBattingTeam) {
    const commentaryLog = document.getElementById('commentary-log');

    fetch(`${window.location.pathname}/start-super-over`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ first_batting_team: firstBattingTeam })
    })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                commentaryLog.insertAdjacentHTML('beforeend', `<p class="error">${escapeHtml(data.error)}</p>`);
                return;
            }
            commentaryLog.insertAdjacentHTML('beforeend', `<p>${escapeHtml(data.commentary)}</p>`);
            commentaryLog.insertAdjacentHTML('beforeend', `
            <div style="text-align: center; margin: 1rem 0;">
                <button onclick="startSuperOverSimulation()" class="impact-btn primary">
                    Start Super Over Simulation
                </button>
            </div>
        `);
        });
}
window.startSuperOver = startSuperOver; // Expose

function startSuperOverSimulation() {
    matchOver = false;
    const commentaryLog = document.getElementById('commentary-log');
    const scoreElem = document.getElementById('score');
    const overElem = document.getElementById('over-info');

    fetch(`${window.location.pathname}/next-super-over-ball`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                commentaryLog.insertAdjacentHTML('beforeend', `<p class="error">${escapeHtml(data.error)}</p>`);
                return;
            }

            commentaryLog.insertAdjacentHTML('beforeend', `<p>${escapeHtml(data.commentary)}</p>`);

            scoreElem.textContent = `Super Over: ${data.score}/${data.wickets}`;
            overElem.textContent = `Ball: ${data.ball}/6`;

            if (data.super_over_tied_again) {
                commentaryLog.insertAdjacentHTML('beforeend', `<p style="color:orange; font-weight:bold;">TIED AGAIN!</p>`);
                matchOver = true;
                return;
            }

            if (data.super_over_complete) {
                commentaryLog.insertAdjacentHTML('beforeend', `<p style="color:green; font-weight:bold;">${escapeHtml(data.result)}</p>`);
                matchOver = true;
                return;
            }

            if (data.super_over_innings_end || data.innings_complete) {
                setTimeout(startSuperOverSimulation, 1000);
            } else {
                setTimeout(startSuperOverSimulation, delay);
            }
        });
}
window.startSuperOverSimulation = startSuperOverSimulation; // Expose

