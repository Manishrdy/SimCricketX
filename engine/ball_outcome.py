import random
import logging
from typing import Optional
from engine.ground_config import (
    get_defaults as _gc_defaults,
    get_scoring_matrix as _gc_scoring_matrix,
    get_run_factor as _gc_run_factor,
    get_wicket_factors as _gc_wicket_factors,
    get_phase_boosts as _gc_phase_boosts,
    get_blending_weights as _gc_blending_weights,
    get_lista_matrix as _gc_lista_matrix,
    get_lista_run_factor as _gc_lista_run_factor,
    get_lista_wicket_mult as _gc_lista_wicket_mult,
    get_lista_wicket_factor_for as _gc_lista_wicket_factor_for,
    get_lista_dot_single as _gc_lista_dot_single,
    get_lista_fine_tune as _gc_lista_fine_tune,
    get_lista_phase_boosts as _gc_lista_phase_boosts,
    get_lista_pitch_wear as _gc_lista_pitch_wear,
    get_lista_dew as _gc_lista_dew,
    get_fc_scoring_matrix as _gc_fc_scoring_matrix,
    get_fc_run_factor as _gc_fc_run_factor,
    get_fc_wicket_factor_for as _gc_fc_wicket_factor_for,
    get_fc_pitch_wear as _gc_fc_pitch_wear,
    get_fc_ball_condition_factor as _gc_fc_ball_condition_factor,
    get_fc_rough_targeting_factor as _gc_fc_rough_targeting_factor,
)
from engine.game_state_engine import apply_game_state_to_probs
from engine.format_config import FormatConfig

logger = logging.getLogger(__name__)

# Tune extras frequency to target ~3-6 extras per innings (120 balls).
EXTRA_ERROR_FLOOR = 0.30
EXTRA_WEIGHT_MULTIPLIER = 2.2

# Free hit boundary boost applied independently to Four and Six weights.
FREE_HIT_BOUNDARY_BOOST = 1.10

# -----------------------------------------------------------------------------
# ball_outcome.py
#
# Implements ball-by-ball outcome logic with:
#   • 60% pitch-influence + 40% player-skill blending
#   • Detailed commentary templates
#   • Enhanced boundary & wicket chances in the final 4 overs (17–20)
#
# Pitch target bands (T20 context) — recalibrated 2026-08-16. The numbers the
# engine actually produces are measured by tests/test_scoring_calibration.py;
# these are the bands it is tuned to hit, not aspirations.
#   - Green: 110–150 runs, 7–10 wkts (pace takes ~75% of the wickets)
#   - Dry  : 110–150 runs, 7–10 wkts (spin takes ~59% off ~40% of the overs)
#   - Hard : 180–220 runs, 5–7 wkts (balanced, slight batting edge)
#   - Flat : 200–230 runs, 3–5 wkts (batting paradise)
#   - Dead : 230+   runs, 1–2 wkts (batting festival; almost nothing for bowlers)
#
# The logic below ensures:
#   – Pitch contributes 60% to each outcome probability
#   – Player ratings (batting, bowling, fielding) contribute 40%
#   – In overs 17–20, boundary (4s/6s) chances and wicket chances are boosted
#     based on pitch type:
#       * Flat/Dead: highest boundary boost (aim ~3 boundaries/over)
#       * Hard       : moderate boundary boost (aim ~2 boundaries/over)
#       * Green/Dry  : minimal boundary boost (max ~1 boundary/over)
#     Wicket chance also increases slightly in these death overs.
#
# Print-based logging is included to trace computations at each step.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 1) Commentary templates for each outcome category
# -----------------------------------------------------------------------------
# 1) Commentary templates - REDUCED FOR MEMORY (Data moved to data/commentary_pack.json)
commentary_templates = {
    "Dot": ["Dot ball."],
    "Single": ["One run."],
    "Double": ["Two runs."],
    "Three": ["Three runs."],
    "Four": ["Four runs."],
    "Six": ["Six runs!"],
    "Wicket": ["Out!"],
    "Extras": ["Extra run."]
}

# -----------------------------------------------------------------------------
# 2) Pitch-influence definitions
# -----------------------------------------------------------------------------
# These are read-only VIEWS over the factory defaults in
# config/ground_conditions_defaults.yaml — not a second copy to edit. They used
# to be hand-maintained literals that had drifted well away from the config
# (Flat Four was 0.18 here vs 0.13 there), which meant the "fallback" values
# silently re-tuned any match that took the fallback path. Edit the YAML.
#
# Per-user overrides never come through here; they arrive as the `config`
# argument on the accessors below.

def _t20_defaults():
    return _gc_defaults("T20")


PITCH_RUN_FACTOR = {
    pitch: profile.get("run_factor", 1.0)
    for pitch, profile in (_t20_defaults().get("pitch_profiles") or {}).items()
}

PITCH_WICKET_FACTOR = {
    pitch: dict(profile.get("wicket_factors") or {})
    for pitch, profile in (_t20_defaults().get("pitch_profiles") or {}).items()
}

PITCH_SCORING_MATRIX = {
    pitch: dict(profile.get("scoring_matrix") or {})
    for pitch, profile in (_t20_defaults().get("pitch_profiles") or {}).items()
}

def get_pitch_run_multiplier(pitch: str, config=None) -> float:
    """
    Returns the run-friendly multiplier for the given pitch.
    Pass *config* to use a user-specific snapshot instead of the defaults.
    """
    factor = _gc_run_factor(pitch, config=config)
    if factor is None:
        factor = PITCH_RUN_FACTOR.get(pitch, 1.0)
    return factor


def get_pitch_wicket_multiplier(pitch: str, bowling_type: str, config=None) -> float:
    """
    Returns the wicket-friendly multiplier for the given pitch and bowling type.
    Pass *config* to use a user-specific snapshot instead of the defaults.
    """
    wf = _gc_wicket_factors(pitch, config=config)
    if wf:
        return wf.get(bowling_type, wf.get("default", 1.0))
    slot = PITCH_WICKET_FACTOR.get(pitch, {})
    return slot.get(bowling_type, slot.get("default", 1.0))


# Fallback matrix for a pitch name that exists in no profile at all.
DEFAULT_SCORING_MATRIX = {
    "Dot":     0.27,
    "Single":  0.352,
    "Double":  0.13,
    "Three":   0.008,  # ~1 three per innings
    "Four":    0.09,
    "Six":     0.05,
    "Wicket":  0.05,
    "Extras":  0.05
    # Sum: ~1.00
}


# =============================================================================
# LIST A (50-OVER) TUNING
# =============================================================================
# Like the T20 block above, these are read-only VIEWS over
# config/ground_conditions_defaults.yaml. ListA tuning used to live here as
# hardcoded literals that the ground-conditions editor could not reach, so a
# user customising their pitches changed nothing about their ListA matches.
#
# Phase selection is driven by FormatConfig.get_phase(over).
# Target profile (Hard pitch, 1st innings, neutral): PP1 ~62, Middle ~126,
# Death ~102 → ~290 runs.
#
# Per-user overrides arrive as the `config` argument on the functions below;
# these module-level views are the no-config fallback only.
# =============================================================================

