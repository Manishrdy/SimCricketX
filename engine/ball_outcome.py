import random
import logging
from engine.ground_config import (
    get_scoring_matrix as _gc_scoring_matrix,
    get_run_factor as _gc_run_factor,
    get_wicket_factors as _gc_wicket_factors,
    get_phase_boosts as _gc_phase_boosts,
    get_blending_weights as _gc_blending_weights,
)
from engine.game_state_engine import apply_game_state_to_probs

logger = logging.getLogger(__name__)

# Tune extras frequency to target ~3-6 extras per innings (120 balls).
EXTRA_ERROR_FLOOR = 0.30
EXTRA_WEIGHT_MULTIPLIER = 2.2

# Free hit boundary share (combined Four+Six probability share).
FREE_HIT_BOUNDARY_SHARE = 0.40

# -----------------------------------------------------------------------------
# ball_outcome.py
#
# Implements ball-by-ball outcome logic with:
#   • 60% pitch-influence + 40% player-skill blending
#   • Detailed commentary templates
#   • Enhanced boundary & wicket chances in the final 4 overs (17–20)
#
# Pitch average ranges (T20 context):
#   - Green: 120–150 runs (favors pace bowlers)
#   - Flat : 180–200 runs (batting paradise)
#   - Dry  : 120–150 runs (favors spin bowlers)
#   - Hard : 150–180 runs (balanced, slight batting edge)
#   - Dead : 200–240 runs (batting festival; very few wickets)
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
# 2) Pitch-influence definitions (60% weight)
# -----------------------------------------------------------------------------
PITCH_RUN_FACTOR = {
    # Slightly boost scoring on Green/Dry (+15%), reduce Flat (-10%)
    "Green": 0.70 * 1.15,   # run-suppressing → ~150–170 average
    "Dry":   0.70 * 1.15,   # spin-friendly → ~150–170 average
    "Hard":  1.10,   # balanced (batting edge) → 160–180
    "Flat":  1.20 * 0.90,   # batting paradise → 180–200 (slightly toned down)
    "Dead":  1.30    # batting festival → 200–230
}

# ---------------------------------------------------------------------
# 2) Pitch-influence definitions (60% weight)
# ---------------------------------------------------------------------

PITCH_WICKET_FACTOR = {
    "Green": {
        "Fast":         1.40,   # fastest bowlers excel on Green
        "Fast-medium":  1.20,
        "Medium-fast":  1.15,
        "default":      0.55    # spinners/pacers that don’t fit above
    },
    "Dry": {
        "Leg spin":     1.40,   # leggies turn square, highest threat
        "Wrist spin":   1.35,   # similar to leggies on a turning track
        "Off spin":     1.30,   # very effective but slightly easier than a leggie
        "Finger spin":  1.20,   # orthodox left-arm; still strong, but a bit less than right-arm
        "default":      0.60    # pace bowlers on a dry turner
    },
    "Hard": {
        "Fast":         1.10,   # pace gets decent bounce & seam, but still batsmen can score
        "Fast-medium":  1.05,
        "Medium-fast":  1.00,
        "default":      0.90    # spin/other styles on a true track
    },
    "Flat": {
        # Almost no one “takes” wickets easily on Flat—batsmen dominate.
        "default":      0.85
    },
    "Dead": {
        # Very tough for bowlers on Dead track—wickets are rare
        "Fast":         0.60,
        "Fast-medium":  0.60,
        "Medium-fast":  0.60,
        "Off spin":     0.60,
        "Leg spin":     0.60,
        "Finger spin":  0.60,
        "Wrist spin":   0.60,
        "default":      0.60
    }
}

def get_pitch_run_multiplier(pitch: str, config=None) -> float:
    """
    Returns the run-friendly multiplier for the given pitch.
    Uses ground_conditions.yaml if available, falls back to hardcoded constants.
    Pass *config* to use a user-specific snapshot instead of the global config.
    """
    factor = _gc_run_factor(pitch, config=config)
    if factor is None:
        factor = PITCH_RUN_FACTOR.get(pitch, 1.0)
    return factor

def get_pitch_wicket_multiplier(pitch: str, bowling_type: str, config=None) -> float:
    """
    Returns the wicket-friendly multiplier for the given pitch and bowling type.
    Uses ground_conditions.yaml if available, falls back to hardcoded constants.
    Pass *config* to use a user-specific snapshot instead of the global config.
    """
    wf = _gc_wicket_factors(pitch, config=config)
    if wf:
        return wf.get(bowling_type, wf.get("default", 1.0))
    slot = PITCH_WICKET_FACTOR.get(pitch, {})
    return slot.get(bowling_type, slot.get("default", 1.0))

