/**
 * SimCricketX Match Center Dashboard
 * Real-time visual analytics: Wagon Wheel, Charts, Timeline, Win Probability
 * Expects Chart.js and match_detail.js globals to be available.
 *
 * PERF: All DOM rendering is gated behind `dashboardActive` (from match_detail.js).
 *       Data accumulation (ballHistory) always runs; rendering only when visible.
 *       Timeline uses incremental append, not full rebuild.
 */

// --- Chart instances (reused across updates) ---
let manhattanChart = null;
let wormChart = null;

// --- Wagon Wheel State ---
const WAGON_NS = 'http://www.w3.org/2000/svg';
let wagonWheelInitialized = false;
let wagonViewMode = 'current'; // 'current' | 'first'

// --- Timeline State (for incremental append) ---
let _timelineRenderedCount = 0;  // how many balls we've already rendered

function _drawWicketDot(ctx, x, y, r) {
    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = '#e5e7eb';
    ctx.stroke();
    ctx.restore();
}

const wicketMarkerPlugin = {
    id: 'wicketMarkerPlugin',
    afterDatasetsDraw(chart) {
        if (chart.config.type !== 'bar') return;
        const { ctx } = chart;
        chart.data.datasets.forEach((ds, dsIndex) => {
            if (!Array.isArray(ds.wicketCounts)) return;
            const meta = chart.getDatasetMeta(dsIndex);
            if (!meta || !meta.data) return;

            meta.data.forEach((bar, i) => {
                const wk = ds.wicketCounts[i] || 0;
                if (!wk) return;
                const y = Math.min(bar.y, bar.base) - 8;
                _drawWicketDot(ctx, bar.x, y, 5);
                if (wk > 1) {
                    ctx.save();
                    ctx.fillStyle = '#d4d4d4';
                    ctx.font = "600 9px 'IBM Plex Mono'";
                    ctx.fillText(String(wk), bar.x + 6, y + 3);
                    ctx.restore();
                }
            });
        });
    }
};

// ============================================================
//  PUBLIC API — called from match_detail.js
// ============================================================

/**
 * Called on every ball. Only touches DOM when dashboard is visible.
 */
function updateDashboard(ballData, history, oRuns, inn1Data) {
    if (typeof dashboardActive !== 'undefined' && !dashboardActive) return;
    if (wagonViewMode === 'current') {
        addWagonWheelShot(ballData);
    }
    updateWagonViewControls(history, inn1Data);
    updatePlayerCards(history);
    appendBallToTimeline(ballData);
    updateCharts(oRuns, history, inn1Data);
    updateWinProbability(history);
    updateLatestBallTicker(ballData);
}

/**
 * Called when user toggles TO the dashboard. Full rebuild from accumulated data.
 */
function refreshDashboard(history, oRuns, inn1Data) {
    renderWagonByMode(history, inn1Data);
    updatePlayerCards(history);
    rebuildOverTimeline(history);
    rebuildCharts(oRuns, history, inn1Data);
    updateWinProbability(history);
    if (history.length > 0) {
        updateLatestBallTicker(history[history.length - 1]);
    }
}

/**
 * Called at innings transition to reset dashboard for new innings.
 */
function resetDashboardForNewInnings() {
    wagonViewMode = 'current';
    const svg = document.getElementById('wagon-wheel-svg');
    if (svg) svg.querySelectorAll('.wagon-shot, .wagon-six-dot').forEach(el => el.remove());

    // Preserve charts; reset live-per-innings widgets.
    _timelineRenderedCount = 0;
    const el = id => document.getElementById(id);
    el('over-timeline').innerHTML = '';
    el('striker-card').innerHTML = '';
    el('non-striker-card').innerHTML = '';
    el('bowler-card').innerHTML = '';
    el('partnership-bar').innerHTML = '';
    el('win-prob-display').innerHTML = '';
    el('latest-ball-ticker').innerHTML = '';
}