def _lista_defaults():
    return _gc_defaults("ListA")


LISTA_PP1_MATRIX = dict(_gc_lista_matrix("pp1") or {})
LISTA_MIDDLE_MATRIX = dict(_gc_lista_matrix("middle") or {})
LISTA_DEATH_MATRIX = dict(_gc_lista_matrix("death") or {})

# Per-pitch run scaling, applied on top of the phase matrices. Must stay
# consistent with _LISTA_PITCH_PAR_FACTORS in format_config.py.
LISTA_RUN_FACTORS = {
    pitch: profile.get("run_factor", 1.0)
    for pitch, profile in (_lista_defaults().get("pitch_profiles") or {}).items()
}

LISTA_WICKET_PITCH_MULT = {
    pitch: profile.get("wicket_mult", 1.0)
    for pitch, profile in (_lista_defaults().get("pitch_profiles") or {}).items()
}

LISTA_DOT_SINGLE_FACTORS = {
    pitch: dict(profile.get("dot_single") or {})
    for pitch, profile in (_lista_defaults().get("pitch_profiles") or {}).items()
}

LISTA_PITCH_FINE_TUNE = {
    pitch: dict(profile.get("fine_tune") or {})
    for pitch, profile in (_lista_defaults().get("pitch_profiles") or {}).items()
}

LISTA_PHASE_BOOSTS = _gc_lista_phase_boosts()
LISTA_PITCH_WEAR = _gc_lista_pitch_wear()
LISTA_DEW = _gc_lista_dew()


def _scale_outcomes(w: dict, multipliers: dict) -> None:
    """Multiply outcomes in *w* in place. Shared by the ListA boost layers."""
    for outcome, mult in multipliers.items():
        w[outcome] = w.get(outcome, 0) * mult


def _renormalise_to(original: dict, adjusted: dict) -> dict:
    """Rescale *adjusted* so its total matches *original*'s."""
    orig_total = sum(original.values())
    new_total = sum(adjusted.values())
    if new_total > 0 and orig_total > 0:
        scale = orig_total / new_total
        return {k: v * scale for k, v in adjusted.items()}
    return adjusted


def _lista_phase_key(over: int, fmt) -> str:
    """Phase name for an over, driven by the format definition rather than
    hardcoded over ranges. Shared by the matrix and phase-boost lookups."""
    if fmt.is_death(over):
        return "death"
    if fmt.is_powerplay(over):
        return "pp1"
    return "middle"


def _get_lista_matrix(over: int, fmt, config=None) -> dict:
    """Return the ListA scoring matrix for the given over."""
    return _gc_lista_matrix(_lista_phase_key(over, fmt), config=config) or {}


def _apply_lista_phase_boosts(weights: dict, over: int, pitch: str,
                               innings: int, fmt, config=None) -> dict:
    """
    Apply mild ListA phase nudges to raw outcome weights.

    The phase character is already embedded in the three ListA matrices
    (PP1 / Middle / Death).  These boosts are small pitch-sensitive adjustments
    on top, not primary scoring drivers.

    PP1   : Slight boundary edge from new ball on friendly pitches.
    Middle: Tiny dot-ball nudge (spinners tightening).
    Death : Modest boundary/wicket uplift; 2nd-innings pressure handling.
    """
    w = dict(weights)

    boosts = _gc_lista_phase_boosts(config=config).get(_lista_phase_key(over, fmt), {})

    # Pitch-specific first, then the every-pitch layer, then 2nd-innings
    # pressure — the order compounds (death Wicket takes both 1.05 and 1.08).
    _scale_outcomes(w, boosts.get("pitch", {}).get(pitch, {}))
    _scale_outcomes(w, boosts.get("all", {}))
    if innings == 2:
        _scale_outcomes(w, boosts.get("second_innings", {}))

    # Dead is a batting paradise (run_factor 1.18, wicket_mult 0.58).
    # No further suppression applied here; the phase matrices and scaling
    # layers already produce the correct low-dot, low-wicket, high-boundary profile.

    return w


def _apply_lista_pitch_wear(weights: dict, pitch: str,
                             pitch_wear: float, config=None) -> dict:
    """
    Progressive pitch wear for ListA (0-300 balls, normalised to [0,1]).

    Unlike T20 (where wear is mild), ListA wear has a pronounced late phase:
      - Green : seam fades after over 20 (wear ~0.40). Batting improves.
      - Dry   : spin gets genuinely unplayable by over 30+ (wear ~0.60).
                Wickets and dots escalate sharply.
      - Hard  : modest steady deterioration across the innings.
      - Flat/Dead: minor wear; pitch remains batting-friendly throughout.

    pitch_wear is the fraction of total balls already bowled (0=fresh, 1=done).
    """
    if pitch_wear <= 0.0:
        return weights

    w = dict(weights)
    pw = pitch_wear

    spec = _gc_lista_pitch_wear(config=config).get(pitch)
    if spec:
        if spec.get("mode") == "late":
            threshold = spec.get("threshold", 0.0)
            # No effect until the pitch has worn past the threshold, then
            # ramps 0→1 across the remainder of the innings.
            ramp = ((pw - threshold) / (1.0 - threshold)) if pw > threshold else 0.0
        else:
            ramp = pw

        if ramp:
            _scale_outcomes(w, {outcome: 1.0 + coef * ramp
                                for outcome, coef in spec.get("factors", {}).items()})

    # Re-normalise to preserve total weight
    return _renormalise_to(weights, w)


def _apply_fc_pitch_wear(weights: dict, pitch: str,
                          pitch_wear: float, config=None) -> dict:
    """
    General (bowling-style-agnostic) pitch wear for First-Class matches,
    applied against the CONTINUOUS match-long wear scalar (0=fresh,
    1=fully worn after `days * overs_per_day` overs — see
    Match.match_balls_bowled), not a per-innings one. Mirrors
    _apply_lista_pitch_wear's mode/threshold/factors shape exactly, reading
    from the FC pitch_wear YAML block instead of ListA's.

    This is necessary but not sufficient on its own — the wear-interpolated
    bowling-style wicket factor (_gc_fc_wicket_factor_for, applied
    separately as a Wicket-only scale in calculate_outcome) is what actually
    makes the pitch favor different bowler types as it wears; this function
    only handles the overall Dot/Four/Wicket drift.
    """
    if pitch_wear <= 0.0:
        return weights

    w = dict(weights)
    pw = pitch_wear

    spec = _gc_fc_pitch_wear(config=config).get(pitch)
    if spec:
        if spec.get("mode") == "late":
            threshold = spec.get("threshold", 0.0)
            ramp = ((pw - threshold) / (1.0 - threshold)) if pw > threshold else 0.0
        else:
            ramp = pw

        if ramp:
            _scale_outcomes(w, {outcome: 1.0 + coef * ramp
                                for outcome, coef in spec.get("factors", {}).items()})

    return _renormalise_to(weights, w)


