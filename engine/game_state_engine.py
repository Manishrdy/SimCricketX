"""
Game State Momentum Engine (GSME)
==================================

Computes a holistic game-state adjustment vector from the last 18 balls of
match history and applies it to raw outcome probability weights.

Inputs consumed
───────────────
• ball_history      : list[dict] – circular window of last ≤18 deliveries.
                      Each entry produced by _make_ball_event() in match.py:
                        {
                          'label':       str,   # 'Dot','Single','Double','Three',
                                                 # 'Four','Six','Wicket',
                                                 # 'Wide','NoBall','Byes','LegBye'
                          'runs':        int,   # bat-runs on the delivery
                          'is_wicket':   bool,
                          'is_boundary': bool,  # Four or Six (not extra)
                          'is_six':      bool,
                          'is_dot':      bool,  # zero bat-runs, not extra
                          'is_extra':    bool,
                        }
• score             : int   – current batting score
• current_over      : int   – 0-indexed over number (0–19)
• current_ball      : int   – 0-indexed ball within over (0–5)
• wickets           : int   – wickets fallen so far (0–10)
• innings           : int   – 1 or 2
• target            : int   – 2nd innings only; runs required to win
• pitch             : str   – 'Green'|'Dry'|'Hard'|'Flat'|'Dead'

Returns
───────
apply_game_state_to_probs() returns a new raw_weights dict with GSME
multipliers applied.  All values are clamped to a safe positive range.

Design principles
─────────────────
• GSME is a multiplicative post-processor; it cannot zero any outcome.
• No multiplier is allowed outside [MULT_MIN, MULT_MAX] = [0.35, 3.00].
• Extras are never adjusted – they reflect bowler error, not game state.
• All existing layers (pitch matrices, player ratings, phase boosts, pressure
  engine, scenario engine) remain intact and execute after GSME.
• The 18-ball window uses exponential decay (MOMENTUM_DECAY = 0.88) so the
  most-recent delivery carries the most weight.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# T20 par score curve – cumulative runs expected after N complete overs.
# Based on IPL / international T20 first-innings averages (neutral pitch).
# ---------------------------------------------------------------------------
_PAR_SCORES: dict = {
    0:   0.0,
    1:   7.0,
    2:  14.5,
    3:  22.0,
    4:  30.5,
    5:  38.5,
    6:  48.0,   # End of powerplay
    7:  55.5,
    8:  63.5,
    9:  72.0,
    10: 81.0,
    11: 90.0,
    12: 99.0,
    13: 108.0,
    14: 116.5,
    15: 125.5,
    16: 135.5,
    17: 146.5,
    18: 157.5,
    19: 167.5,
    20: 176.0,
}

# Per-pitch par-score adjustment factors (1.0 = neutral, >1 = high-scoring)
_PITCH_PAR_FACTOR: dict = {
    "Green": 0.88,
    "Dry":   0.88,
    "Hard":  1.00,
    "Flat":  1.14,
    "Dead":  1.22,
}

# Pitch-aware RRR baseline (Feature 15).
# Divides the required run-rate to produce a normalised aggression index.
# Low-scoring pitches (Green/Dry) need fewer runs/over to be "neutral",
# so a smaller baseline makes moderate RRRs feel more urgent.
# High-scoring pitches (Flat/Dead) need more runs/over to pressure batters.
_PITCH_RRR_BASELINE: dict = {
    "Green": 7.5,
    "Dry":   7.5,
    "Hard":  8.5,
    "Flat":  10.5,
    "Dead":  11.5,
}

# ---------------------------------------------------------------------------
# Momentum delta per outcome label
# ---------------------------------------------------------------------------
_MOMENTUM_DELTA: dict = {
    "Six":     25.0,
    "Four":    15.0,
    "Three":    8.0,
    "Double":   5.0,
    "Single":   2.0,
    "Dot":     -5.0,
    "Wicket": -30.0,
    "Wide":     3.0,   # Free ball — batting advantage
    "NoBall":   3.0,
    "Byes":     1.0,
    "LegBye":   1.0,
}

MOMENTUM_DECAY = 0.88   # Weight of most-recent ball = 1.0; 18th-back ≈ 0.10
BALL_HISTORY_WINDOW = 18

# ---------------------------------------------------------------------------
# Safety clamp limits for all GSME multipliers
# ---------------------------------------------------------------------------
MULT_MIN = 0.35
MULT_MAX = 3.00


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _par_score_at(over: int, ball: int, pitch: str = "Hard") -> float:
    """Interpolate pitch-adjusted expected score at an exact over.ball point."""
    base_at    = _PAR_SCORES.get(over,     _PAR_SCORES[20])
    base_next  = _PAR_SCORES.get(over + 1, _PAR_SCORES[20])
    fraction   = ball / 6.0
    base_score = base_at + fraction * (base_next - base_at)
    factor     = _PITCH_PAR_FACTOR.get(pitch, 1.0)
    return base_score * factor


def _compute_momentum(history: list) -> float:
    """
    Weighted momentum score from up to the last BALL_HISTORY_WINDOW deliveries.
    Most-recent delivery has weight 1.0; oldest has weight MOMENTUM_DECAY^(n-1).
    Returns a value in [−100, +100].
    """
    if not history:
        return 0.0

    raw = 0.0
    max_possible = 0.0
    min_possible = 0.0
    n = len(history)

    for i, event in enumerate(history):
        age    = n - 1 - i                      # 0 = most recent
        weight = MOMENTUM_DECAY ** age
        label  = event.get("label", "Dot")
        delta  = _MOMENTUM_DELTA.get(label, 0.0)
        raw          += delta  * weight
        max_possible += 25.0   * weight          # best possible = all sixes
        min_possible -= 30.0   * weight          # worst possible = all wickets

    # Normalise to [-100, +100]
    if raw >= 0 and max_possible > 0:
        score = (raw / max_possible) * 100.0
    elif raw < 0 and min_possible < 0:
        score = (raw / abs(min_possible)) * 100.0
    else:
        score = 0.0

    return max(-100.0, min(100.0, score))


def _count_tail(history: list, labels: set, window: int = BALL_HISTORY_WINDOW) -> int:
    """
    Count consecutive matching events at the END (tail) of the history window.
    Stops at the first non-matching entry.
    """
    tail  = history[-window:] if len(history) >= window else history
    count = 0
    for event in reversed(tail):
        if event.get("label") in labels:
            count += 1
        else:
            break
    return count


def _count_in_window(history: list, labels: set,
                     window: int = BALL_HISTORY_WINDOW) -> int:
    """Total occurrences of any label from `labels` in the last `window` balls."""
    tail = history[-window:] if len(history) >= window else history
    return sum(1 for e in tail if e.get("label") in labels)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Public: build the game-state descriptor
# ---------------------------------------------------------------------------

def compute_game_state_vector(
    ball_history:     list,
    score:            int,
    current_over:     int,
    current_ball:     int,
    wickets:          int,
    innings:          int,
    target:           int  = 0,
    pitch:            str  = "Hard",
    partnership_balls: int = 0,
    scenario_phase:   str  = "inactive",
) -> dict:
    """
    Compute the full game-state descriptor for the CURRENT delivery.
    `ball_history` contains the previous ≤18 deliveries (NOT the current one).

    Returns a dict with all intermediate values for transparency / logging.
    """
    history = ball_history or []

    # ── 1. Momentum ──────────────────────────────────────────────────────────
    momentum = _compute_momentum(history)

    # ── 2. Run-rate context ──────────────────────────────────────────────────
    par = _par_score_at(current_over, current_ball, pitch)

    # First innings: how the team is doing relative to par
    rr_ratio = (score / par) if par > 0.0 else 1.0   # >1 ahead, <1 behind

    # Second innings: required-run-rate based aggression index
    balls_remaining = (20 - current_over) * 6 - current_ball
    if innings == 2 and balls_remaining > 0 and target > 0:
        runs_needed        = max(0, target - score)
        overs_left         = balls_remaining / 6.0
        rrr                = runs_needed / overs_left
        # Feature 15: pitch-aware RRR baseline — different pitches have
        # different "neutral" run rates, so we scale the aggression index
        # relative to what's achievable on that surface.
        rrr_baseline        = _PITCH_RRR_BASELINE.get(pitch, 9.0)
        required_aggression = rrr / rrr_baseline
    else:
        rrr                 = 0.0
        required_aggression = 1.0

    # ── 3. Resources remaining ───────────────────────────────────────────────
    balls_remaining_clamped = max(0, balls_remaining)
    wickets_in_hand         = max(0, 10 - wickets)
    # resource_index ≈ 1.0 at match start, → 0 as wickets/balls exhaust
    resource_index = (balls_remaining_clamped / 120.0) * (wickets_in_hand / 10.0)

    # ── 4. Collapse risk from the 18-ball window ─────────────────────────────
    recent_wickets_18  = _count_in_window(history, {"Wicket"}, BALL_HISTORY_WINDOW)
    _collapse_table    = {0: 1.00, 1: 1.135, 2: 1.315, 3: 1.495, 4: 1.630, 5: 1.765}
    collapse_multiplier = _collapse_table.get(min(recent_wickets_18, 5), 1.85)

    # ── 5. Tail-pattern detectors (run on the full 18-ball window) ────────────
    consecutive_dots        = _count_tail(history, {"Dot"},          BALL_HISTORY_WINDOW)
    consecutive_wickets     = _count_tail(history, {"Wicket"},       BALL_HISTORY_WINDOW)
    consecutive_boundaries  = _count_tail(history, {"Four", "Six"},  BALL_HISTORY_WINDOW)

    # Count dot-ball ratio in the window (heat-map of bowler domination)
    window_size  = len(history[-BALL_HISTORY_WINDOW:]) if history else 1
    dot_count    = _count_in_window(history, {"Dot"}, BALL_HISTORY_WINDOW)
    dot_ratio    = dot_count / window_size if window_size > 0 else 0.0

    state = {
        # Core momentum
        "momentum":               momentum,

        # Run-rate context
        "rr_ratio":               rr_ratio,
        "required_aggression":    required_aggression,
        "rrr":                    rrr,

        # Resources
        "resource_index":         resource_index,
        "balls_remaining":        balls_remaining_clamped,
        "wickets_in_hand":        wickets_in_hand,

        # Collapse/cluster risk
        "collapse_multiplier":    collapse_multiplier,
        "recent_wickets_18":      recent_wickets_18,

        # Tail patterns
        "consecutive_dots":       consecutive_dots,
        "consecutive_wickets":    consecutive_wickets,
        "consecutive_boundaries": consecutive_boundaries,
        "dot_ratio":              dot_ratio,

        # Context
        "innings":                innings,
        "pitch":                  pitch,

        # Feature 6: current partnership length (balls batted together)
        "partnership_balls":      partnership_balls,

        # Scenario steering phase — used to dampen collapse layers during convergence
        "scenario_phase":         scenario_phase,
    }

    logger.debug(
        "GSME state | momentum=%.1f  rr_ratio=%.2f  req_agg=%.2f  "
        "resource=%.2f  collapse=%.2f  c_dots=%d  c_W=%d  c_bdry=%d",
        momentum, rr_ratio, required_aggression, resource_index,
        collapse_multiplier, consecutive_dots, consecutive_wickets,
        consecutive_boundaries,
    )

    return state


# ---------------------------------------------------------------------------
# Public: apply game-state multipliers to raw outcome weights
# ---------------------------------------------------------------------------

def apply_game_state_to_probs(raw_weights: dict, state: dict) -> dict:
    """
    Apply GSME multipliers to a raw_weights dict and return the adjusted dict.

    The original dict is NOT mutated.  All multipliers are independently
    clamped to [MULT_MIN, MULT_MAX] before application, and the final
    per-outcome weights are floored at 1e-9 to prevent zero probabilities.

    Application order (all multiplicative):
      A. Momentum              (Four, Six, Wicket, Dot)
      B. Collapse risk         (Wicket ↑; boundary ↓; Dot ↑)
      C. Run-rate pressure     (1st innings: catch-up vs ahead-of-par)
      D. Required aggression   (2nd innings: rrr-driven slog vs grind)
      E. Pattern overrides
           E1. Frustration dots  (batsman tempted to explode)
           E2. Boundary streak   (bowler counter-adjustment / field change)
           E3. Consecutive wickets (psychological collapse amplifier)
      F. Resource conservatism (protect wickets when resources are thin)
      G. Partnership bonus     (Feature 6 — set partnership confidence boost)
    """
    OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six",
                "Wicket", "Extras")

    # Initialise all multipliers at 1.0 (identity)
    mults: dict = {o: 1.0 for o in OUTCOMES}

    # Unpack state values (safe defaults for every key)
    momentum              = state.get("momentum",              0.0)
    rr_ratio              = state.get("rr_ratio",              1.0)
    required_aggression   = state.get("required_aggression",   1.0)
    resource_index        = state.get("resource_index",        0.5)
    collapse_multiplier   = state.get("collapse_multiplier",   1.0)
    consecutive_dots      = state.get("consecutive_dots",      0)
    consecutive_wickets   = state.get("consecutive_wickets",   0)
    consecutive_boundaries= state.get("consecutive_boundaries",0)
    innings               = state.get("innings",               1)
    wickets_in_hand       = state.get("wickets_in_hand",       10)
    rrr                   = state.get("rrr",                   0.0)
    scenario_phase        = state.get("scenario_phase",        "inactive")

    # ── A. MOMENTUM ──────────────────────────────────────────────────────────
    mom = momentum / 100.0       # [-1.0, +1.0]

    if mom > 0:
        # Batting in flow: more runs flow freely, wicket risk eases slightly
        mults["Four"]   *= 1.0 + mom * 0.25      # up to +25 %
        mults["Six"]    *= 1.0 + mom * 0.32      # sixes amplify a touch more
        mults["Double"] *= 1.0 + mom * 0.12
        mults["Single"] *= 1.0 + mom * 0.06
        mults["Wicket"] *= 1.0 - mom * 0.10      # up to -10 %
        mults["Dot"]    *= 1.0 - mom * 0.14      # up to -14 %
    else:
        # Batting out of rhythm: dots accumulate, wicket danger rises
        mults["Four"]   *= 1.0 + mom * 0.22      # mom<0 → reduction
        mults["Six"]    *= 1.0 + mom * 0.22
        mults["Wicket"] *= 1.0 - mom * 0.20      # mom<0 → increase
        mults["Dot"]    *= 1.0 - mom * 0.16      # mom<0 → increase

    # ── B. COLLAPSE RISK ─────────────────────────────────────────────────────
    # During scenario convergence (overs 15–17), cap the collapse multiplier so
    # the scenario engine's wicket steering can operate without being overwhelmed.
    _effective_cm = collapse_multiplier
    if scenario_phase == "convergence":
        _effective_cm = min(collapse_multiplier, 1.20)
    if _effective_cm > 1.0:
        excess = _effective_cm - 1.0             # 0.0 → 0.85 range
        mults["Wicket"] *= _effective_cm
        mults["Dot"]    *= 1.0 + excess * 0.40
        mults["Four"]   *= _clamp(1.0 - excess * 0.32, 0.50, 1.0)
        mults["Six"]    *= _clamp(1.0 - excess * 0.38, 0.45, 1.0)

    # ── C. RUN-RATE PRESSURE — FIRST INNINGS ─────────────────────────────────
    if innings == 1:
        if rr_ratio < 0.72:
            # Well behind par — desperate acceleration
            mults["Four"]   *= 1.28
            mults["Six"]    *= 1.40
            mults["Wicket"] *= 1.14   # Risk being taken
            mults["Dot"]    *= 0.83
            mults["Single"] *= 0.92   # Fewer dot-and-a-single, go big
        elif rr_ratio < 0.88:
            # Slightly behind — nudge the rate up
            mults["Four"]   *= 1.14
            mults["Six"]    *= 1.20
            mults["Wicket"] *= 1.07
            mults["Dot"]    *= 0.91
        elif rr_ratio > 1.30:
            # Comfortably ahead — bat conservatively, keep wickets
            mults["Four"]   *= 0.86
            mults["Six"]    *= 0.82
            mults["Wicket"] *= 0.88
            mults["Dot"]    *= 1.10
            mults["Single"] *= 1.08
        elif rr_ratio > 1.15:
            # Slightly ahead — modest conservatism
            mults["Four"]   *= 0.93
            mults["Six"]    *= 0.90
            mults["Wicket"] *= 0.93
            mults["Dot"]    *= 1.04

    # ── D. REQUIRED AGGRESSION — SECOND INNINGS ──────────────────────────────
    if innings == 2:
        if required_aggression < 0.67:
            # Very comfortable chase (rrr < ~6) — rotate and grind
            mults["Wicket"] *= 0.86
            mults["Four"]   *= 0.92
            mults["Six"]    *= 0.88
            mults["Single"] *= 1.10
            mults["Dot"]    *= 0.94

        elif required_aggression < 0.89:
            # Comfortable (rrr 6–8) — normal, light touch
            mults["Wicket"] *= 0.94
            mults["Four"]   *= 0.96
            mults["Single"] *= 1.05

        elif required_aggression < 1.11:
            # Neutral zone (rrr ~8–10) — no adjustment
            pass

        elif required_aggression < 1.34:
            # Moderate pressure (rrr ~10–12) — step on the gas
            mults["Four"]   *= 1.12
            mults["Six"]    *= 1.18
            mults["Wicket"] *= 1.10
            mults["Dot"]    *= 0.90
            mults["Single"] *= 0.94

        elif required_aggression < 1.67:
            # High pressure (rrr ~12–15) — full aggression
            mults["Four"]   *= 1.26
            mults["Six"]    *= 1.38
            mults["Wicket"] *= 1.22
            mults["Dot"]    *= 0.80
            mults["Single"] *= 0.85
            mults["Double"] *= 0.92

        elif required_aggression < 2.00:
            # Near-impossible (rrr ~15–18) — swinging for the fences
            mults["Six"]    *= 1.60
            mults["Four"]   *= 1.38
            mults["Wicket"] *= 1.38
            mults["Dot"]    *= 0.70
            mults["Single"] *= 0.76
            mults["Double"] *= 0.88

        else:
            # Absolutely impossible (rrr 18+) — last-gasp slog-fest
            mults["Six"]    *= 1.90
            mults["Four"]   *= 1.50
            mults["Wicket"] *= 1.55
            mults["Dot"]    *= 0.62
            mults["Single"] *= 0.68

    # ── E. PATTERN OVERRIDES ─────────────────────────────────────────────────

    # E1. Frustration dots — batsman tempted to break free
    if consecutive_dots >= 8:
        # Extended dot-ball siege → desperate wild swing
        mults["Six"]    *= 1.60
        mults["Four"]   *= 1.35
        mults["Wicket"] *= 1.35
        mults["Dot"]    *= 0.72
    elif consecutive_dots >= 6:
        mults["Six"]    *= 1.40
        mults["Four"]   *= 1.24
        mults["Wicket"] *= 1.24
        mults["Dot"]    *= 0.80
    elif consecutive_dots >= 4:
        mults["Four"]   *= 1.14
        mults["Six"]    *= 1.20
        mults["Wicket"] *= 1.14
        mults["Dot"]    *= 0.88
    elif consecutive_dots >= 2:
        mults["Four"]   *= 1.06
        mults["Six"]    *= 1.08
        mults["Wicket"] *= 1.04
        mults["Dot"]    *= 0.95

    # E2. Boundary streak — bowler counter-adjusts, field changes
    # NOTE: 2-consecutive boundary penalty is already in compute_weighted_prob();
    #       GSME extends it beyond 2 consecutive boundaries.
    if consecutive_boundaries >= 5:
        mults["Four"]   *= 0.60
        mults["Six"]    *= 0.58
        mults["Wicket"] *= 1.42
        mults["Dot"]    *= 1.30
    elif consecutive_boundaries >= 4:
        mults["Four"]   *= 0.68
        mults["Six"]    *= 0.65
        mults["Wicket"] *= 1.36
        mults["Dot"]    *= 1.22
    elif consecutive_boundaries >= 3:
        mults["Four"]   *= 0.78
        mults["Six"]    *= 0.75
        mults["Wicket"] *= 1.26
        mults["Dot"]    *= 1.12

    # E3. Consecutive wickets — psychological collapse amplifier
    # During scenario convergence, halve the excess so scenario steering can steer
    # wicket count without being overwhelmed by the cascade.
    _cw_dampen = 0.5 if scenario_phase == "convergence" else 1.0
    if consecutive_wickets >= 4:
        mults["Wicket"] *= 1.0 + 0.495 * _cw_dampen   # normal: 1.495 | convergence: 1.248
        mults["Dot"]    *= 1.32
        mults["Four"]   *= 0.68
        mults["Six"]    *= 0.62
    elif consecutive_wickets >= 3:
        mults["Wicket"] *= 1.0 + 0.405 * _cw_dampen   # normal: 1.405 | convergence: 1.203
        mults["Dot"]    *= 1.25
        mults["Four"]   *= 0.74
        mults["Six"]    *= 0.70
    elif consecutive_wickets >= 2:
        mults["Wicket"] *= 1.0 + 0.270 * _cw_dampen   # normal: 1.270 | convergence: 1.135
        mults["Dot"]    *= 1.16
        mults["Four"]   *= 0.82
        mults["Six"]    *= 0.78
    elif consecutive_wickets == 1:
        # New batsman just in — small additional collapse-fear on top of
        # the new-batter vulnerability already in compute_weighted_prob()
        mults["Wicket"] *= 1.0 + 0.108 * _cw_dampen   # normal: 1.108 | convergence: 1.054

    # ── F. RESOURCE CONSERVATISM ─────────────────────────────────────────────
    # When resources are very thin (tail in, or near the end), protect wickets
    # UNLESS the game situation demands all-out aggression.
    if resource_index < 0.12 and wickets_in_hand <= 3:
        # Tail exposed, or virtually all-out
        survival_needed = (innings == 1) or (innings == 2 and required_aggression < 1.40)
        if survival_needed:
            mults["Wicket"] *= 0.78
            mults["Six"]    *= 0.80
            mults["Single"] *= 1.18
            mults["Dot"]    *= 1.08

    elif resource_index < 0.25 and wickets_in_hand <= 5:
        if innings == 1 or (innings == 2 and required_aggression < 1.25):
            mults["Wicket"] *= 0.86
            mults["Single"] *= 1.10

    # ── G. PARTNERSHIP BONUS (Feature 6) ─────────────────────────────────────
    # A well-set partnership gives batsmen confidence: they know each other's
    # running, read the bowling better, and are more willing to play strokes.
    pb = state.get("partnership_balls", 0)
    if pb >= 80:
        _p_bonus = 1.10
    elif pb >= 50:
        _p_bonus = 1.07
    elif pb >= 30:
        _p_bonus = 1.04
    else:
        _p_bonus = 1.00

    if _p_bonus > 1.0:
        for _o in ("Four", "Six", "Double", "Three"):
            if _o in mults:
                mults[_o] *= _p_bonus

    # ── SAFETY: hard-clamp every multiplier ───────────────────────────────────
    for key in mults:
        mults[key] = _clamp(mults[key], MULT_MIN, MULT_MAX)

    # Apply to raw_weights (floor each adjusted weight at tiny positive value)
    adjusted: dict = {}
    for outcome, weight in raw_weights.items():
        mult              = mults.get(outcome, 1.0)
        adjusted[outcome] = max(weight * mult, 1e-9)

    logger.debug(
        "GSME mults | Dot=%.3f  1=%.3f  2=%.3f  3=%.3f  4=%.3f  "
        "6=%.3f  W=%.3f  X=%.3f",
        mults["Dot"], mults["Single"], mults["Double"], mults["Three"],
        mults["Four"], mults["Six"], mults["Wicket"], mults["Extras"],
    )

    return adjusted


# ---------------------------------------------------------------------------
# Factory: build a ball event dict from a resolved ball outcome
# Used by match.py to append to self.ball_history after each delivery.
# ---------------------------------------------------------------------------

def make_ball_event(outcome: dict) -> dict:
    """
    Convert a resolved ball outcome dict (from calculate_outcome / match.py)
    into the lightweight event format stored in Match.ball_history.

    Parameters
    ----------
    outcome : dict  – the outcome dict returned by calculate_outcome().
                      Expected keys: 'runs', 'batter_out', 'is_extra',
                      'extra_type' (optional).

    Returns
    -------
    dict with keys: label, runs, is_wicket, is_boundary, is_six,
                    is_dot, is_extra
    """
    runs       = outcome.get("runs", 0)
    is_wicket  = bool(outcome.get("batter_out", False))
    is_extra   = bool(outcome.get("is_extra",   False))
    extra_type = outcome.get("extra_type", "")

    # Determine the canonical label
    if is_wicket:
        label = "Wicket"
    elif is_extra:
        if extra_type == "Wide":
            label = "Wide"
        elif extra_type == "No Ball":
            label = "NoBall"
        elif extra_type == "Byes":
            label = "Byes"
        else:
            label = "LegBye"
    else:
        _run_to_label = {0: "Dot", 1: "Single", 2: "Double",
                         3: "Three", 4: "Four", 6: "Six"}
        label = _run_to_label.get(runs, "Single")   # fallback to Single

    # Bat-runs: for extras, bat_runs may be on the sub-delivery key
    bat_runs   = outcome.get("bat_runs", runs if not is_extra else 0)
    is_boundary = (not is_extra) and (not is_wicket) and (runs in (4, 6))
    is_six      = (not is_extra) and (not is_wicket) and (runs == 6)
    is_dot      = (not is_extra) and (not is_wicket) and (bat_runs == 0)

    return {
        "label":       label,
        "runs":        bat_runs,
        "is_wicket":   is_wicket,
        "is_boundary": is_boundary,
        "is_six":      is_six,
        "is_dot":      is_dot,
        "is_extra":    is_extra,
    }