function updateWagonViewControls(history, inn1Data) {
    const label = document.getElementById('wagon-view-label');
    const btn = document.getElementById('wagon-flip-btn');
    if (!label || !btn) return;

    const hasInn1 = !!(inn1Data && Array.isArray(inn1Data.ballHistory) && inn1Data.ballHistory.length > 0);
    const hasCurrent = Array.isArray(history) && history.length > 0;
    btn.style.display = (hasInn1 && hasCurrent) ? 'inline-flex' : 'none';

    if (wagonViewMode === 'first' && !hasInn1) wagonViewMode = 'current';
    if (wagonViewMode === 'first') {
        label.textContent = 'Innings 1';
    } else {
        const inferred = (hasInn1 && hasCurrent) ? 'Innings 2' : ((history[0] && history[0].innings === 1) ? 'Innings 1' : 'Innings 2');
        label.textContent = inferred;
    }
}

function renderWagonByMode(history, inn1Data) {
    initWagonWheel();
    const svg = document.getElementById('wagon-wheel-svg');
    if (!svg) return;
    svg.querySelectorAll('.wagon-shot, .wagon-six-dot').forEach(el => el.remove());

    let source = history;
    if (wagonViewMode === 'first' && inn1Data && Array.isArray(inn1Data.ballHistory)) {
        source = inn1Data.ballHistory;
    }
    source.forEach(bd => addWagonWheelShot(bd));
    updateWagonViewControls(history, inn1Data);
}

// ============================================================
//  1. WAGON WHEEL (SVG) — already incremental
// ============================================================

function initWagonWheel() {
    if (wagonWheelInitialized) return;
    const svg = document.getElementById('wagon-wheel-svg');
    if (!svg) return;
    svg.innerHTML = '';

    const c = (tag, attrs) => {
        const el = document.createElementNS(WAGON_NS, tag);
        for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
        return el;
    };

    svg.appendChild(c('circle', { cx:150, cy:150, r:130, fill:'#1a3a1a', stroke:'#2d5a2d', 'stroke-width':2 }));
    svg.appendChild(c('circle', { cx:150, cy:150, r:120, fill:'none', stroke:'#3d7a3d', 'stroke-width':1, 'stroke-dasharray':'4,4' }));
    svg.appendChild(c('circle', { cx:150, cy:150, r:60, fill:'none', stroke:'#2d5a2d', 'stroke-width':1, 'stroke-dasharray':'3,3' }));
    svg.appendChild(c('rect', { x:145, y:135, width:10, height:30, rx:2, fill:'#8B7355', stroke:'#6b5a3d', 'stroke-width':'0.5' }));
    svg.appendChild(c('circle', { cx:150, cy:158, r:3, fill:'#d4d4d4' }));

    wagonWheelInitialized = true;
}

function addWagonWheelShot(bd) {
    initWagonWheel();
    const svg = document.getElementById('wagon-wheel-svg');
    if (!svg) return;

    const runs = bd.runs, isWicket = bd.batter_out, isExtra = bd.is_extra;
    if (runs === 0 && !isWicket) return;

    if (bd._angle === undefined) bd._angle = Math.random() * Math.PI * 2;
    const angle = bd._angle;
    const cx = 150, cy = 158;
    let length, color, width;

    if (isWicket)        { length = 20 + Math.random() * 20; color = '#ef4444'; width = 2; }
    else if (runs >= 6)  { length = 135; color = '#8b5cf6'; width = 2.5; }
    else if (runs >= 4)  { length = 120; color = '#3b82f6'; width = 2; }
    else if (runs === 3) { length = 75 + Math.random() * 30; color = '#93c5fd'; width = 1.5; }
    else if (runs === 2) { length = 55 + Math.random() * 25; color = '#a3bfcf'; width = 1.2; }
    else                 { length = 30 + Math.random() * 25; color = '#d1d5db'; width = 1; }

    if (isExtra && !isWicket) { color = '#eab308'; width = 1; length = Math.min(length, 50); }

    const x2 = cx + Math.cos(angle) * length;
    const y2 = cy + Math.sin(angle) * length;

    const line = document.createElementNS(WAGON_NS, 'line');
    line.setAttribute('x1', cx); line.setAttribute('y1', cy);
    line.setAttribute('x2', x2); line.setAttribute('y2', y2);
    line.setAttribute('stroke', color); line.setAttribute('stroke-width', width);
    line.setAttribute('stroke-linecap', 'round');
    line.setAttribute('stroke-dasharray', length); line.setAttribute('stroke-dashoffset', length);
    line.classList.add('wagon-shot');
    line.setAttribute('opacity', bd.innings === 1 ? '0.55' : '0.9');
    svg.appendChild(line);

    if (runs >= 6 && !isExtra) {
        const dot = document.createElementNS(WAGON_NS, 'circle');
        dot.setAttribute('cx', x2); dot.setAttribute('cy', y2);
        dot.setAttribute('r', 3); dot.setAttribute('fill', '#8b5cf6'); dot.setAttribute('opacity', '0');
        dot.classList.add('wagon-six-dot');
        svg.appendChild(dot);
        setTimeout(() => dot.setAttribute('opacity', '0.8'), 400);
    }
}