def _apply_dew_factor(weights: dict, innings: int, over: int,
                      is_day_night: bool, fmt, config=None) -> dict:
    """
    Dew factor for Day-Night ListA matches (floodlit evening conditions).

    Physics: surface moisture makes the ball slippery after ~25 overs of the
    2nd innings. Spin grips less, pace loses control (more wides), batting
    becomes easier — classic ODI D/N swing to the chasing side.

    Effect kicks in from the configured start over of the 2nd innings in D/N
    matches, ramping linearly to full intensity at the peak over. Defaults are
    overs 25 → 45 with Extras +40%, Wicket -15%, Four +10%.
    """
    if not is_day_night or innings != 2:
        return weights

    dew = _gc_lista_dew(config=config)
    dew_start = dew.get("start_over", 24)   # 0-based over index (= over 25)
    dew_peak = dew.get("peak_over", 44)     # full effect by over 45
    if over < dew_start:
        return weights

    intensity = min((over - dew_start) / max(dew_peak - dew_start, 1), 1.0)
    w = dict(weights)
    _scale_outcomes(w, {outcome: 1.0 + coef * intensity
                        for outcome, coef in (dew.get("factors") or {}).items()})

    # Re-normalise
    return _renormalise_to(weights, w)


# -----------------------------------------------------------------------------
# Feature 3: Pitch deterioration function
# -----------------------------------------------------------------------------
def _apply_pitch_wear(raw_weights: dict, pitch_type: str, pitch_wear: float) -> dict:
    """
    Apply pitch-deterioration effects to raw outcome probability weights.

    pitch_wear is a float in [0.0, 1.0] representing how worn the surface is
    (0.0 = fresh, 1.0 = fully worn after 120 balls of the innings).

    Effects by pitch type:
      Dry   – spin deterioration → wickets and dots increase with wear
      Green – old ball eases seam movement → batting gets slightly easier
      Flat/Dead – batting-friendly surface gets even more so
      Hard  – slight deterioration; wickets and dots creep up

    Weights are re-normalised after adjustment so they remain proportional.
    """
    if pitch_wear <= 0.0:
        return raw_weights

    adjusted = dict(raw_weights)
    w = pitch_wear  # shorthand

    if pitch_type == "Dry":
        # Spin track worsens for batting: wickets and dots go up
        adjusted["Wicket"] = adjusted.get("Wicket", 0) * (1.0 + 0.30 * w)
        adjusted["Dot"]    = adjusted.get("Dot",    0) * (1.0 + 0.15 * w)

    elif pitch_type == "Green":
        # Seam movement reduces as ball gets older; batting becomes easier
        adjusted["Four"]   = adjusted.get("Four",   0) * (1.0 + 0.10 * w)
        adjusted["Six"]    = adjusted.get("Six",    0) * (1.0 + 0.08 * w)
        adjusted["Wicket"] = adjusted.get("Wicket", 0) * (1.0 - 0.20 * w)

    elif pitch_type in ("Flat", "Dead"):
        # Already batting-friendly; gets marginally more so with wear
        adjusted["Four"]   = adjusted.get("Four",   0) * (1.0 + 0.08 * w)
        adjusted["Six"]    = adjusted.get("Six",    0) * (1.0 + 0.08 * w)
        adjusted["Wicket"] = adjusted.get("Wicket", 0) * (1.0 - 0.10 * w)

    elif pitch_type == "Hard":
        # Balanced track deteriorates slightly; wickets and dots increase
        adjusted["Wicket"] = adjusted.get("Wicket", 0) * (1.0 + 0.10 * w)
        adjusted["Dot"]    = adjusted.get("Dot",    0) * (1.0 + 0.05 * w)

    # Re-normalise so total weight is preserved (proportional scaling)
    orig_total = sum(raw_weights.values())
    new_total  = sum(adjusted.values())
    if new_total > 0 and orig_total > 0:
        scale    = orig_total / new_total
        adjusted = {k: v * scale for k, v in adjusted.items()}

    return adjusted


# Batting position context multipliers (Feature 9)
# Top-order batters have higher baseline impact; tail-enders are penalised.
_POS_BATTING_MULT: dict = {
    1: 1.05, 2: 1.05, 3: 1.03, 4: 1.02,
    5: 1.00, 6: 0.98, 7: 0.95, 8: 0.90,
    9: 0.85, 10: 0.80, 11: 0.75,
}