# -----------------------------------------------------------------------------
# 3) Base outcome probabilities (raw frequencies)
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# 3) Pitch-specific outcome probabilities (realistic scoring patterns)
# -----------------------------------------------------------------------------
PITCH_SCORING_MATRIX = {
    "Green": {
        "Dot":     0.42,   # High dot ball % (difficult to score)
        "Single":  0.345,
        "Double":  0.065,
        "Three":   0.005,  # ~0.6 threes per innings (very rare)
        "Four":    0.05,   # Low boundaries
        "Six":     0.015,
        "Wicket":  0.07,   # High wicket chance (favors pacers)
        "Extras":  0.04
    },
    "Dry": {
        "Dot":     0.38,   # Spin friendly = difficult scoring
        "Single":  0.355,
        "Double":  0.08,
        "Three":   0.008,  # ~1 three per innings
        "Four":    0.06,
        "Six":     0.02,
        "Wicket":  0.06,   # Favors spinners
        "Extras":  0.04
    },
    "Hard": {
        # 65/35 Batting/Bowling split — batters favored but bowlers compete
        "Dot":     0.30,
        "Single":  0.34,
        "Double":  0.11,
        "Three":   0.008,  # ~1 three per innings
        "Four":    0.09,
        "Six":     0.05,
        "Wicket":  0.06,
        "Extras":  0.04
    },
    "Flat": {
        # Pure batting paradise
        "Dot":     0.20,   # Very low dot %
        "Single":  0.305,
        "Double":  0.14,
        "Three":   0.008,  # ~1 three per innings
        "Four":    0.18,   # High boundaries
        "Six":     0.12,   # High sixes
        "Wicket":  0.03,   # Very low wickets
        "Extras":  0.02
    },
    "Dead": {
        # Batting paradise (200+ average, ~4-5 wickets)
        "Dot":     0.18,
        "Single":  0.325,
        "Double":  0.145,
        "Three":   0.005,  # ~0.6 threes per innings (very rare)
        "Four":    0.19,
        "Six":     0.10,
        "Wicket":  0.03,
        "Extras":  0.03
    }
}

# Fallback matrix for unknown pitch types (CORRECTED)
DEFAULT_SCORING_MATRIX = {
    "Dot":     0.27,   # Increased from 0.25
    "Single":  0.34,   # Absorbed most of Three reduction
    "Double":  0.13,   # Absorbed some of Three reduction
    "Three":   0.008,  # ~1 three per innings (was 0.06 — far too high)
    "Four":    0.09,   # Increased from 0.08
    "Six":     0.05,   # Increased from 0.04
    "Wicket":  0.05,   # Rounded from 0.044
    "Extras":  0.05    # Rounded from 0.048
    # Sum: ~1.00 ✅
}


def _validate_scoring_matrices():
    """Validate that all pitch scoring matrices sum to 1.0"""
    logger.info("Validating pitch scoring matrices:")

    for pitch_type, matrix in PITCH_SCORING_MATRIX.items():
        total = sum(matrix.values())
        status = "OK" if abs(total - 1.0) < 0.001 else "FAIL"
        logger.info(f"  {pitch_type}: {total:.3f} [{status}]")

        if abs(total - 1.0) >= 0.001:
            logger.warning(f"    Warning: {pitch_type} matrix doesn't sum to 1.0!")
    
    # Validate default matrix too
    default_total = sum(DEFAULT_SCORING_MATRIX.values())
    # print(f"  DEFAULT: {default_total:.3f} {'✅' if abs(default_total - 1.0) < 0.1 else '❌'}")