// ============================================================
//  2. PLAYER CARDS
// ============================================================

function updatePlayerCards(history) {
    if (history.length === 0) return;
    const latest = history[history.length - 1];
    const strikerName = latest.striker;
    const nonStrikerName = latest.non_striker;
    const bowlerName = latest.bowler;

    const strikerStats = derivePlayerStats(history, strikerName);
    const nsStats = derivePlayerStats(history, nonStrikerName);
    const bowlerStats = deriveBowlerStats(history, bowlerName);

    document.getElementById('striker-card').className = 'player-card striker';
    document.getElementById('striker-card').innerHTML = renderBatterCard(strikerName, strikerStats, true);
    document.getElementById('non-striker-card').className = 'player-card';
    document.getElementById('non-striker-card').innerHTML = renderBatterCard(nonStrikerName, nsStats, false);
    document.getElementById('bowler-card').className = 'player-card bowler-card';
    document.getElementById('bowler-card').innerHTML = renderBowlerCard(bowlerName, bowlerStats);

    const pRuns = latest.partnership_runs || 0;
    const pBalls = latest.partnership_balls || 0;
    const fillPct = Math.min((pRuns / Math.max(pRuns, 50)) * 100, 100);
    document.getElementById('partnership-bar').innerHTML =
        `<span>P'ship</span>
         <div class="partnership-fill"><div class="partnership-fill-inner" style="width:${fillPct}%"></div></div>
         <span>${pRuns}(${pBalls})</span>`;
}

function derivePlayerStats(history, name) {
    let runs = 0, balls = 0, fours = 0, sixes = 0;
    for (const b of history) {
        if (b.striker === name && !b.is_extra) {
            runs += b.runs; balls++; if (b.runs === 4) fours++; if (b.runs === 6) sixes++;
        }
    }
    return { runs, balls, fours, sixes, sr: balls > 0 ? ((runs / balls) * 100).toFixed(1) : '0.0' };
}

function deriveBowlerStats(history, name) {
    let runs = 0, wickets = 0, legalBalls = 0;
    for (const b of history) {
        if (b.bowler === name) {
            runs += b.runs; if (b.batter_out) wickets++;
            if (!b.is_extra || (b.extra_type !== 'Wide' && b.extra_type !== 'No Ball')) legalBalls++;
        }
    }
    const overs = Math.floor(legalBalls / 6) + '.' + (legalBalls % 6);
    return { runs, wickets, overs, econ: legalBalls > 0 ? ((runs / legalBalls) * 6).toFixed(1) : '0.0' };
}

function renderBatterCard(name, s, isStriker) {
    const icon = isStriker ? '<i class="fa fa-crosshairs" style="color:#569cd6;font-size:0.6rem;margin-right:3px"></i>' : '';
    return `<span class="player-name">${icon}${escapeHtml(name)}</span>
            <span class="player-stats">
                <span class="stat-primary">${s.runs}(${s.balls})</span>
                <span>${s.fours}x4</span><span>${s.sixes}x6</span>
                <span>SR ${s.sr}</span>
            </span>`;
}

function renderBowlerCard(name, s) {
    return `<span class="player-name"><i class="fa fa-baseball" style="color:#ce9178;font-size:0.55rem;margin-right:3px"></i>${escapeHtml(name)}</span>
            <span class="player-stats">
                <span class="stat-primary">${s.overs}-${s.wickets}/${s.runs}</span>
                <span>Econ ${s.econ}</span>
            </span>`;
}

// ============================================================
//  3. OVER TIMELINE — incremental append
// ============================================================