# -----------------------------------------------------------------------------
# 4) Compute blended probability weight for a single outcome
# -----------------------------------------------------------------------------
def compute_weighted_prob(
    outcome_type: str,
    base_prob: float,
    batting: int,
    bowling: int,
    fielding: int,
    pitch: str,
    bowling_type: str,
    streak: dict,
    batter_runs: int = 0,
    balls_faced: int = 0,
    format_name: Optional[str] = None,
    config=None,
    technique_rating: Optional[int] = None,
) -> float:
    """
    Returns a raw weight for one outcome (Dot/Single/Double/Three/Four/Six/Wicket/Extras),
    combining pitch-influence + player-skill.
    Includes special handling for "Hard" pitch (80/20 split), new-batter vulnerability,
    and graduated confidence curve.
    """
    # 0) Batter innings phase modifiers
    effective_batting = batting

    _is_lista = (format_name == "ListA")

    # New batter vulnerability: first 5 balls are dangerous.
    # ListA uses softer penalties than T20 to avoid middle-order wipeouts.
    if balls_faced <= 2:
        effective_batting *= 0.88 if _is_lista else 0.82
    elif balls_faced <= 5:
        effective_batting *= 0.94 if _is_lista else 0.90

    # Graduated confidence based on runs scored.
    # ListA keeps this curve flatter to reduce opener snowballing.
    if batter_runs >= 50:
        effective_batting *= 1.10 if _is_lista else 1.20
    elif batter_runs >= 35:
        effective_batting *= 1.07 if _is_lista else 1.15
    elif batter_runs >= 20:
        effective_batting *= 1.05 if _is_lista else 1.10
    elif batter_runs >= 10:
        effective_batting *= 1.02 if _is_lista else 1.05

    # Balls-faced confidence layer (independent of runs).
    if balls_faced >= 20:
        effective_batting *= 1.02 if _is_lista else 1.05
    elif balls_faced >= 12:
        effective_batting *= 1.01 if _is_lista else 1.03

    # 1) Player-skill fraction
    skill_frac = 0.5
    
    if outcome_type in ("Dot", "Single", "Double", "Three", "Four", "Six"):
        # Run scoring: defined by Batting vs Bowling
        if (effective_batting + bowling) > 0:
            # Standard calculation
            skill_frac = effective_batting / (effective_batting + bowling)
            
            # Hard pitch: batting-favored but bowlers still matter
            # 65/35 split — batters have the edge but good bowlers can compete
            if pitch == "Hard":
                skill_frac = (effective_batting * 0.65) / ((effective_batting * 0.65) + (bowling * 0.35))
                
        else:
            skill_frac = 0.5

    elif outcome_type == "Wicket":
        # Wicket taking: bowling vs batting contest only.
        # Fielding is handled separately in calculate_outcome() via the
        # catch-drop mechanic — it must NOT reduce chance-creation probability here.
        # FC (Phase 2): blend in defensive technique — distinct from
        # batting_rating's general scoring skill — specifically for the
        # dismissal contest, not the run-scoring skill_frac above. A
        # technically correct batter is harder to dismiss even at the same
        # batting_rating as a more free-scoring one.
        _defensive_batting = effective_batting
        if format_name == "FC" and technique_rating is not None:
            _defensive_batting = effective_batting * 0.7 + technique_rating * 0.3
        if (_defensive_batting + bowling) > 0:
            contest_frac = bowling / (_defensive_batting + bowling)
            skill_frac = contest_frac

            # Hard pitch: wickets harder to come by but not impossible
            if pitch == "Hard":
                skill_frac *= 0.85 if _is_lista else 0.75
        else:
            skill_frac = 0.5

    # 2) Pitch-influence fraction
    pitch_frac = 1.0
    if _is_lista or format_name == "FC":
        # ListA/FC each have their own phase/wear-scaling layers applied
        # outside this function (calculate_outcome's "3.25" stage; FC's is
        # the wear-interpolated bowling-style wicket factor). Reusing T20's
        # static pitch multipliers here would double-count — and for FC,
        # get_pitch_wicket_multiplier() always reads the T20 pitch block
        # regardless of the pitch name passed, which would silently apply
        # T20's wicket_factors instead of FC's wear-interpolated ones.
        pitch_frac = 1.0
    else:
        if outcome_type in ("Dot", "Single", "Double", "Three", "Four", "Six"):
            pitch_frac = get_pitch_run_multiplier(pitch, config=config)
        elif outcome_type == "Wicket":
            pitch_frac = get_pitch_wicket_multiplier(pitch, bowling_type, config=config)

    # 3) Blend Pitch & Skill
    # Default is 60% Pitch, 40% Skill.
    # But for "Hard", we want to emphasize the skew we just calculated.

    # 🔧 USER REQUEST: "If flat, batsman will have advantage over bowlers"
    # Logic: Boosting the skill component if favorable to bat

    _weights = _gc_blending_weights(config=config)
    alpha = _weights[0] if _weights else 0.6  # Pitch weight
    beta = _weights[1] if _weights else 0.4   # Skill weight

    if pitch == "Hard":
        # User explicitly mentioned 80/20. We applied that in skill logic.
        # Let's keep standard blending but rely on the skewed skill_frac.
        pass
    
    blended_frac = (alpha * pitch_frac) + (beta * skill_frac)

    # 4) Compute raw weight
    if outcome_type == "Extras":
        # Extras depend on bowler error but are floored to avoid near-zero rates.
        error_rate = max(EXTRA_ERROR_FLOOR, (100 - bowling) / 100.0)
        raw_weight = base_prob * error_rate * EXTRA_WEIGHT_MULTIPLIER
        return max(raw_weight, 0.0)

    # Apply specific boosts/penalties logic
    # Boundary streak penalty (same as before)
    boundary_penalty = 1.0
    if outcome_type in ("Four", "Six") and streak.get("boundaries", 0) >= 2:
        boundary_penalty = 0.8
    
    # Wicket boundary streak boost (same as before)
    wicket_boost = 1.0
    if outcome_type == "Wicket" and streak.get("boundaries", 0) >= 2:
        wicket_boost = 1.5

    raw_weight = base_prob * blended_frac * boundary_penalty * wicket_boost
    
    return max(raw_weight, 0.0)

# -----------------------------------------------------------------------------
# 4a2) Bowling matchup modifier — shared by calculate_outcome() and the
# Super Over engine so the two never drift (see squad_rules.py for the same
# "single source of truth" lesson applied to a different subsystem).
# -----------------------------------------------------------------------------
def compute_matchup_boost(bowling_type: str, bowling_hand: str, batting_hand: str,
                           batting: float, pitch: str) -> tuple:
    """
    Returns (matchup_boost, boundary_suppression) for a bowling-type vs
    batting-hand/pitch contest. matchup_boost multiplies Wicket weight;
    boundary_suppression (<=1.0) multiplies Four/Six weight when the bowler
    has a favorable matchup.
    """
    matchup_boost = 1.0

    # 1. Spin turning away from bat — classic cricket advantage
    if bowling_type in ("Off spin", "Finger spin") and batting_hand == "Left":
        matchup_boost *= 1.15  # Turning away from left-hander
    if bowling_type in ("Leg spin", "Wrist spin") and batting_hand == "Right":
        matchup_boost *= 1.15  # Turning away from right-hander

    # 2. Pace vs tail-enders — raw pace terrifies lower order
    if bowling_type in ("Fast", "Fast-medium", "Medium-fast") and batting < 30:
        matchup_boost *= 1.25

    # 3. Left-arm pace angle vs right-handers (all pitches)
    if (bowling_hand == "Left" and batting_hand == "Right"
            and bowling_type in ("Fast", "Fast-medium", "Medium-fast")):
        matchup_boost *= 1.10
        if pitch == "Green":
            matchup_boost *= 1.08  # Extra seam movement on Green

    # 4. Spin vs lower-order on turning tracks
    if (bowling_type in ("Off spin", "Leg spin", "Finger spin", "Wrist spin")
            and pitch == "Dry" and batting < 50):
        matchup_boost *= 1.10

    # Boundary suppression when bowler has matchup advantage
    boundary_suppression = 1.0
    if matchup_boost > 1.0:
        boundary_suppression = 1.0 / (matchup_boost ** 0.5)  # Mild inverse

    return matchup_boost, boundary_suppression


# -----------------------------------------------------------------------------
# 4a3) Pressure-effects application — shared by calculate_outcome() and the
# Super Over engine. Mutates a copy of raw_weights per PressureEngine's
# get_pressure_effects() output; does not touch Extras (bowler-error only).
# -----------------------------------------------------------------------------
def apply_pressure_effects_to_weights(raw_weights: dict, pressure_effects: dict,
                                       total_weight: float = None) -> tuple:
    """
    Returns (new_raw_weights, new_total_weight) with pressure_effects applied.
    raw_weights is not mutated in place.
    """
    weights = dict(raw_weights)
    if total_weight is None:
        total_weight = sum(weights.values())

    if not pressure_effects:
        return weights, total_weight

    logger.debug(f"  [PRESSURE] Applying pressure effects: {pressure_effects}")

    if "Dot" in weights:
        original_dot = weights["Dot"]
        dot_bonus = pressure_effects.get('dot_bonus', 0.0)
        weights["Dot"] += dot_bonus * total_weight
        logger.debug(f"  [PRESSURE] Dot: {original_dot:.6f} -> {weights['Dot']:.6f}")

    boundary_modifier = pressure_effects.get('boundary_modifier', 1.0)
    for boundary_type in ["Four", "Six"]:
        if boundary_type in weights:
            original_boundary = weights[boundary_type]
            weights[boundary_type] *= boundary_modifier
            logger.debug(f"  [PRESSURE] {boundary_type}: {original_boundary:.6f} -> {weights[boundary_type]:.6f}")

    if "Wicket" in weights:
        original_wicket = weights["Wicket"]
        weights["Wicket"] *= pressure_effects.get('wicket_modifier', 1.0)
        logger.debug(f"  [PRESSURE] Wicket: {original_wicket:.6f} -> {weights['Wicket']:.6f}")

    if "Single" in weights:
        original_single = weights["Single"]

        if 'single_boost' in pressure_effects:
            weights["Single"] *= pressure_effects['single_boost']
            logger.debug(f"  [PRESSURE] Single BOOST: {original_single:.6f} -> {weights['Single']:.6f}")

        elif 'strike_rotation_penalty' in pressure_effects:
            penalty = pressure_effects['strike_rotation_penalty']
            single_floor = pressure_effects.get('single_floor', 0.0)

            new_single_weight = original_single * (1 - penalty)
            floor_weight = single_floor * total_weight

            weights["Single"] = max(new_single_weight, floor_weight)
            logger.debug(f"  [PRESSURE] Single PENALTY: {original_single:.6f} -> {weights['Single']:.6f} (floor: {floor_weight:.6f})")

    if "Three" in weights:
        strike_rotation_penalty = pressure_effects.get('strike_rotation_penalty', 0.0)
        if strike_rotation_penalty > 0:
            original_three = weights["Three"]
            weights["Three"] *= (1 - strike_rotation_penalty)
            logger.debug(f"  [PRESSURE] Three: {original_three:.6f} -> {weights['Three']:.6f}")

    total_weight = sum(weights.values())
    return weights, total_weight