# Validate matrices on module import (runs once when ball_outcome.py is imported)
_validate_scoring_matrices()


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
    config=None,
) -> float:
    """
    Returns a raw weight for one outcome (Dot/Single/Double/Three/Four/Six/Wicket/Extras),
    combining pitch-influence + player-skill.
    Includes special handling for "Hard" pitch (80/20 split), new-batter vulnerability,
    and graduated confidence curve.
    """
    # 0) Batter innings phase modifiers
    effective_batting = batting

    # New batter vulnerability: first 5 balls are dangerous
    if balls_faced <= 2:
        effective_batting *= 0.82  # 18% penalty — very vulnerable, adjusting to conditions
    elif balls_faced <= 5:
        effective_batting *= 0.90  # 10% penalty — still settling in

    # Graduated confidence based on runs scored
    if batter_runs >= 50:
        effective_batting *= 1.20   # Dominant — on top of the bowling
    elif batter_runs >= 35:
        effective_batting *= 1.15   # Dangerous — timing everything
    elif batter_runs >= 20:
        effective_batting *= 1.10   # Set — comfortable at the crease
    elif batter_runs >= 10:
        effective_batting *= 1.05   # Getting eye in — starting to find gaps

    # Balls-faced confidence layer (independent of runs, kicks in after vulnerability window)
    if balls_faced >= 20:
        effective_batting *= 1.05   # Well settled — extra familiarity bonus
    elif balls_faced >= 12:
        effective_batting *= 1.03   # Reading the bowler now

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
        if (effective_batting + bowling) > 0:
            contest_frac = bowling / (effective_batting + bowling)
            skill_frac = contest_frac

            # Hard pitch: wickets harder to come by but not impossible
            if pitch == "Hard":
                skill_frac *= 0.75  # 25% reduction in wicket-taking ability
        else:
            skill_frac = 0.5

    # 2) Pitch-influence fraction
    pitch_frac = 1.0
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
    ground_config_override: dict = None,
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
    """
    # print("\n==================== New Delivery ====================")
    # print(f"Ball context -> Over: {over_number + 1}, BatterRunsSoFar: {batter_runs}")
    # print(f"Batter: {batter['name']}, BattingRating: {batter['batting_rating']}, BattingHand: {batter['batting_hand']}")
    # print(f"Bowler: {bowler['name']}, BowlingRating: {bowler['bowling_rating']}, FieldingRating: {bowler['fielding_rating']}, BowlingHand: {bowler['bowling_hand']}, BowlingType: {bowler['bowling_type']}")
    # print(f"Pitch type: {pitch}, Current Streak: {streak}")

    # 1) Unpack numeric ratings & attributes
    # Feature 9: batting position context — top-order batters have a higher
    # effective batting rating; tail-enders face a modest penalty.
    _pos_mult = _POS_BATTING_MULT.get(batting_position, 1.00)
    batting = batter["batting_rating"] * _pos_mult
    bowling = bowler["bowling_rating"]
    fielding = bowler["fielding_rating"]
    batting_hand = batter["batting_hand"]
    bowling_hand = bowler["bowling_hand"]
    bowling_type = bowler["bowling_type"]

    # 2) Get pitch-specific scoring matrix (user config snapshot → global config → hardcoded fallback)
    # Feature 13: pass game_mode_override so dynamic game mode selection is respected.
    _gc = ground_config_override  # shorthand; None for legacy matches → falls back to global cache
    pitch_matrix = _gc_scoring_matrix(pitch, mode_override=game_mode_override, config=_gc) or PITCH_SCORING_MATRIX.get(pitch, DEFAULT_SCORING_MATRIX)
    # print(f"[calculate_outcome] Using scoring matrix for pitch: {pitch}")

    raw_weights = {}
    for outcome in pitch_matrix:
        base = pitch_matrix[outcome]
        # print(f"\n-- Computing weight for outcome: {outcome} (Base: {base}) --")

        if outcome == "Extras" and not allow_extras:
            raw_weights[outcome] = 0.0
            continue

        # Compute base weight via 60/40 blending
        # --- Bowling matchup modifier (computed once, applied to wickets + boundaries) ---
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

        if outcome in ("Dot", "Single", "Double", "Three", "Four", "Six"):
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak, batter_runs, balls_faced,
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
                config=_gc,
            ) * matchup_boost
        else:  # "Extras"
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak,
                config=_gc,
            )

        # --- Phase boosts (apply to ALL outcome types, not just Extras) ---
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
    
    # 3.25) Apply pitch deterioration (Feature 3).
    # Adjusts raw weights based on how many balls have been bowled this innings.
    # Must run BEFORE GSME so the momentum engine sees wear-adjusted base weights.
    if pitch_wear > 0.0:
        raw_weights = _apply_pitch_wear(raw_weights, pitch, pitch_wear)
        logger.debug("[PitchWear=%.3f] Applied pitch deterioration to raw_weights.", pitch_wear)

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
        logger.debug(f"  [PRESSURE] Applying pressure effects: {pressure_effects}")
        
        # Increase dot ball probability
        if "Dot" in raw_weights:
            original_dot = raw_weights["Dot"]
            dot_bonus = pressure_effects.get('dot_bonus', 0.0)
            raw_weights["Dot"] += dot_bonus * total_weight
            logger.debug(f"  [PRESSURE] Dot: {original_dot:.6f} -> {raw_weights['Dot']:.6f}")
        
        # Modify boundary probabilities
        boundary_modifier = pressure_effects.get('boundary_modifier', 1.0)
        for boundary_type in ["Four", "Six"]:
            if boundary_type in raw_weights:
                original_boundary = raw_weights[boundary_type]
                raw_weights[boundary_type] *= boundary_modifier
                logger.debug(f"  [PRESSURE] {boundary_type}: {original_boundary:.6f} -> {raw_weights[boundary_type]:.6f}")
        
        # Modify wicket probability
        if "Wicket" in raw_weights:
            original_wicket = raw_weights["Wicket"]
            raw_weights["Wicket"] *= pressure_effects.get('wicket_modifier', 1.0)
            logger.debug(f"  [PRESSURE] Wicket: {original_wicket:.6f} -> {raw_weights['Wicket']:.6f}")
        
        # 🔧 NEW: Handle singles (boost or penalty with floor)
        if "Single" in raw_weights:
            original_single = raw_weights["Single"]
            
            # Apply single boost (defensive mode)
            if 'single_boost' in pressure_effects:
                raw_weights["Single"] *= pressure_effects['single_boost']
                logger.debug(f"  [PRESSURE] Single BOOST: {original_single:.6f} -> {raw_weights['Single']:.6f}")
            
            # Apply single penalty with floor (aggressive mode)
            elif 'strike_rotation_penalty' in pressure_effects:
                penalty = pressure_effects['strike_rotation_penalty']
                single_floor = pressure_effects.get('single_floor', 0.0)
                
                # Apply penalty but enforce minimum floor
                new_single_weight = original_single * (1 - penalty)
                floor_weight = single_floor * total_weight
                raw_weights["Single"] = max(new_single_weight, floor_weight)

                logger.debug(f"  [PRESSURE] Single PENALTY: {original_single:.6f} -> {raw_weights['Single']:.6f} (floor: {floor_weight:.6f})")
        
        # Reduce strike rotation for threes
        if "Three" in raw_weights:
            strike_rotation_penalty = pressure_effects.get('strike_rotation_penalty', 0.0)
            if strike_rotation_penalty > 0:
                original_three = raw_weights["Three"]
                raw_weights["Three"] *= (1 - strike_rotation_penalty)
                logger.debug(f"  [PRESSURE] Three: {original_three:.6f} -> {raw_weights['Three']:.6f}")
        
        # Recalculate total weight after pressure modifications
        total_weight = sum(raw_weights.values())
    
    # 4) Free hit: enforce combined Four+Six share (default 40%)
    if free_hit and "Four" in raw_weights and "Six" in raw_weights:
        total_weight = sum(raw_weights.values())
        if total_weight > 0:
            boundary_weight = raw_weights["Four"] + raw_weights["Six"]
            target_boundary = FREE_HIT_BOUNDARY_SHARE * total_weight
            if boundary_weight > 0 and total_weight > boundary_weight:
                boundary_scale = target_boundary / boundary_weight
                other_scale = (total_weight - target_boundary) / (total_weight - boundary_weight)
                for key in raw_weights:
                    if key in ("Four", "Six"):
                        raw_weights[key] *= boundary_scale
                    else:
                        raw_weights[key] *= other_scale
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
        # High fielding team rarely drops; poor fielding team drops more often.
        # fielding=90 → ~3% drop  |  fielding=60 → ~10% drop  |  fielding=30 → ~19% drop
        if wicket_choice in ("Caught", "Stumped") and fielding_quality is not None:
            drop_prob = max(0.02, 0.22 - (fielding_quality / 100.0) * 0.19)
            if random.random() < drop_prob:
                # Dropped! Convert wicket into runs
                result["batter_out"] = False
                result["wicket_type"] = None
                result["type"] = "run"
                result["runs"] = random.choices([1, 2, 4], weights=[35, 35, 30])[0]
                result["dropped_catch"] = True
                result["description"] = "DROPPED! The chance goes begging — a costly miss in the field!"
                logger.debug("[Fielding] Catch dropped (quality=%.1f, drop_prob=%.3f)", fielding_quality, drop_prob)
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

        # A4: Weighted extra type selection (realistic T20 distribution)
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

        # FIELDING: Misfield mechanic — poor fielding teams give away extra runs.
        # Only on dot balls and singles (not boundaries or multiple-run shots).
        # fielding=90 → ~1.5%  |  fielding=60 → ~5%  |  fielding=30 → ~10%
        if result["runs"] in (0, 1) and fielding_quality is not None:
            misfield_prob = max(0.01, 0.115 - (fielding_quality / 100.0) * 0.105)
            if random.random() < misfield_prob:
                result["runs"] += 1
                result["misfield"] = True
                result["description"] += " — misfield, they steal an extra!"
                logger.debug("[Fielding] Misfield! extra run granted (quality=%.1f, prob=%.3f)", fielding_quality, misfield_prob)

        # logger.debug(f"[calculate_outcome] RUN! Outcome: {chosen}, Runs: {result['runs']}, Description: {template}")

    logger.debug("=======================================================\n")
    return result