function appendBallToTimeline(bd) {
    const container = document.getElementById('over-timeline');
    if (!container) return;

    // Find or create the over-group for this ball's over
    let group = container.querySelector(`[data-over="${bd.over}"]`);
    if (!group) {
        group = document.createElement('div');
        group.className = 'over-group';
        group.dataset.over = bd.over;

        const label = document.createElement('span');
        label.className = 'over-label';
        label.textContent = bd.over + 1;
        group.appendChild(label);

        container.appendChild(group);
    }

    const dot = document.createElement('span');
    dot.className = 'ball-dot ' + getBallDotClass(bd);
    dot.textContent = getBallDotText(bd);
    group.appendChild(dot);

    container.scrollLeft = container.scrollWidth;
    _timelineRenderedCount++;
}

function rebuildOverTimeline(history) {
    const container = document.getElementById('over-timeline');
    if (!container) return;
    container.innerHTML = '';
    _timelineRenderedCount = 0;
    for (const b of history) appendBallToTimeline(b);
}

function getBallDotClass(b) {
    if (b.batter_out) return 'wicket';
    if (b.is_extra) return 'extra';
    if (b.runs === 6) return 'six';
    if (b.runs === 4) return 'four';
    if (b.runs === 3) return 'three';
    if (b.runs === 2) return 'two';
    if (b.runs === 1) return 'single';
    return 'dot-ball';
}

function getBallDotText(b) {
    if (b.batter_out) return 'W';
    if (b.is_extra) {
        if (b.extra_type === 'Wide') return 'Wd';
        if (b.extra_type === 'No Ball') return 'Nb';
        if (b.extra_type === 'Leg Bye') return 'Lb';
        if (b.extra_type === 'Byes') return 'B';
        return 'E';
    }
    if (b.runs === 0) return '\u00B7';
    return String(b.runs);
}

// ============================================================
//  4/5. CHARTS — Manhattan (bar) + Worm (line)
// ============================================================

function updateCharts(oRuns, history, inn1Data) {
    const currentStats = buildOverStats(oRuns, history);
    const inn1Stats = (inn1Data && Array.isArray(inn1Data.ballHistory))
        ? buildOverStats(inn1Data.overRuns || [], inn1Data.ballHistory)
        : null;
    _updateManhattan(currentStats, inn1Stats);
    _rebuildWorm(history, inn1Data);
}

function rebuildCharts(oRuns, history, inn1Data) {
    if (manhattanChart) { manhattanChart.destroy(); manhattanChart = null; }
    if (wormChart) { wormChart.destroy(); wormChart = null; }
    updateCharts(oRuns, history, inn1Data);
}

function buildOverStats(oRuns, history) {
    const runsByOver = {};
    const wicketsByOver = {};
    for (const b of history) {
        runsByOver[b.over] = (runsByOver[b.over] || 0) + (b.runs || 0);
        if (b.batter_out) wicketsByOver[b.over] = (wicketsByOver[b.over] || 0) + 1;
    }
    for (let i = 0; i < oRuns.length; i++) if (oRuns[i] !== undefined) runsByOver[i] = oRuns[i];
    return { runsByOver, wicketsByOver };
}

function _buildWormSeries(history) {
    const path = [{ x: 0, y: 0 }];
    const wickets = [];
    let cumulative = 0;
    for (const b of history) {
        cumulative += (b.runs || 0);
        const point = { x: b.over + (b.ball + 1) / 6, y: cumulative };
        path.push(point);
        if (b.batter_out) wickets.push(point);
    }
    return { path, wickets };
}