# -----------------------------------------------------------------------------
# 4b) Wicket type selection based on bowling style
# -----------------------------------------------------------------------------
def _get_wicket_type_by_bowling(bowling_type: str):
    """Return (types, weights) for wicket dismissal based on bowling style.

    Includes Stumped as a dismissal mode. Spinners produce far more stumpings
    than pace bowlers, matching real T20 cricket patterns.
    """
    if bowling_type in ("Fast", "Fast-medium", "Medium-fast"):
        # Pace bowlers: more bowled/LBW, very few stumpings
        types   = ["Caught", "Bowled", "LBW", "Run Out", "Stumped"]
        weights = [0.40,     0.28,     0.20,   0.08,      0.04]
    elif bowling_type in ("Off spin", "Leg spin", "Finger spin", "Wrist spin"):
        # Spinners: high stumping rate, more caught (bat-pad)
        types   = ["Caught", "Stumped", "Bowled", "LBW", "Run Out"]
        weights = [0.30,     0.25,      0.18,    0.15,   0.12]
    else:
        # Medium pace / default: balanced distribution
        types   = ["Caught", "Bowled", "LBW", "Run Out", "Stumped"]
        weights = [0.35,     0.25,     0.20,   0.10,      0.10]
    return types, weights

# -----------------------------------------------------------------------------
# 4c) Fielder selection — picked BEFORE the catch/misfield is resolved, so the
# chosen fielder's own rating (not the team average) drives the drop/misfield
# odds. Mirrors the role/position weighting that used to live in
# Match._select_fielder_for_wicket(), which ran only after the fact to name a
# fielder for commentary.
# -----------------------------------------------------------------------------
def _select_fielder(fielding_team, wicket_type: str = None, exclude_name: str = None):
    """Weighted-pick a fielder from the bowling XI.

    Returns (name, fielding_rating) or (None, None) if fielding_team is empty.
    """
    if not fielding_team:
        return None, None

    if wicket_type == "Stumped":
        keeper = next((p for p in fielding_team if p.get("role") == "Wicketkeeper"), None)
        if keeper:
            return keeper["name"], keeper.get("fielding_rating", 60)
        # No keeper in the XI: a stumping is physically impossible, but we still
        # need someone to attribute the chance to. Log loudly, same as the old
        # match.py fallback did.
        logger.warning("Stumped chance with no Wicketkeeper in bowling XI; falling back to a non-bowler fielder.")
        fallback = [p for p in fielding_team if p.get("name") != exclude_name] or list(fielding_team)
        chosen = random.choice(fallback)
        return chosen["name"], chosen.get("fielding_rating", 60)

    candidates = []
    weights = []
    for player in fielding_team:
        if exclude_name and player.get("name") == exclude_name:
            continue
        candidates.append(player)
        weight = player.get("fielding_rating", 60)
        if wicket_type == "Caught" and player.get("role") == "Wicketkeeper":
            weight *= 1.5
        if player.get("role") == "All-rounder" and player.get("fielding_rating", 0) > 70:
            weight *= 1.2
        weights.append(weight)

    if not candidates:
        candidates = list(fielding_team)
        weights = [p.get("fielding_rating", 60) for p in candidates]

    chosen = random.choices(candidates, weights=weights)[0]
    return chosen["name"], chosen.get("fielding_rating", 60)


# -----------------------------------------------------------------------------
# 4d) Catch/stumping drop resolution — shared by calculate_outcome() and the
# Super Over engine. The fielder is picked first (via _select_fielder above)
# so THEIR rating, not a team average, drives the drop odds.
# -----------------------------------------------------------------------------
def resolve_fielding_chance(fielding_team, bowler_name: str, wicket_choice: str,
                             fielding_quality: float = None) -> tuple:
    """
    For a Caught/Stumped dismissal chance, pick the fielder and roll for a
    drop. Returns (dropped: bool, fielder_name: str | None, drop_runs: int).
    drop_runs is only meaningful when dropped is True.

    fielding=90 -> ~3% drop | fielding=60 -> ~10% drop | fielding=30 -> ~19% drop
    """
    fielder_name = None
    fielder_rating = None
    if fielding_team:
        exclude = bowler_name if wicket_choice == "Caught" else None
        fielder_name, fielder_rating = _select_fielder(
            fielding_team, wicket_type=wicket_choice, exclude_name=exclude
        )

    drop_quality = fielder_rating if fielder_rating is not None else fielding_quality
    if drop_quality is None:
        return False, fielder_name, 0

    drop_prob = max(0.02, 0.22 - (drop_quality / 100.0) * 0.19)
    if random.random() < drop_prob:
        drop_runs = random.choices([1, 2, 4], weights=[35, 35, 30])[0]
        logger.debug(
            "[Fielding] Catch dropped by %s (rating=%.1f, drop_prob=%.3f)",
            fielder_name or "?", drop_quality, drop_prob,
        )
        return True, fielder_name, drop_runs

    return False, fielder_name, 0

