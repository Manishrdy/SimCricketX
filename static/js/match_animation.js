/**
 * SimCricketX Live Match Animation
 * Phase 5: broadcast polish with audio cues, replay queue, and cinematic camera presets.
 */

(function () {
    const NS = 'http://www.w3.org/2000/svg';

    const state = {
        ready: false,
        queue: Promise.resolve(),
        entities: {},
        fielders: [],
        svg: null,
        stage: null,
        statusEl: null,
        batterHands: {},
        eventBadgeEl: null,
        replayStingerEl: null,
        replayQueueEl: null,
        replayQueue: [],
        replayRunning: false,
        audioEnabled: false,
        audioCtx: null,
        settingsPanelEl: null,
        settings: {
            volume: 35,
            replayEnabled: true,
            replayFrequency: 100,
            cameraIntensity: 100
        }
    };

    const CAMERA_PRESETS = {
        default: { base: 12, scale: 1.014, duration: 430 },
        boundary: { base: 13, scale: 1.018, duration: 460 },
        six: { base: 15, scale: 1.022, duration: 520 },
        wicket: { base: 14, scale: 1.02, duration: 500 },
        close: { base: 11, scale: 1.02, duration: 420 },
        field: { base: 10, scale: 1.012, duration: 380 }
    };

    const POS = {
        center: { x: 500, y: 280 },
        striker: { x: 500, y: 312 },
        nonStriker: { x: 500, y: 248 },
        bowler: { x: 500, y: 140 },
        umpire: { x: 545, y: 280 },
        keeper: { x: 500, y: 356 },
        strikerStumps: { x: 500, y: 330 },
        fielders: [
            { x: 360, y: 160, name: 'Deep Point', short: 'DP', ring: 'deep' },
            { x: 635, y: 165, name: 'Long Off', short: 'LO', ring: 'deep' },
            { x: 290, y: 275, name: 'Cover', short: 'CV', ring: 'inner' },
            { x: 710, y: 278, name: 'Mid Wicket', short: 'MW', ring: 'inner' },
            { x: 410, y: 430, name: 'Fine Leg', short: 'FL', ring: 'deep' },
            { x: 600, y: 430, name: 'Third Man', short: 'TM', ring: 'deep' }
        ]
    };

    function createSvgEl(tag, attrs = {}) {
        const el = document.createElementNS(NS, tag);
        Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
        return el;
    }

    function shortName(name) {
        if (!name) return '-';
        const parts = String(name).trim().split(/\s+/);
        if (parts.length === 1) return parts[0];
        return `${parts[0]} ${parts[parts.length - 1]}`;
    }

    function setStatus(text) {
        if (state.statusEl) state.statusEl.textContent = text;
    }

    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    function ensureBroadcastUi() {
        if (!state.stage) return;

        if (!state.eventBadgeEl) {
            const badge = document.createElement('div');
            badge.className = 'animation-event-badge';
            state.stage.appendChild(badge);
            state.eventBadgeEl = badge;
        }

        if (!state.replayStingerEl) {
            const stinger = document.createElement('div');
            stinger.className = 'animation-replay-stinger';
            stinger.textContent = 'REPLAY';
            state.stage.appendChild(stinger);
            state.replayStingerEl = stinger;
        }

        if (!state.replayQueueEl) {
            const wrap = document.createElement('div');
            wrap.className = 'animation-replay-queue';
            wrap.innerHTML = '<div class="rq-title">Replay Queue</div><div class="rq-list"></div>';
            state.stage.appendChild(wrap);
            state.replayQueueEl = wrap;
            renderReplayQueue();
        }

        if (!state.settingsPanelEl) {
            const panel = document.createElement('div');
            panel.className = 'animation-settings-panel';
            panel.innerHTML = `
                <div class="asp-title">Animation Settings</div>
                <label class="asp-check">
                    <input type="checkbox" id="asp-replay-enabled" checked>
                    <span>Replay Queue Enabled</span>
                </label>
                <div class="asp-row">
                    <span class="asp-label">Audio Volume</span>
                    <span class="asp-value" id="asp-volume-val">35%</span>
                </div>
                <input type="range" id="asp-volume" min="0" max="100" value="35">
                <div class="asp-row">
                    <span class="asp-label">Replay Frequency</span>
                    <span class="asp-value" id="asp-replayfreq-val">100%</span>
                </div>
                <input type="range" id="asp-replayfreq" min="0" max="100" value="100">
                <div class="asp-row">
                    <span class="asp-label">Camera Intensity</span>
                    <span class="asp-value" id="asp-camint-val">100%</span>
                </div>
                <input type="range" id="asp-camint" min="40" max="140" value="100">
            `;
            state.stage.appendChild(panel);
            state.settingsPanelEl = panel;
            bindSettingsPanel(panel);
        }
    }

    function bindSettingsPanel(panel) {
        const replayEnabledEl = panel.querySelector('#asp-replay-enabled');
        const volumeEl = panel.querySelector('#asp-volume');
        const replayFreqEl = panel.querySelector('#asp-replayfreq');
        const camIntEl = panel.querySelector('#asp-camint');
        const volumeVal = panel.querySelector('#asp-volume-val');
        const replayFreqVal = panel.querySelector('#asp-replayfreq-val');
        const camIntVal = panel.querySelector('#asp-camint-val');
        if (!replayEnabledEl || !volumeEl || !replayFreqEl || !camIntEl || !volumeVal || !replayFreqVal || !camIntVal) return;

        replayEnabledEl.checked = !!state.settings.replayEnabled;
        volumeEl.value = String(state.settings.volume);
        replayFreqEl.value = String(state.settings.replayFrequency);
        camIntEl.value = String(state.settings.cameraIntensity);

        volumeVal.textContent = `${state.settings.volume}%`;
        replayFreqVal.textContent = `${state.settings.replayFrequency}%`;
        camIntVal.textContent = `${state.settings.cameraIntensity}%`;

        replayEnabledEl.addEventListener('change', () => {
            state.settings.replayEnabled = replayEnabledEl.checked;
        });
        volumeEl.addEventListener('input', () => {
            state.settings.volume = Number(volumeEl.value);
            volumeVal.textContent = `${state.settings.volume}%`;
        });
        replayFreqEl.addEventListener('input', () => {
            state.settings.replayFrequency = Number(replayFreqEl.value);
            replayFreqVal.textContent = `${state.settings.replayFrequency}%`;
        });
        camIntEl.addEventListener('input', () => {
            state.settings.cameraIntensity = Number(camIntEl.value);
            camIntVal.textContent = `${state.settings.cameraIntensity}%`;
        });
    }

    function renderReplayQueue() {
        if (!state.replayQueueEl) return;
        const list = state.replayQueueEl.querySelector('.rq-list');
        if (!list) return;
        list.innerHTML = '';
        const items = state.replayQueue.slice(0, 4);
        if (!items.length) {
            const empty = document.createElement('div');
            empty.className = 'rq-item';
            empty.textContent = 'No highlights queued';
            list.appendChild(empty);
            return;
        }
        items.forEach((item, idx) => {
            const row = document.createElement('div');
            row.className = 'rq-item';
            row.textContent = `${idx + 1}. ${item.label}`;
            list.appendChild(row);
        });
    }

    function showEventBadge(text, tone = '') {
        if (!state.eventBadgeEl) return;
        const badge = state.eventBadgeEl;
        badge.className = 'animation-event-badge';
        if (tone) badge.classList.add(`tone-${tone}`);
        badge.textContent = text;
        badge.classList.add('show');
        setTimeout(() => badge && badge.classList.remove('show'), 1200);
    }

    function playReplayStinger(label = 'Replay') {
        if (!state.replayStingerEl) return;
        const stinger = state.replayStingerEl;
        stinger.textContent = String(label || 'Replay').toUpperCase();
        stinger.classList.remove('show');
        void stinger.getBoundingClientRect();
        stinger.classList.add('show');
    }

    function pulseCrowd() {
        if (!state.stage) return;
        state.stage.classList.remove('crowd-pulse');
        void state.stage.getBoundingClientRect();
        state.stage.classList.add('crowd-pulse');
        setTimeout(() => state.stage && state.stage.classList.remove('crowd-pulse'), 620);
    }

    function panCameraTo(point, presetName = 'default', strength = 1) {
        if (!state.svg || !point) return;
        const preset = CAMERA_PRESETS[presetName] || CAMERA_PRESETS.default;
        const intensity = (state.settings.cameraIntensity || 100) / 100;
        const dx = ((point.x - POS.center.x) / 260) * preset.base * strength * intensity;
        const dy = ((point.y - POS.center.y) / 200) * preset.base * strength * intensity;
        const scale = 1 + ((preset.scale - 1) * intensity);
        state.svg.style.transform = `translate(${-dx.toFixed(2)}px, ${-dy.toFixed(2)}px) scale(${scale})`;
        setTimeout(() => {
            if (state.svg) state.svg.style.transform = 'translate(0px, 0px) scale(1)';
        }, preset.duration);
    }

    function ensureAudioContext() {
        if (state.audioCtx) return state.audioCtx;
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return null;
        state.audioCtx = new Ctx();
        return state.audioCtx;
    }

    function playAudioCue(kind) {
        if (!state.audioEnabled) return;
        const ctx = ensureAudioContext();
        if (!ctx) return;
        if (ctx.state === 'suspended') {
            ctx.resume().catch(() => null);
        }

        const now = ctx.currentTime;
        const cues = {
            four: [560, 640],
            six: [650, 820, 980],
            wicket: [220, 160, 140],
            run: [430],
            dot: [280]
        };
        const tones = cues[kind] || [360];

        tones.forEach((freq, idx) => {
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = kind === 'wicket' ? 'square' : 'triangle';
            osc.frequency.value = freq;
            const maxGain = 0.0001 + ((state.settings.volume || 0) / 100) * 0.03;
            gain.gain.setValueAtTime(0.0001, now);
            gain.gain.exponentialRampToValueAtTime(maxGain, now + (idx * 0.06) + 0.01);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + (idx * 0.06) + 0.14);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(now + (idx * 0.06));
            osc.stop(now + (idx * 0.06) + 0.16);
        });
    }

    function enqueueReplayHighlight(label, point, preset = 'default') {
        if (!state.settings.replayEnabled) return;
        const freq = Number(state.settings.replayFrequency || 100);
        if (Math.random() * 100 > freq) return;
        state.replayQueue.push({ label, point, preset });
        if (state.replayQueue.length > 12) {
            state.replayQueue = state.replayQueue.slice(-12);
        }
        renderReplayQueue();
        processReplayQueue();
    }

    async function processReplayQueue() {
        if (state.replayRunning) return;
        state.replayRunning = true;
        while (state.replayQueue.length) {
            const clip = state.replayQueue.shift();
            renderReplayQueue();
            await sleep(720);
            playReplayStinger(`Replay: ${clip.label}`);
            panCameraTo(clip.point || POS.center, clip.preset || 'default', 1);
            await sleep((CAMERA_PRESETS[clip.preset]?.duration || 430) + 280);
        }
        state.replayRunning = false;
    }

    function hashName(name) {
        const s = String(name || '').toLowerCase();
        let h = 0;
        for (let i = 0; i < s.length; i++) h = ((h * 31) + s.charCodeAt(i)) >>> 0;
        return h;
    }

    function inferHandFromData(ballData, batterName) {
        const handCandidate =
            ballData?.striker_hand ||
            ballData?.striker_handedness ||
            ballData?.batting_hand ||
            ballData?.batting_style ||
            ballData?.striker_style ||
            '';

        if (typeof handCandidate === 'string' && handCandidate.trim()) {
            return /left/i.test(handCandidate) ? 'left' : 'right';
        }

        if (state.batterHands[batterName]) return state.batterHands[batterName];
        const fallback = (hashName(batterName) % 2 === 0) ? 'right' : 'left';
        state.batterHands[batterName] = fallback;
        return fallback;
    }

    function setBatterStance(entity, hand) {
        if (!entity || !entity.g) return;
        entity.hand = hand;
        entity.g.classList.toggle('stance-left', hand === 'left');
        entity.g.classList.toggle('stance-right', hand !== 'left');
    }

    function createPlayer(id, role, x, y, labelText) {
        const g = createSvgEl('g', { class: `anim-player ${role}`, 'data-id': id });

        const torso = createSvgEl('rect', {
            x: x - 7, y: y - 10, width: 14, height: 20, rx: 4, class: 'anim-player-body'
        });
        const head = createSvgEl('circle', { cx: x, cy: y - 17, r: 5.2, class: 'anim-player-head' });

        const armLeft = createSvgEl('line', { x1: x - 4, y1: y - 6, x2: x - 11, y2: y + 2, class: 'anim-limb arm arm-left' });
        const armRight = createSvgEl('line', { x1: x + 4, y1: y - 6, x2: x + 11, y2: y + 2, class: 'anim-limb arm arm-right' });
        const legLeft = createSvgEl('line', { x1: x - 3, y1: y + 10, x2: x - 8, y2: y + 19, class: 'anim-limb leg leg-left' });
        const legRight = createSvgEl('line', { x1: x + 3, y1: y + 10, x2: x + 8, y2: y + 19, class: 'anim-limb leg leg-right' });

        g.appendChild(armLeft);
        g.appendChild(armRight);
        g.appendChild(legLeft);
        g.appendChild(legRight);
        g.appendChild(torso);
        g.appendChild(head);

        let bat = null;
        if (role === 'batter') {
            bat = createSvgEl('line', {
                x1: x + 9, y1: y - 2, x2: x + 17, y2: y - 20, class: 'anim-bat'
            });
            g.appendChild(bat);
            g.classList.add('stance-right');
        }

        const label = createSvgEl('text', {
            x, y: y - 28, class: 'anim-label', 'text-anchor': 'middle'
        });
        label.textContent = labelText;
        g.appendChild(label);

        return { g, label, x, y, role, bat, hand: 'right' };
    }

    function createStumps(x, y) {
        const g = createSvgEl('g', { class: 'anim-stumps' });
        [-6, 0, 6].forEach(dx => {
            g.appendChild(createSvgEl('line', {
                x1: x + dx, y1: y - 10, x2: x + dx, y2: y + 10, class: 'anim-stump-line'
            }));
        });
        g.appendChild(createSvgEl('line', { x1: x - 8, y1: y - 10, x2: x + 2, y2: y - 10, class: 'anim-bail-line' }));
        g.appendChild(createSvgEl('line', { x1: x - 2, y1: y - 10, x2: x + 8, y2: y - 10, class: 'anim-bail-line' }));
        return g;
    }

    function createGround(svg) {
        svg.innerHTML = '';
        svg.appendChild(createSvgEl('ellipse', {
            cx: 500, cy: 280, rx: 430, ry: 240, fill: '#1d4f28', stroke: '#315f3a', 'stroke-width': '4'
        }));
        svg.appendChild(createSvgEl('ellipse', {
            cx: 500, cy: 280, rx: 330, ry: 180, fill: 'none', stroke: '#5b8f5b', 'stroke-width': '2', 'stroke-dasharray': '8 7'
        }));
        svg.appendChild(createSvgEl('rect', {
            x: 472, y: 214, width: 56, height: 132, rx: 9, fill: '#c8b08a', stroke: '#8b7355', 'stroke-width': '2'
        }));
        svg.appendChild(createSvgEl('line', {
            x1: 500, y1: 214, x2: 500, y2: 346, stroke: '#8b7355', 'stroke-width': '1.5', 'stroke-dasharray': '3 4'
        }));
    }

    function clearPoses(entity) {
        if (!entity || !entity.g) return;
        entity.g.classList.remove(
            'pose-runup', 'pose-release', 'pose-followthrough', 'pose-bat',
            'pose-bat-left', 'pose-bat-right',
            'pose-run', 'pose-run-left', 'pose-run-right',
            'pose-dive', 'pose-catch-high', 'pose-pickup-throw', 'pose-celebrate',
            'pose-gesture-four', 'pose-gesture-six', 'pose-gesture-out'
        );
    }

    function pulsePose(entity, poseClass, duration = 450) {
        if (!entity || !entity.g) return;
        entity.g.classList.remove(poseClass);
        void entity.g.getBoundingClientRect();
        entity.g.classList.add(poseClass);
        setTimeout(() => entity.g && entity.g.classList.remove(poseClass), duration);
    }

    function pulseBatterShot(entity, duration = 360) {
        if (!entity || !entity.g) return;
        const pose = entity.hand === 'left' ? 'pose-bat-left' : 'pose-bat-right';
        pulsePose(entity, pose, duration);
    }

    function pulseBatterRun(entity, duration = 340) {
        if (!entity || !entity.g) return;
        const pose = entity.hand === 'left' ? 'pose-run-left' : 'pose-run-right';
        pulsePose(entity, pose, duration);
    }

    function pulseStumpsHit() {
        const stumps = state.entities.strikerStumps;
        if (!stumps) return;
        stumps.classList.remove('stumps-hit');
        void stumps.getBoundingClientRect();
        stumps.classList.add('stumps-hit');
        setTimeout(() => stumps && stumps.classList.remove('stumps-hit'), 450);
    }

    async function runBowlerSequence() {
        const b = state.entities.bowler;
        if (!b) return;
        pulsePose(b, 'pose-runup', 260);
        await sleep(130);
        pulsePose(b, 'pose-release', 240);
        await sleep(120);
        pulsePose(b, 'pose-followthrough', 280);
    }

    function flashStage(kind) {
        if (!state.stage) return;
        const cls = `flash-${kind}`;
        state.stage.classList.remove('flash-four', 'flash-six', 'flash-wicket');
        void state.stage.getBoundingClientRect();
        state.stage.classList.add(cls);
        setTimeout(() => state.stage && state.stage.classList.remove(cls), 420);
    }

    function setBallPosition(x, y, visible = true) {
        const ball = state.entities.ball;
        if (!ball) return;
        ball.setAttribute('cx', String(x));
        ball.setAttribute('cy', String(y));
        ball.classList.toggle('hidden', !visible);
    }

    function animateBall(from, to, duration = 360, arc = 0) {
        return new Promise(resolve => {
            const start = performance.now();
            function tick(now) {
                const t = Math.min(1, (now - start) / duration);
                const eased = 1 - Math.pow(1 - t, 3);
                const x = from.x + ((to.x - from.x) * eased);
                const baseY = from.y + ((to.y - from.y) * eased);
                const lift = (Math.sin(Math.PI * t) * arc);
                const y = baseY - lift;
                setBallPosition(x, y, true);
                if (t < 1) requestAnimationFrame(tick);
                else resolve();
            }
            requestAnimationFrame(tick);
        });
    }

    function chooseNearestFielder(target, predicate = null) {
        if (!state.fielders.length) return state.entities.keeper || null;
        const pool = predicate ? state.fielders.filter(predicate) : state.fielders;
        const usePool = pool.length ? pool : state.fielders;
        let best = usePool[0];
        let bestDist = Infinity;
        for (const f of usePool) {
            const dx = f.x - target.x;
            const dy = f.y - target.y;
            const d = (dx * dx) + (dy * dy);
            if (d < bestDist) {
                bestDist = d;
                best = f;
            }
        }
        return best;
    }

    function pickShotDirection(hand, runs) {
        const legBias = hand === 'left' ? -1 : 1;
        const isBoundary = runs >= 4;
        const angleBase = isBoundary ? (Math.PI * 0.85) : (Math.PI * 0.95);
        const jitter = (Math.random() - 0.5) * (isBoundary ? 1.0 : 0.7);
        const angle = angleBase + (legBias * 0.45) + jitter;
        const radius = runs >= 6 ? 260 : (runs === 4 ? 220 : 150);
        return {
            x: POS.center.x + (Math.cos(angle) * radius),
            y: POS.center.y + (Math.sin(angle) * radius)
        };
    }

    function chooseFielderForLanding(landing, runs, preferCatch = false) {
        const boundaryBall = runs >= 4;
        if (preferCatch) {
            return chooseNearestFielder(landing);
        }
        if (boundaryBall) {
            return chooseNearestFielder(landing, f => f.ring === 'deep');
        }
        return chooseNearestFielder(landing, f => f.ring === 'inner');
    }

    function inferWicketType(ballData, commentary) {
        const explicit = String(
            ballData?.wicket_type ||
            ballData?.dismissal_type ||
            ballData?.out_type ||
            ''
        ).toLowerCase();
        const text = String(commentary || '').toLowerCase();
        const source = `${explicit} ${text}`;

        if (/run\s*out/.test(source)) return 'run_out';
        if (/stump/.test(source)) return 'stumped';
        if (/lbw/.test(source)) return 'lbw';
        if (/bowled|clean bowled/.test(source)) return 'bowled';
        if (/caught|c&b|catch/.test(source)) return 'caught';
        return 'caught';
    }

    function eventLabel(ballData) {
        if (!ballData) return 'Waiting for toss...';
        if (ballData.batter_out) return `WICKET! ${shortName(ballData.bowler)} strikes`;
        if (ballData.extra_type === 'Wide') return 'WIDE';
        if (ballData.extra_type === 'No Ball') return 'NO BALL';
        if (ballData.runs === 6) return 'SIX!';
        if (ballData.runs === 4) return 'FOUR!';
        if (ballData.runs === 0) return 'DOT BALL';
        return `${ballData.runs} RUN${ballData.runs > 1 ? 'S' : ''}`;
    }

    function syncNames(ballData) {
        if (!ballData) return;
        const e = state.entities;
        if (e.striker) {
            e.striker.label.textContent = `${shortName(ballData.striker)} *`;
            setBatterStance(e.striker, inferHandFromData(ballData, ballData.striker));
        }
        if (e.nonStriker) {
            e.nonStriker.label.textContent = shortName(ballData.non_striker);
            setBatterStance(e.nonStriker, inferHandFromData(ballData, ballData.non_striker));
        }
        if (e.bowler) e.bowler.label.textContent = shortName(ballData.bowler);
    }

    async function animateWicketFlow(kind, pitchHit, hand) {
        const keeper = state.entities.keeper;
        const bowler = state.entities.bowler;
        const umpire = state.entities.umpire;
        const striker = state.entities.striker;
        const nonStriker = state.entities.nonStriker;

        flashStage('wicket');
        pulseCrowd();
        playReplayStinger(kind.replace('_', ' '));
        playAudioCue('wicket');
        if (umpire) pulsePose(umpire, 'pose-gesture-out', 900);
        if (bowler) pulsePose(bowler, 'pose-celebrate', 900);

        if (kind === 'bowled') {
            panCameraTo(POS.strikerStumps, 'wicket', 1.2);
            await animateBall(pitchHit, { x: POS.strikerStumps.x, y: POS.strikerStumps.y - 2 }, 180, 6);
            pulseStumpsHit();
            setBallPosition(POS.strikerStumps.x, POS.strikerStumps.y - 2, false);
            return;
        }

        if (kind === 'lbw') {
            await animateBall(pitchHit, { x: POS.striker.x + (hand === 'left' ? -5 : 5), y: POS.striker.y + 1 }, 120, 2);
            if (striker) pulsePose(striker, 'pose-bat', 260);
            setBallPosition(POS.striker.x, POS.striker.y + 1, false);
            return;
        }

        if (kind === 'stumped') {
            panCameraTo(POS.keeper, 'close', 1.05);
            if (keeper) pulsePose(keeper, 'pose-dive', 320);
            await animateBall(pitchHit, { x: POS.keeper.x, y: POS.keeper.y - 8 }, 210, 14);
            pulseStumpsHit();
            setBallPosition(POS.keeper.x, POS.keeper.y - 8, false);
            return;
        }

        if (kind === 'run_out') {
            if (striker) pulseBatterRun(striker, 380);
            if (nonStriker) pulseBatterRun(nonStriker, 380);
            const throwSpot = pickShotDirection(hand, 2);
            const chaser = chooseFielderForLanding(throwSpot, 2, false);
            if (chaser) {
                panCameraTo(chaser, 'field', 1.1);
                pulsePose(chaser, 'pose-pickup-throw', 320);
                await animateBall(pitchHit, { x: chaser.x, y: chaser.y - 6 }, 260, 10);
                await animateBall({ x: chaser.x, y: chaser.y - 6 }, { x: POS.strikerStumps.x, y: POS.strikerStumps.y - 2 }, 190, 8);
                pulseStumpsHit();
                setBallPosition(POS.strikerStumps.x, POS.strikerStumps.y - 2, false);
            }
            return;
        }

        const landing = pickShotDirection(hand, 2);
        const catcher = chooseFielderForLanding(landing, 2, true);
        if (catcher) {
            panCameraTo(catcher, 'field', 1.1);
            pulsePose(catcher, 'pose-catch-high', 360);
            await animateBall(pitchHit, { x: catcher.x, y: catcher.y - 8 }, 290, 22);
            pulsePose(catcher, 'pose-celebrate', 800);
            setBallPosition(catcher.x, catcher.y - 8, false);
        }
    }

    async function playBallEvent(ballData, meta = {}) {
        if (!ballData) return;
        syncNames(ballData);
        setStatus(eventLabel(ballData));

        const striker = state.entities.striker;
        const nonStriker = state.entities.nonStriker;
        const keeper = state.entities.keeper;
        const umpire = state.entities.umpire;

        const hand = striker?.hand || inferHandFromData(ballData, ballData.striker);
        const release = { x: POS.bowler.x, y: POS.bowler.y + 6 };
        const pitchHit = { x: POS.striker.x, y: POS.striker.y - 10 };
        const scoreText = (meta.score !== undefined && meta.wickets !== undefined) ? `${meta.score}/${meta.wickets}` : '';

        await runBowlerSequence();
        await animateBall(release, pitchHit, 340, 12);

        if (ballData.extra_type === 'Wide') {
            if (keeper) pulsePose(keeper, 'pose-dive', 280);
            await animateBall(pitchHit, { x: POS.keeper.x, y: POS.keeper.y - 8 }, 240);
            setBallPosition(POS.keeper.x, POS.keeper.y - 8, false);
            return;
        }

        if (striker) pulseBatterShot(striker, 360);

        if (ballData.batter_out) {
            showEventBadge(`${eventLabel(ballData)}${scoreText ? ` | ${scoreText}` : ''}`, 'wicket');
            enqueueReplayHighlight('Wicket', POS.strikerStumps, 'wicket');
            await animateWicketFlow(inferWicketType(ballData, meta.commentary), pitchHit, hand);
            return;
        }

        if (ballData.runs === 6) {
            flashStage('six');
            pulseCrowd();
            playReplayStinger('Six');
            playAudioCue('six');
            showEventBadge(`SIX${scoreText ? ` | ${scoreText}` : ''}`, 'six');
            if (umpire) pulsePose(umpire, 'pose-gesture-six', 950);
            const sixTarget = pickShotDirection(hand, 6);
            enqueueReplayHighlight('Six', sixTarget, 'six');
            panCameraTo(sixTarget, 'six', 1.15);
            await animateBall(pitchHit, sixTarget, 560, 48);
            setBallPosition(POS.center.x, POS.center.y, false);
            return;
        }

        if (ballData.runs === 4) {
            flashStage('four');
            pulseCrowd();
            playReplayStinger('Four');
            playAudioCue('four');
            showEventBadge(`FOUR${scoreText ? ` | ${scoreText}` : ''}`, 'four');
            if (umpire) pulsePose(umpire, 'pose-gesture-four', 780);
            const fourTarget = pickShotDirection(hand, 4);
            enqueueReplayHighlight('Four', fourTarget, 'boundary');
            panCameraTo(fourTarget, 'boundary', 1.0);
            await animateBall(pitchHit, fourTarget, 460, 18);
            setBallPosition(POS.center.x, POS.center.y, false);
            return;
        }

        if (ballData.runs > 0) {
            if (striker) pulseBatterRun(striker, 340);
            if (nonStriker) pulseBatterRun(nonStriker, 340);
            const landing = pickShotDirection(hand, ballData.runs);
            const fielder = chooseFielderForLanding(landing, ballData.runs, false);
            if (fielder) {
                playAudioCue('run');
                panCameraTo(fielder, 'field', 0.7);
                showEventBadge(`${ballData.runs} RUN${ballData.runs > 1 ? 'S' : ''}${scoreText ? ` | ${scoreText}` : ''}`);
                if (ballData.runs <= 2) pulsePose(fielder, 'pose-pickup-throw', 300);
                else pulsePose(fielder, 'pose-dive', 260);
                await animateBall(pitchHit, { x: fielder.x, y: fielder.y - 6 }, 330, 10);
                setBallPosition(fielder.x, fielder.y - 6, false);
                return;
            }
        }

        playAudioCue('dot');
        showEventBadge(`DOT${scoreText ? ` | ${scoreText}` : ''}`);
        await animateBall(pitchHit, { x: POS.keeper.x, y: POS.keeper.y - 8 }, 280, 8);
        setBallPosition(POS.keeper.x, POS.keeper.y - 8, false);
    }

    function initMatchAnimation() {
        if (state.ready) return;
        const svg = document.getElementById('animation-ground-svg');
        const stage = document.getElementById('animation-stage');
        const statusEl = document.getElementById('animation-status');
        if (!svg || !stage || !statusEl) return;

        state.svg = svg;
        state.stage = stage;
        state.statusEl = statusEl;
        createGround(svg);
        ensureBroadcastUi();

        const striker = createPlayer('striker', 'batter', POS.striker.x, POS.striker.y, 'Striker');
        const nonStriker = createPlayer('nonStriker', 'batter', POS.nonStriker.x, POS.nonStriker.y, 'Non-Striker');
        const bowler = createPlayer('bowler', 'bowler', POS.bowler.x, POS.bowler.y, 'Bowler');
        const umpire = createPlayer('umpire', 'umpire', POS.umpire.x, POS.umpire.y, 'Umpire');
        const keeper = createPlayer('keeper', 'keeper', POS.keeper.x, POS.keeper.y, 'Keeper');
        const fielders = POS.fielders.map((p, idx) => {
            const f = createPlayer(`fielder-${idx + 1}`, 'fielder', p.x, p.y, p.short || `F${idx + 1}`);
            f.zoneName = p.name || `Fielder ${idx + 1}`;
            f.ring = p.ring || 'inner';
            return f;
        });
        const strikerStumps = createStumps(POS.strikerStumps.x, POS.strikerStumps.y);

        svg.appendChild(strikerStumps);
        [bowler, striker, nonStriker, keeper, umpire, ...fielders].forEach(p => svg.appendChild(p.g));

        const ball = createSvgEl('circle', { cx: '500', cy: '280', r: '4.2', class: 'anim-ball hidden' });
        svg.appendChild(ball);

        state.entities = { striker, nonStriker, bowler, umpire, keeper, ball, strikerStumps };
        state.fielders = fielders;
        state.ready = true;
    }

    function updateMatchAnimation(ballData, meta = {}) {
        initMatchAnimation();
        if (!state.ready || !ballData) return;
        state.queue = state.queue.then(() => playBallEvent(ballData, meta)).catch(() => null);
    }

    function refreshMatchAnimation(history) {
        initMatchAnimation();
        if (!state.ready) return;
        if (!Array.isArray(history) || history.length === 0) {
            setStatus('Waiting for toss...');
            return;
        }
        const latest = history[history.length - 1];
        syncNames(latest);
        setStatus(eventLabel(latest));
    }

    function resetMatchAnimationForNewInnings() {
        initMatchAnimation();
        if (!state.ready) return;
        setStatus('Innings break. Teams switching...');
        ['striker', 'nonStriker', 'bowler', 'keeper', 'umpire'].forEach(k => clearPoses(state.entities[k]));
        if (state.entities.striker) state.entities.striker.label.textContent = 'Striker';
        if (state.entities.nonStriker) state.entities.nonStriker.label.textContent = 'Non-Striker';
        if (state.entities.bowler) state.entities.bowler.label.textContent = 'Bowler';
        setBallPosition(POS.center.x, POS.center.y, false);
        state.replayQueue = [];
        renderReplayQueue();
    }

    function toggleMatchAnimationAudio() {
        state.audioEnabled = !state.audioEnabled;
        const ctx = ensureAudioContext();
        if (ctx && state.audioEnabled && ctx.state === 'suspended') {
            ctx.resume().catch(() => null);
        }
        return state.audioEnabled;
    }

    function toggleMatchAnimationSettingsPanel() {
        ensureBroadcastUi();
        if (!state.settingsPanelEl) return false;
        state.settingsPanelEl.classList.toggle('show');
        return state.settingsPanelEl.classList.contains('show');
    }

    window.initMatchAnimation = initMatchAnimation;
    window.updateMatchAnimation = updateMatchAnimation;
    window.refreshMatchAnimation = refreshMatchAnimation;
    window.resetMatchAnimationForNewInnings = resetMatchAnimationForNewInnings;
    window.toggleMatchAnimationAudio = toggleMatchAnimationAudio;
    window.toggleMatchAnimationSettingsPanel = toggleMatchAnimationSettingsPanel;
})();