function _updateManhattan(currentStats, inn1Stats) {
    const canvas = document.getElementById('manhattan-chart');
    if (!canvas || typeof Chart === 'undefined') return;

    const hasTwoInnings = !!(inn1Stats && Object.keys(inn1Stats.runsByOver || {}).length > 0);
    const allOvers = [
        ...Object.keys(currentStats.runsByOver || {}).map(Number),
        ...Object.keys((inn1Stats && inn1Stats.runsByOver) || {}).map(Number)
    ];
    const maxOver = Math.max(...allOvers, 0);
    const labels = [];
    const currentData = [];
    const currentWkts = [];
    const currentColors = [];
    const inn1Data = [];
    const inn1Wkts = [];
    for (let i = 0; i <= maxOver; i++) {
        labels.push(i + 1);
        const rCurrent = currentStats.runsByOver[i] || 0;
        const rInn1 = hasTwoInnings ? ((inn1Stats.runsByOver[i] || 0)) : 0;
        currentData.push(rCurrent);
        currentWkts.push(currentStats.wicketsByOver[i] || 0);
        currentColors.push(rCurrent >= 15 ? '#8b5cf6' : rCurrent >= 10 ? '#3b82f6' : rCurrent >= 6 ? '#569cd6' : '#3c6e8f');
        if (hasTwoInnings) {
            inn1Data.push(rInn1);
            inn1Wkts.push(inn1Stats.wicketsByOver[i] || 0);
        }
    }

    const datasets = [];
    if (hasTwoInnings) {
        datasets.push({
            label: '1st Inn',
            data: inn1Data,
            backgroundColor: '#4b5563',
            borderRadius: 3,
            borderSkipped: false,
            wicketCounts: inn1Wkts
        });
        datasets.push({
            label: '2nd Inn',
            data: currentData,
            backgroundColor: currentColors,
            borderRadius: 3,
            borderSkipped: false,
            wicketCounts: currentWkts
        });
    } else {
        datasets.push({
            label: 'Runs',
            data: currentData,
            backgroundColor: currentColors,
            borderRadius: 3,
            borderSkipped: false,
            wicketCounts: currentWkts
        });
    }

    if (manhattanChart) {
        manhattanChart.data.labels = labels;
        manhattanChart.data.datasets = datasets;
        if (manhattanChart.options && manhattanChart.options.plugins && manhattanChart.options.plugins.legend) {
            manhattanChart.options.plugins.legend.display = hasTwoInnings;
        }
        manhattanChart.update('none');
        return;
    }

    manhattanChart = new Chart(canvas.getContext('2d'), {
        plugins: [wicketMarkerPlugin],
        type: 'bar',
        data: { labels, datasets },
        options: {
            responsive: false,
            animation: false,
            plugins: {
                legend: {
                    display: hasTwoInnings,
                    labels: { color: '#a3a3a3', boxWidth: 8, boxHeight: 8, font: { size: 9, family: 'IBM Plex Mono' } }
                }
            },
            scales: {
                x: { grid: { color: '#2d2d2d' }, ticks: { color: '#888', font: { size: 9, family: 'IBM Plex Mono' } } },
                y: { beginAtZero: true, grid: { color: '#2d2d2d' }, ticks: { color: '#888', font: { size: 9, family: 'IBM Plex Mono' }, stepSize: 5 } }
            }
        }
    });
    _resizeChartToParent(manhattanChart, canvas);
}