# -----------------------------------------------------------------------------
# 5) Main outcome selection function: calculate_outcome
# -----------------------------------------------------------------------------
def calculate_outcome(
    batter: dict,
    bowler: dict,
    pitch: str,
    streak: dict,
    over_number: int,
    batter_runs: int,
    innings: int = 1,
    pressure_effects: dict = None,
    allow_extras: bool = True,
    free_hit: bool = False,
    balls_faced: int = 0,
    game_state: dict = None,
    pitch_wear: float = 0.0,
    batting_position: int = 5,
    game_mode_override: str = None,
    fielding_quality: float = None,
    fielding_team: list = None,
    ground_config_override: dict = None,
    format_config: Optional[FormatConfig] = None,
    is_day_night: bool = False,
    ball_overs_bowled: int = 0,
    new_ball_overs: int = 80,
) -> dict:
    """
    Determines the outcome of a single delivery.
    Returns a dict:
      - "type"       ∈ {"run", "wicket", "extra"}
      - "runs"       ∈ {0,1,2,3,4,6}
      - "description": string commentary
      - "wicket_type": if a wicket, one of ["Caught","Bowled","LBW","Run Out"], else None
      - "is_extra"   ∈ {True, False}
      - "batter_out" ∈ {True, False}

    In the final 4 overs (over_number >= 16), boundary (4/6) and wicket probabilities
    are boosted based on pitch type:
      • Flat/Dead: largest boundary boost
      • Hard     : moderate boundary boost
      • Green/Dry: minimal boundary boost (max ~1 boundary/over)
      • Wicket   : slight boost in all cases

    fielding_team, if given, is the bowling XI (list of player dicts with
    name/role/fielding_rating). On a Caught/Stumped chance or a misfield, a
    specific fielder is picked first and THEIR rating (not the team average)
    drives the drop/misfield odds; the pick is returned as result["fielder_name"].
    fielding_quality is a fallback team-average used only when fielding_team
    isn't supplied.
    """
    # print("\n==================== New Delivery ====================")
    # print(f"Ball context -> Over: {over_number + 1}, BatterRunsSoFar: {batter_runs}")
    # print(f"Batter: {batter['name']}, BattingRating: {batter['batting_rating']}, BattingHand: {batter['batting_hand']}")
    # print(f"Bowler: {bowler['name']}, BowlingRating: {bowler['bowling_rating']}, FieldingRating: {bowler['fielding_rating']}, BowlingHand: {bowler['bowling_hand']}, BowlingType: {bowler['bowling_type']}")
    # print(f"Pitch type: {pitch}, Current Streak: {streak}")

    # 1) Unpack numeric ratings & attributes
    # Feature 9: batting position context — top-order batters have a higher
    # effective batting rating; tail-enders face a modest penalty.
    _lista_pos_mult = {
        1: 1.02, 2: 1.02, 3: 1.01, 4: 1.00, 5: 1.00,
        6: 0.99, 7: 0.97, 8: 0.94, 9: 0.90, 10: 0.86, 11: 0.82,
    }
    _pos_mult = (
        _lista_pos_mult.get(batting_position, 1.00)
        if (format_config is not None and format_config.name == "ListA")
        else _POS_BATTING_MULT.get(batting_position, 1.00)
    )
    batting = batter["batting_rating"] * _pos_mult
    bowling = bowler["bowling_rating"]
    fielding = bowler["fielding_rating"]
    batting_hand = batter["batting_hand"]
    bowling_hand = bowler["bowling_hand"]
    bowling_type = bowler["bowling_type"]

    # 2) Select scoring matrix — format-aware.
    #    ListA: phase-specific matrix (PP1 / Middle / Death) scaled by pitch run factor.
    #    T20 / legacy: ground_conditions.yaml → hardcoded matrix (existing path).
    _gc = ground_config_override  # shorthand; None → global config cache
    _is_lista = (format_config is not None and format_config.name == "ListA")
    _is_fc = (format_config is not None and getattr(format_config, "format_family", None) == "multi_day")

    if _is_lista:
        # Pick the phase matrix then scale every outcome by the pitch run factor.
        # We do NOT apply game_mode_override here — ListA uses its own phase boosts
        # instead of T20 game-mode multipliers.
        _base_matrix = _get_lista_matrix(over_number, format_config, config=_gc)
        _run_factor  = _gc_lista_run_factor(pitch, config=_gc)
        # Boundary and Wicket rows are pitch-modulated differently from run rows.
        # Scale run outcomes by run_factor; keep Wicket/Extras at original proportions.
        _RUN_OUTCOMES = {"Dot", "Single", "Double", "Three", "Four", "Six"}
        pitch_matrix = {}
        for _k, _v in _base_matrix.items():
            if _k in _RUN_OUTCOMES:
                pitch_matrix[_k] = _v * _run_factor
            else:
                pitch_matrix[_k] = _v
        # Renormalise so weights still sum to 1.0
        _pm_total = sum(pitch_matrix.values())
        if _pm_total > 0:
            pitch_matrix = {k: v / _pm_total for k, v in pitch_matrix.items()}
        logger.debug("[ListA] phase=%s pitch=%s run_factor=%.2f",
                     format_config.get_phase(over_number).name, pitch, _run_factor)
    elif _is_fc:
        # FC: a single base matrix per pitch (no phase concept — FC has no
        # fielding-circle/powerplay phases), scaled by the FC run factor.
        # We do NOT apply game_mode_override here — FC has no game_modes
        # YAML section (Phase 1 scope; see engine/match.py's
        # _get_dynamic_game_mode, which is skipped entirely for FC).
        _base_matrix = _gc_fc_scoring_matrix(pitch, config=_gc) or DEFAULT_SCORING_MATRIX
        _run_factor = _gc_fc_run_factor(pitch, config=_gc)
        _RUN_OUTCOMES = {"Dot", "Single", "Double", "Three", "Four", "Six"}
        pitch_matrix = {}
        for _k, _v in _base_matrix.items():
            if _k in _RUN_OUTCOMES:
                pitch_matrix[_k] = _v * _run_factor
            else:
                pitch_matrix[_k] = _v
        _pm_total = sum(pitch_matrix.values())
        if _pm_total > 0:
            pitch_matrix = {k: v / _pm_total for k, v in pitch_matrix.items()}
        logger.debug("[FC] pitch=%s run_factor=%.2f", pitch, _run_factor)
    else:
        # Existing T20 / legacy path
        pitch_matrix = (_gc_scoring_matrix(pitch, mode_override=game_mode_override, config=_gc)
                        or PITCH_SCORING_MATRIX.get(pitch, DEFAULT_SCORING_MATRIX))

    # --- Bowling matchup modifier (computed once, applied to wickets + boundaries) ---
    matchup_boost, boundary_suppression = compute_matchup_boost(
        bowling_type, bowling_hand, batting_hand, batting, pitch
    )

    raw_weights = {}
    for outcome in pitch_matrix:
        base = pitch_matrix[outcome]
        # print(f"\n-- Computing weight for outcome: {outcome} (Base: {base}) --")

        if outcome == "Extras" and not allow_extras:
            raw_weights[outcome] = 0.0
            continue

        if outcome in ("Dot", "Single", "Double", "Three", "Four", "Six"):
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak, batter_runs, balls_faced,
                format_name=(format_config.name if format_config is not None else None),
                config=_gc,
            )
            # Apply boundary suppression when bowler has favorable matchup
            if outcome in ("Four", "Six") and boundary_suppression < 1.0:
                weight *= boundary_suppression

        elif outcome == "Wicket":
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak, batter_runs, balls_faced,
                format_name=(format_config.name if format_config is not None else None),
                config=_gc,
                technique_rating=batter.get("technique_rating"),
            ) * matchup_boost
        else:  # "Extras"
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak,
                format_name=(format_config.name if format_config is not None else None),
                config=_gc,
            )

        # --- T20 phase boosts (skipped for ListA — handled via
        # _apply_lista_phase_boosts; skipped for FC — no fielding-circle
        # phases exist in FC at all, and these hardcoded over-ranges
        # (powerplay 0-5, death 16-19) are T20-over-numbering-specific and
        # would misfire on every FC over if applied) ---
        if not _is_lista and not _is_fc:
            # Load configurable phase boosts with hardcoded fallbacks
            _phase = _gc_phase_boosts(config=_gc) or {}
            _pp_cfg = _phase.get("powerplay", {})
            _death_cfg = _phase.get("death_overs", {})
            _inn2_cfg = _phase.get("second_innings_death", {})

            # Powerplay boosts
            pp_start = _pp_cfg.get("overs_start", 0)
            pp_end = _pp_cfg.get("overs_end", 5)
            if pp_start <= over_number <= pp_end:
                if outcome in ("Four", "Six"):
                    pp_boost = _pp_cfg.get("boundary_multiplier", 1.25)
                    logger.debug(f"  [Powerplay] Boosting {outcome} by {pp_boost}x")
                    weight *= pp_boost

            # Death-over boosts (last 4 overs: 17-20)
            death_start = _death_cfg.get("overs_start", 16)
            death_end = _death_cfg.get("overs_end", 19)
            in_death = death_start <= over_number <= death_end

            if in_death:
                if outcome in ("Four", "Six"):
                    if pitch in ("Flat", "Dead", "Hard"):
                        boundary_boost = _death_cfg.get("boundary_boost_batting_pitch", 2.2)
                    else:  # Green or Dry
                        boundary_boost = _death_cfg.get("boundary_boost_bowling_pitch", 1.8)
                    logger.debug(f"  DeathOver: BOUNDARY ({outcome}) on {pitch} by factor {boundary_boost}")
                    weight *= boundary_boost

                if outcome == "Wicket":
                    wicket_boost = _death_cfg.get("wicket_boost", 1.6)
                    logger.debug(f"  DeathOver: WICKET on {pitch} by factor {wicket_boost}")
                    weight *= wicket_boost

            # Second innings death-over boosts (mild — chasing advantage already helps)
            if innings == 2 and in_death:
                if outcome in ("Single", "Double", "Three", "Four", "Six"):
                    scoring_boost = _inn2_cfg.get("scoring_boost", 1.05)
                    weight *= scoring_boost

                if outcome == "Wicket":
                    wicket_boost_2nd = _inn2_cfg.get("wicket_boost", 1.15)
                    weight *= wicket_boost_2nd

        # Ensure no negative weights
        weight = max(weight, 0.0)
        raw_weights[outcome] = weight
        # print(f"  FinalRawWeight[{outcome}]: {weight:.6f}")
    
    # 3.25) Apply phase boosts and pitch deterioration.
    # ListA uses its own phase boost table and progressive wear model.
    # T20 uses the existing _apply_pitch_wear (unchanged).
    # All wear layers run BEFORE GSME so the momentum engine sees adjusted weights.
    if _is_lista:
        # ListA phase boosts (PP1 / Middle / Death boundary/wicket modifiers)
        raw_weights = _apply_lista_phase_boosts(raw_weights, over_number, pitch,
                                                innings, format_config, config=_gc)
        # ListA progressive pitch wear (more pronounced on Dry/Hard over 50 overs)
        if pitch_wear > 0.0:
            raw_weights = _apply_lista_pitch_wear(raw_weights, pitch, pitch_wear,
                                                  config=_gc)
            logger.debug("[ListA PitchWear=%.3f] Applied ListA wear model.", pitch_wear)
        # Dew factor for Day/Night matches (2nd innings evening)
        raw_weights = _apply_dew_factor(raw_weights, innings, over_number,
                                        is_day_night, format_config, config=_gc)
        # Pitch-specific fine-tuning after wear and dew layers, then the
        # rotation profile, then wicket scaling. Order is significant.
        _scale_outcomes(raw_weights, _gc_lista_fine_tune(pitch, config=_gc))
        _scale_outcomes(raw_weights, _gc_lista_dot_single(pitch, config=_gc))
        # Wicket scaling is the pitch-wide multiplier times this bowler's style
        # affinity for the surface. The style term is what makes a Green top a
        # seamer's pitch and a Dry one a spinner's; before it existed ListA had
        # only the pitch-wide scalar, so both surfaces handed out wickets in
        # whatever ratio the two attacks happened to bowl their overs.
        _scale_outcomes(raw_weights, {"Wicket": (
            _gc_lista_wicket_mult(pitch, config=_gc)
            * _gc_lista_wicket_factor_for(pitch, bowling_type, config=_gc)
        )})
    elif _is_fc:
        # FC general wear (continuous match-long scalar, not per-innings —
        # pitch_wear is computed by Match against days*overs_per_day*6, see
        # engine/match.py).
        if pitch_wear > 0.0:
            raw_weights = _apply_fc_pitch_wear(raw_weights, pitch, pitch_wear, config=_gc)
            logger.debug("[FC PitchWear=%.3f] Applied FC wear model.", pitch_wear)
        # Wear-interpolated bowling-style wicket factor — the mechanism that
        # actually makes the pitch favor different bowler types as it wears
        # (a static per-pitch table, unlike T20/ListA's, would not do this).
        _scale_outcomes(raw_weights, {"Wicket":
            _gc_fc_wicket_factor_for(pitch, bowling_type, pitch_wear, config=_gc)
        })
        # Ball-condition (Phase 2): independent of pitch wear/type — a new
        # ball swings for genuine pace, that fades, then an old ball can
        # reverse-swing for genuinely fast bowlers just before the next new
        # ball is due (fmt.new_ball_overs).
        _scale_outcomes(raw_weights, {"Wicket":
            _gc_fc_ball_condition_factor(bowling_type, ball_overs_bowled, new_ball_overs, config=_gc)
        })
        # Handedness-specific rough-targeting: footmark rough only exists
        # where days of the same bowling angle have worn the same patch —
        # a wear-*and*-matchup effect distinct from both the bowling-style-
        # only wear factor above and compute_matchup_boost's static (no
        # wear scaling) handedness bonus applied to every format below.
        _scale_outcomes(raw_weights, {"Wicket":
            _gc_fc_rough_targeting_factor(bowling_type, batting_hand, pitch_wear, pitch, config=_gc)
        })
    else:
        # T20 / legacy path — existing pitch wear model unchanged
        if pitch_wear > 0.0:
            raw_weights = _apply_pitch_wear(raw_weights, pitch, pitch_wear)
            logger.debug("[PitchWear=%.3f] Applied T20 pitch deterioration.", pitch_wear)

    # 3.5) Apply Game State Momentum Engine (GSME) adjustments.
    # This layer accounts for ball history (last 18 deliveries), run-rate
    # pressure, resources remaining, and collapse risk — BEFORE the
    # pressure-engine and scenario-engine modifiers are applied.
    if game_state is not None:
        raw_weights = apply_game_state_to_probs(raw_weights, game_state)
        logger.debug("[GSME] Applied game-state multipliers to raw_weights.")

    # 3.6) Calculate total weight
    total_weight = sum(raw_weights.values())

    # 3.7) Apply pressure effects if provided
    if pressure_effects:
        raw_weights, total_weight = apply_pressure_effects_to_weights(
            raw_weights, pressure_effects, total_weight
        )
    
    # 4) Free hit: slight boundary boost (+10%) for both Four and Six.
    if free_hit and "Four" in raw_weights and "Six" in raw_weights:
        raw_weights["Four"] *= FREE_HIT_BOUNDARY_BOOST
        raw_weights["Six"] *= FREE_HIT_BOUNDARY_BOOST
        total_weight = sum(raw_weights.values())

    # 5) Normalize weights into probabilities
    # print(f"\n[calculate_outcome] Total raw weight sum: {total_weight:.6f}")
    if total_weight <= 0:
        # Fallback in pathological case
        chosen = "Dot"
        # print("[calculate_outcome] Warning: Total weight <= 0, defaulting to Dot ball")
    else:
        normalized_weights = [raw_weights[o] / total_weight for o in raw_weights]
        # logger.debug(f"[calculate_outcome] Normalized weights:")
        for o, nw in zip(raw_weights.keys(), normalized_weights):
            logger.debug(f"  {o}: {nw:.4f}")
        chosen = random.choices(list(raw_weights.keys()), weights=normalized_weights)[0]

    # print(f"[calculate_outcome] Chosen outcome: {chosen}")

    # 5) Build and return the result dictionary
    result = {
        "type": None,
        "runs": 0,
        "description": "",
        "wicket_type": None,
        "is_extra": False,
        "batter_out": False
    }

    if chosen == "Wicket":
        result["type"] = "wicket"
        result["runs"] = 0
        result["batter_out"] = True

        # Decide wicket type based on bowling style (A7: varies by bowling type, A6: includes Stumped)
        types, weights_pct = _get_wicket_type_by_bowling(bowling_type)
        wicket_choice = random.choices(types, weights=weights_pct)[0]

        result["wicket_type"] = wicket_choice

        # A1: Run Out happens after completing 1 run (out attempting the 2nd)
        if wicket_choice == "Run Out":
            result["runs"] = 1

        # FIELDING: Catch-drop check for Caught and Stumped dismissals.
        # The fielder is picked FIRST, then their own rating (not the team
        # average) drives the drop odds — a gun fielder holds on far more
        # often than a part-timer, and hiding a poor fielder now matters.
        # fielding=90 → ~3% drop  |  fielding=60 → ~10% drop  |  fielding=30 → ~19% drop
        if wicket_choice in ("Caught", "Stumped"):
            dropped, fielder_name, drop_runs = resolve_fielding_chance(
                fielding_team, bowler.get("name"), wicket_choice, fielding_quality
            )
            if fielder_name:
                result["fielder_name"] = fielder_name

            if dropped:
                # Dropped! Convert wicket into runs
                result["batter_out"] = False
                result["wicket_type"] = None
                result["type"] = "run"
                result["runs"] = drop_runs
                result["dropped_catch"] = True
                if fielder_name:
                    result["description"] = f"DROPPED! {fielder_name} spills a sitter — a costly miss in the field!"
                else:
                    result["description"] = "DROPPED! The chance goes begging — a costly miss in the field!"
                return result

        # Use guaranteed wicket commentary templates
        wicket_descriptions = [
        "He's out! Brilliant delivery!",
        "Gone! A crucial wicket falls!",
        "What a fantastic catch to dismiss him!",
        "Wicket! Excellent bowling!",
        "Out! Clean bowled!",
        "Caught! Brilliant fielding!",
        "LBW! Plumb in front!",
        "Stumped! Lightning quick!",
        "Run out! Direct hit!",
        "Caught behind! Great catch!",
        "Bowled! Perfect delivery!",
        "Out! Magnificent catch!",
        "Wicket falls! Great bowling!",
        "Dismissed! Excellent work!",
        "Gone! Spectacular catch!",
        "Wicket! Superb delivery!",
        "Caught! Brilliant take!",
        "Bowled middle stump!",
        "Out! Perfect line and length!",
        "Gone! Perfect execution!"
    ]

        # Use commentary template for Wicket
        template = random.choice(wicket_descriptions)
        result["description"] = template

        # print(f"[calculate_outcome] WICKET! Type: {wicket_choice}, Description: {template}")

    elif chosen == "Extras":
        result["type"] = "extra"
        result["is_extra"] = True

        # A4: Weighted extra type selection — format-aware distribution.
        # ListA: more wides (slower pace/spin in 30-over middle overs bowl
        #        wider lines; spinner drifts are common); fewer no-balls
        #        (less aggressive short-ball pace attack than T20).
        # T20:   higher no-ball rate from aggressive pace bowling.
        if _is_lista:
            extra_types   = ["Wide", "No Ball", "Leg Bye", "Byes"]
            extra_weights = [0.52,   0.13,      0.22,      0.13]
        else:
            extra_types   = ["Wide", "No Ball", "Leg Bye", "Byes"]
            extra_weights = [0.40,   0.25,      0.20,      0.15]
        extra_choice  = random.choices(extra_types, weights=extra_weights)[0]

        # A4: Variable runs per extra type
        if extra_choice == "Wide":
            result["runs"] = 1
        elif extra_choice == "No Ball":
            result["runs"] = 1
        elif extra_choice == "Leg Bye":
            result["runs"] = random.choices([1, 2], weights=[0.80, 0.20])[0]
        elif extra_choice == "Byes":
            result["runs"] = random.choices([1, 2, 4], weights=[0.85, 0.10, 0.05])[0]

        result["extra_type"] = extra_choice
        template = random.choice(commentary_templates["Extras"])
        result["description"] = f"{template} ({extra_choice})"

    else:
        # It must be one of Dot, Single, Double, Three, Four, Six
        runs_map = {
            "Dot":    0,
            "Single": 1,
            "Double": 2,
            "Three":  3,
            "Four":   4,
            "Six":    6
        }
        result["type"] = "run"
        result["runs"] = runs_map[chosen]
        result["batter_out"] = False

        # Use commentary template for run outcomes
        template = random.choice(commentary_templates[chosen])
        result["description"] = f"{template}"

        # FIELDING: Misfield mechanic — poor fielders give away extra runs.
        # Only on dot balls and singles (not boundaries or multiple-run shots).
        # The fielder involved is picked first; THEIR rating drives the odds.
        # fielding=90 → ~1.5%  |  fielding=60 → ~5%  |  fielding=30 → ~10%
        if result["runs"] in (0, 1):
            misfield_fielder, misfield_rating = (
                _select_fielder(fielding_team) if fielding_team else (None, None)
            )
            misfield_quality = misfield_rating if misfield_rating is not None else fielding_quality
            if misfield_quality is not None:
                misfield_prob = max(0.01, 0.115 - (misfield_quality / 100.0) * 0.105)
                if random.random() < misfield_prob:
                    result["runs"] += 1
                    result["misfield"] = True
                    if misfield_fielder:
                        result["fielder_name"] = misfield_fielder
                        result["description"] += f" — misfield by {misfield_fielder}, they steal an extra!"
                    else:
                        result["description"] += " — misfield, they steal an extra!"
                    logger.debug(
                        "[Fielding] Misfield by %s! extra run granted (rating=%.1f, prob=%.3f)",
                        misfield_fielder or "?", misfield_quality, misfield_prob,
                    )

        # logger.debug(f"[calculate_outcome] RUN! Outcome: {chosen}, Runs: {result['runs']}, Description: {template}")

    logger.debug("=======================================================\n")
    return result