function _rebuildWorm(history, inn1Data) {
    const canvas = document.getElementById('worm-chart');
    if (!canvas || typeof Chart === 'undefined') return;

    const hasTwoInnings = !!(inn1Data && Array.isArray(inn1Data.ballHistory) && inn1Data.ballHistory.length > 0);
    const currentSeries = _buildWormSeries(history);

    const datasets = [{
        label: hasTwoInnings ? '2nd Innings' : 'Current',
        data: currentSeries.path,
        borderColor: '#569cd6', backgroundColor: 'rgba(86,156,214,0.1)',
        fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
    }];
    datasets.push({
        type: 'scatter',
        label: 'Wickets (Current)',
        data: currentSeries.wickets,
        pointRadius: 5,
        pointHoverRadius: 6,
        pointBackgroundColor: '#ffffff',
        pointBorderColor: '#e5e7eb',
        pointBorderWidth: 1,
        showLine: false
    });

    if (hasTwoInnings) {
        const inn1Series = _buildWormSeries(inn1Data.ballHistory);
        datasets.push({ label: '1st Innings', data: inn1Series.path, borderColor: '#6b7280', borderDash: [5, 3], fill: false, tension: 0.3, pointRadius: 0, borderWidth: 1.5 });
        datasets.push({
            type: 'scatter',
            label: 'Wickets (1st)',
            data: inn1Series.wickets,
            pointRadius: 5,
            pointHoverRadius: 6,
            pointBackgroundColor: '#ffffff',
            pointBorderColor: '#e5e7eb',
            pointBorderWidth: 1,
            showLine: false
        });

        const target = history.length > 0 ? history[history.length - 1].target : null;
        if (target) datasets.push({ label: 'Target', data: [{ x: 0, y: target }, { x: 20, y: target }], borderColor: '#ef4444', borderDash: [8, 4], fill: false, pointRadius: 0, borderWidth: 1 });
    }

    if (wormChart) {
        wormChart.data.datasets = datasets;
        if (wormChart.options && wormChart.options.plugins && wormChart.options.plugins.legend) {
            wormChart.options.plugins.legend.display = hasTwoInnings;
        }
        wormChart.update('none');
        return;
    }

    wormChart = new Chart(canvas.getContext('2d'), {
        type: 'line', data: { datasets },
        options: {
            responsive: false,
            animation: false,
            plugins: {
                legend: {
                    display: hasTwoInnings,
                    labels: { color: '#a3a3a3', boxWidth: 8, boxHeight: 8, font: { size: 9, family: 'IBM Plex Mono' } }
                }
            },
            scales: {
                x: { type: 'linear', min: 0, max: 20, grid: { color: '#2d2d2d' }, ticks: { color: '#888', font: { size: 9, family: 'IBM Plex Mono' }, stepSize: 5 } },
                y: { beginAtZero: true, grid: { color: '#2d2d2d' }, ticks: { color: '#888', font: { size: 9, family: 'IBM Plex Mono' } } }
            }
        }
    });
    _resizeChartToParent(wormChart, canvas);
}

/** Size canvas to its panel, since responsive:false */
function _resizeChartToParent(chart, canvas) {
    const parent = canvas.parentElement;
    if (!parent) return;
    const headerH = parent.querySelector('.panel-header')?.offsetHeight || 0;
    const badgeH = parent.querySelector('.panel-badge-row')?.offsetHeight || 0;
    const w = parent.clientWidth - 16;
    const h = parent.clientHeight - headerH - badgeH - 16;
    if (w > 0 && h > 0) {
        canvas.style.width = w + 'px';
        canvas.style.height = h + 'px';
        chart.resize(w, h);
    }
}

// ============================================================
//  6. WIN PROBABILITY
// ============================================================

function updateWinProbability(history) {
    const container = document.getElementById('win-prob-display');
    if (!container || history.length === 0) return;

    const latest = history[history.length - 1];
    const innings = latest.innings, score = latest.score, wickets = latest.wickets;
    const oversCompleted = latest.over + (latest.ball + 1) / 6;
    let battingProb;

    if (innings === 1) {
        const parScore = oversCompleted * 8.5;
        battingProb = 50 + (score - parScore) * 1.5 - wickets * 5;
    } else {
        const target = latest.target;
        if (!target) { battingProb = 50; }
        else {
            const remaining = target - score;
            const ballsLeft = 120 - (latest.over * 6 + latest.ball + 1);
            const wicketsInHand = 10 - wickets;
            if (remaining <= 0) battingProb = 100;
            else if (wicketsInHand <= 0 || ballsLeft <= 0) battingProb = 0;
            else {
                const rrr = (remaining * 6) / ballsLeft;
                const crr = ballsLeft < 120 ? (score * 6) / (120 - ballsLeft) : 8;
                battingProb = 50 * Math.max(0, 1 - (rrr - crr) * 0.08) + 30 * (wicketsInHand / 10) + 20 * (ballsLeft / 120);
            }
        }
    }

    battingProb = Math.max(5, Math.min(95, battingProb));
    const bowlingProb = 100 - battingProb;
    const batLabel = innings === 1 ? 'BAT' : 'CHASE';
    const bowlLabel = innings === 1 ? 'BOWL' : 'DEF';

    container.innerHTML =
        `<div class="win-prob-bar">
            <div class="win-prob-fill batting" style="width:${battingProb}%">${Math.round(battingProb)}%</div>
            <div class="win-prob-fill bowling" style="width:${bowlingProb}%">${Math.round(bowlingProb)}%</div>
        </div>
        <div class="win-prob-labels"><span>${batLabel}</span><span>${bowlLabel}</span></div>`;
}

// ============================================================
//  7. LATEST BALL TICKER
// ============================================================

function updateLatestBallTicker(bd) {
    const container = document.getElementById('latest-ball-ticker');
    if (!container) return;

    const overBall = `${bd.over}.${bd.ball + 1}`;
    let cls = 'ticker-dot';
    if (bd.batter_out) cls = 'ticker-wicket';
    else if (bd.runs === 6) cls = 'ticker-six';
    else if (bd.runs === 4) cls = 'ticker-four';
    else if (bd.is_extra) cls = 'ticker-extra';
    else if (bd.runs > 0) cls = 'ticker-runs';

    const desc = escapeHtml(bd.description || '');
    container.innerHTML =
        `<span style="color:#888">${overBall}</span>
         <span class="${cls}">${bd.batter_out ? 'W! ' : bd.runs + ' '}${desc}</span>`;
}

async function saveMatchCenterImage() {
    if (typeof html2canvas === 'undefined') {
        alert('Image export library is not loaded.');
        return;
    }
    if (typeof dashboardActive !== 'undefined' && !dashboardActive) {
        alert('Switch to Match Center view before saving.');
        return;
    }

    const panelIds = ['wagon-panel', 'players-panel', 'manhattan-panel', 'worm-panel'];
    const panels = panelIds.map(id => document.getElementById(id));
    if (panels.some(p => !p)) {
        alert('Unable to find dashboard panels for export.');
        return;
    }

    const button = document.getElementById('save-dashboard-btn');
    if (button) button.disabled = true;

    try {
        const captures = await Promise.all(
            panels.map(panel => html2canvas(panel, {
                backgroundColor: '#1e1e1e',
                scale: 2,
                useCORS: true,
                logging: false
            }))
        );

        const colWidths = [
            Math.max(captures[0].width, captures[2].width),
            Math.max(captures[1].width, captures[3].width)
        ];
        const rowHeights = [
            Math.max(captures[0].height, captures[1].height),
            Math.max(captures[2].height, captures[3].height)
        ];
        const gap = 16;
        const canvas = document.createElement('canvas');
        canvas.width = colWidths[0] + colWidths[1] + gap;
        canvas.height = rowHeights[0] + rowHeights[1] + gap;

        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#111827';
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        const drawInCell = (img, cellX, cellY, cellW, cellH) => {
            const dx = cellX + Math.floor((cellW - img.width) / 2);
            const dy = cellY + Math.floor((cellH - img.height) / 2);
            ctx.drawImage(img, dx, dy);
        };

        drawInCell(captures[0], 0, 0, colWidths[0], rowHeights[0]);
        drawInCell(captures[1], colWidths[0] + gap, 0, colWidths[1], rowHeights[0]);
        drawInCell(captures[2], 0, rowHeights[0] + gap, colWidths[0], rowHeights[1]);
        drawInCell(captures[3], colWidths[0] + gap, rowHeights[0] + gap, colWidths[1], rowHeights[1]);

        const a = document.createElement('a');
        const matchId = (window.matchData && window.matchData.match_id) ? window.matchData.match_id : 'match';
        a.href = canvas.toDataURL('image/png');
        a.download = `match_center_${matchId}.png`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    } catch (e) {
        console.error('Failed to export Match Center image', e);
        alert('Failed to export Match Center image.');
    } finally {
        if (button) button.disabled = false;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('save-dashboard-btn');
    if (btn) btn.addEventListener('click', saveMatchCenterImage);

    const flipBtn = document.getElementById('wagon-flip-btn');
    if (flipBtn) {
        flipBtn.addEventListener('click', () => {
            const currentHistory = (typeof ballHistory !== 'undefined' && Array.isArray(ballHistory)) ? ballHistory : [];
            const inn1 = (typeof innings1Data !== 'undefined') ? innings1Data : null;
            const hasInn1 = !!(inn1 && Array.isArray(inn1.ballHistory) && inn1.ballHistory.length > 0);
            const hasCurrent = currentHistory.length > 0;
            if (!(hasInn1 && hasCurrent)) return;

            wagonViewMode = wagonViewMode === 'first' ? 'current' : 'first';
            renderWagonByMode(currentHistory, inn1);
        });
    }
});
