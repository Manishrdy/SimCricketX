import random
import logging
from engine.ground_config import (
    get_scoring_matrix as _gc_scoring_matrix,
    get_run_factor as _gc_run_factor,
    get_wicket_factors as _gc_wicket_factors,
    get_phase_boosts as _gc_phase_boosts,
    get_blending_weights as _gc_blending_weights,
)

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
commentary_templates = {
    "Dot": [
        "Good length, no run.",
        "Well defended.",
        "Beaten on the front foot.",
        "Solid defensive shot.",
        "Straight to the fielder.",
        "No run there, tight bowling.",
        "Blocked back to the bowler.",
        "Good line and length, no score.",
        "Watchful leave outside off stump.",
        "Forward defense, maiden ball.",
        "Sharp fielding prevents the single.",
        "Played straight to mid-wicket.",
        "No shot offered, through to keeper.",
        "Dead bat, excellent technique.",
        "Defended with soft hands.",
        "Beaten by the pace and bounce.",
        "Inside edge onto the pads.",
        "Well bowled, no run scored.",
        "Compact defense, no runs added.",
        "Sharp stop in the covers.",
        "Maiden ball, excellent bowling.",
        "Pushed defensively to mid-off.",
        "Good bounce, batsman beaten.",
        "Solidly behind the line.",
        "No run, pressure building."
    ],
    "Single": [
        "Tapped away for a quick single.",
        "Pushes gently for one.",
        "Smart turn and a single taken.",
        "Nudged into the gap for one.",
        "Quick single to mid-wicket.",
        "Rotates the strike with ease.",
        "Dabs it down for a single.",
        "Clever placement, one run.",
        "Worked off the pads for one.",
        "Single taken with soft hands.",
        "Guided to third man for one.",
        "Punched off the back foot for one.",
        "Flicked to fine leg, easy single.",
        "Dropped in front of point, quick run.",
        "Single worked into the leg side.",
        "Tapped to cover, sharp running.",
        "Turned to square leg for one.",
        "Gentle push for a comfortable single.",
        "Quick feet, single stolen.",
        "Easy single behind square.",
        "Milked away for a single run.",
        "Dabbed to backward point for one.",
        "Clipped off the hips for one.",
        "Single worked with the angle.",
        "Smart cricket, keeping strike."
    ],
    "Double": [
        "Driven into the gap for two.",
        "Quick running, two runs.",
        "Nicely placed, they're off for a brace.",
        "Well timed, coming back for two.",
        "Pushed through covers for a couple.",
        "Excellent running, two taken.",
        "Placed perfectly, easy two runs.",
        "Quick feet, two runs completed.",
        "Good shot, they scamper back for two.",
        "Timing was perfect, two runs added.",
        "Sharp running between wickets.",
        "Driven wide of mid-off for two.",
        "Worked into the gap, comfortable two.",
        "Good placement yields two runs.",
        "They hustle back for the second.",
        "Two runs taken with authority.",
        "Clipped through mid-wicket for two.",
        "Excellent judge of a run, two taken.",
        "Quick turn and back for two.",
        "Well run, easy couple.",
        "Placed in the gap, two runs.",
        "Good cricket, two runs added.",
        "Driven firmly for a brace.",
        "Superb running, two completed.",
        "Nicely timed, they get two."
    ],
    "Three": [
        "Excellently placed, three runs taken!",
        "Triple taken with sharp running.",
        "Outstanding running, three completed!",
        "Superbly placed, they get three!",
        "Magnificent running for three runs!",
        "Three runs with brilliant placement!",
        "Excellent timing, three runs taken!",
        "Perfect placement yields three runs!",
        "Driven beautifully, three runs!",
        "Sharp cricket, three runs completed!",
        "Well struck, racing back for three!",
        "Three runs taken with smart cricket!",
        "Excellent shot, three runs added!",
        "Perfectly timed, they get three!",
        "Great running, three runs taken!",
        "Three runs with superb placement!",
        "Driven hard, three runs completed!",
        "Brilliant cricket, three taken!",
        "Well placed shot, three runs!",
        "Outstanding effort, three runs!",
        "Three runs with excellent timing!",
        "Superb shot placement, three runs!",
        "Quick feet, three runs completed!",
        "Magnificent stroke, three taken!",
        "Perfect execution, three runs!"
    ],
    "Four": [
        "Beautifully struck boundary!",
        "Cracking shot for four!",
        "Racing to the fence for a four.",
        "Magnificent boundary shot!",
        "Four runs with a superb drive!",
        "Crashing boundary through covers!",
        "Brilliant shot, straight to the fence!",
        "Four runs with perfect timing!",
        "Wonderful stroke for four!",
        "Boundary! What a shot!",
        "Four runs with authority!",
        "Driven superbly for four!",
        "Excellent boundary through point!",
        "Four runs with class!",
        "Boundary! Magnificent stroke!",
        "Four runs off a beautiful drive!",
        "Superb timing, boundary scored!",
        "Four runs with elegant stroke!",
        "Boundary through the covers!",
        "Four runs with perfect placement!",
        "Cracking boundary shot!",
        "Four runs with sublime timing!",
        "Boundary! Excellent cricket!",
        "Four runs off the middle!",
        "Wonderful boundary stroke!",
        "Four runs with sweet timing!",
        "Boundary carved through point!",
        "Four runs with brilliant shot!",
        "Magnificent boundary drive!",
        "Four runs in style!"
    ],
    "Six": [
        "That's a huge six!",
        "Launched into the stands!",
        "Cleared the ropes with ease!",
        "Massive six over mid-wicket!",
        "Into the crowd for six!",
        "Six runs! What a strike!",
        "Enormous hit for maximum!",
        "Six runs over long-on!",
        "Huge six into the stands!",
        "Maximum! Cleared the boundary!",
        "Six runs with tremendous power!",
        "Massive strike for six!",
        "Six runs! Magnificent hit!",
        "Launched for a huge six!",
        "Six runs over the bowler's head!",
        "Colossal six over square leg!",
        "Six runs! Pure power!",
        "Massive maximum cleared!",
        "Six runs into the upper tier!",
        "Huge strike over long-off!",
        "Six runs! What a blow!",
        "Enormous six over mid-wicket!",
        "Six runs with brutal force!",
        "Massive hit for maximum!",
        "Six runs! Cleared easily!",
        "Huge six over the bowler!",
        "Six runs into the crowd!",
        "Magnificent six over cover!",
        "Six runs! Tremendous strike!",
        "Colossal maximum achieved!"
    ],
    "Wicket": [
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
        "Out LBW! Dead plumb!",
        "Wicket! Superb delivery!",
        "Caught! Brilliant take!",
        "Bowled middle stump!",
        "Out! Perfect line and length!",
        "Caught in the deep!",
        "Wicket! Outstanding bowling!",
        "Gone! Terrific catch!",
        "Out! Unplayable delivery!",
        "Dismissed! Great cricket!",
        "Wicket falls at crucial time!",
        "Out! Magnificent bowling!",
        "Caught! Excellent reflexes!",
        "Bowled! What a ball!",
        "Gone! Perfect execution!"
    ],
    "Extras": [
        "Wide delivery, extras added.",
        "No-ball called by umpire.",
        "Leg bye taken, extra run.",
        "Byes conceded, run added.",
        "Wide down the leg side.",
        "No-ball, free hit coming up!",
        "Leg byes, off the pads.",
        "Wide called, pressure release.",
        "Byes through to the keeper.",
        "No-ball overstepping.",
        "Wide outside off stump.",
        "Leg bye deflected off pads.",
        "Byes, keeper couldn't collect.",
        "Wide down leg, extras given.",
        "No-ball called, extra run.",
        "Leg byes off the thigh pad.",
        "Wide delivery, poor line.",
        "Byes, ball beats everyone.",
        "No-ball, front foot fault.",
        "Wide called by square leg.",
        "Leg bye, off the hip.",
        "Byes, fumbled by keeper.",
        "Wide ball, wayward delivery.",
        "No-ball, overstepped clearly.",
        "Leg byes, hit on pads.",
        "Wide called, erratic bowling.",
        "Byes, missed by keeper.",
        "No-ball given, extra run.",
        "Wide delivery, poor control.",
        "Leg bye, deflection taken."
    ]
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

def get_pitch_run_multiplier(pitch: str) -> float:
    """
    Returns the run-friendly multiplier for the given pitch.
    Uses ground_conditions.yaml if available, falls back to hardcoded constants.
    """
    factor = _gc_run_factor(pitch)
    if factor is None:
        factor = PITCH_RUN_FACTOR.get(pitch, 1.0)
    return factor

def get_pitch_wicket_multiplier(pitch: str, bowling_type: str) -> float:
    """
    Returns the wicket-friendly multiplier for the given pitch and bowling type.
    Uses ground_conditions.yaml if available, falls back to hardcoded constants.
    """
    wf = _gc_wicket_factors(pitch)
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
        "Single":  0.34,
        "Double":  0.06,
        "Three":   0.005,
        "Four":    0.05,   # Low boundaries
        "Six":     0.015,
        "Wicket":  0.07,   # High wicket chance (favors pacers)
        "Extras":  0.04
    },
    "Dry": {
        "Dot":     0.38,   # Spin friendly = difficult scoring
        "Single":  0.35,
        "Double":  0.07,
        "Three":   0.02,
        "Four":    0.06,
        "Six":     0.02,
        "Wicket":  0.06,   # Favors spinners
        "Extras":  0.04
    },
    "Hard": {
        # 80/20 Batting/Bowling split implemented in compute_weighted_prob
        # Base matrix should be good for batting but not excessive
        "Dot":     0.28,
        "Single":  0.33,
        "Double":  0.10,
        "Three":   0.05,
        "Four":    0.10,   # Good boundaries
        "Six":     0.06,
        "Wicket":  0.04,   # Lower wicket chance (batsman dominated)
        "Extras":  0.04
    },
    "Flat": {
        # Pure batting paradise
        "Dot":     0.20,   # Very low dot %
        "Single":  0.30,
        "Double":  0.12,
        "Three":   0.03,
        "Four":    0.18,   # High boundaries
        "Six":     0.12,   # High sixes
        "Wicket":  0.03,   # Very low wickets
        "Extras":  0.02
    },
    "Dead": {
        # Batting paradise (200+ average, ~4-5 wickets)
        "Dot":     0.18,
        "Single":  0.32,
        "Double":  0.14,
        "Three":   0.01,
        "Four":    0.19,
        "Six":     0.10,
        "Wicket":  0.03,
        "Extras":  0.03
    }
}

# Fallback matrix for unknown pitch types (CORRECTED)
DEFAULT_SCORING_MATRIX = {
    "Dot":     0.27,   # Increased from 0.25
    "Single":  0.32,   # Increased from 0.30
    "Double":  0.11,   # Increased from 0.10
    "Three":   0.06,   # Kept same
    "Four":    0.09,   # Increased from 0.08
    "Six":     0.05,   # Increased from 0.04
    "Wicket":  0.05,   # Rounded from 0.044
    "Extras":  0.05    # Rounded from 0.048
    # Sum: 1.00 ✅
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
    batter_runs: int = 0
) -> float:
    """
    Returns a raw weight for one outcome (Dot/Single/Double/Three/Four/Six/Wicket/Extras),
    combining pitch-influence + player-skill.
    Includes special handling for "Hard" pitch (80/20 split) and "Set Batter" bonus.
    """
    # 0) Apply "Set Batter" bonus
    # Batter gets a boost if they have scored > 20 runs
    effective_batting = batting
    if batter_runs >= 20:
        effective_batting *= 1.15  # 15% skill boost for set batter
        # print(f"  [Set Batter] Rating boosted: {batting} -> {effective_batting:.1f}")

    # 1) Player-skill fraction
    skill_frac = 0.5
    
    if outcome_type in ("Dot", "Single", "Double", "Three", "Four", "Six"):
        # Run scoring: defined by Batting vs Bowling
        if (effective_batting + bowling) > 0:
            # Standard calculation
            skill_frac = effective_batting / (effective_batting + bowling)
            
            # 🔧 USER REQUEST: "If hard, batsman will have 80 and bowlers will have 20"
            # We interpret this as: On Hard pitches, the batting skill contributes 80% to the contest,
            # or the contest is heavily skewed towards batting rating.
            # Implementation: Weight the batting rating 4x more than bowling rating in the contest ratio.
            if pitch == "Hard":
                # 80% weight to batting, 20% to bowling
                skill_frac = (effective_batting * 0.8) / ((effective_batting * 0.8) + (bowling * 0.2))
                # print(f"  [Hard Pitch] Adjusted skill_frac (favors bat): {skill_frac:.4f}")
                
        else:
            skill_frac = 0.5

    elif outcome_type == "Wicket":
        # Wicket taking: defined by Bowling vs Batting (and Fielding)
        if (effective_batting + bowling) > 0:
            # Base contest
            # Normal: Prob ~ Bowling / (Bat + Bowl)
            contest_frac = bowling / (effective_batting + bowling)
            
            # Adjust regarding fielding
            skill_frac = contest_frac * (fielding / 100.0)
            
            # 🔧 USER REQUEST: Hard pitch favors batsman (bowlers only 20%)
            if pitch == "Hard":
                # Bowler struggles: Reduce the effectiveness of the bowling rating
                # contest_frac = (bowling * 0.2) / ((effective_batting * 0.8) + (bowling * 0.2))
                # Actually, let's just apply a dampener to the final skill_frac for wickets on Hard pitch
                skill_frac *= 0.5 # significantly reduce wicket taking skill impact
        else:
            skill_frac = 0.5

    # 2) Pitch-influence fraction
    pitch_frac = 1.0
    if outcome_type in ("Dot", "Single", "Double", "Three", "Four", "Six"):
        pitch_frac = get_pitch_run_multiplier(pitch)
    elif outcome_type == "Wicket":
        pitch_frac = get_pitch_wicket_multiplier(pitch, bowling_type)

    # 3) Blend Pitch & Skill
    # Default is 60% Pitch, 40% Skill. 
    # But for "Hard", we want to emphasize the skew we just calculated.
    
    # 🔧 USER REQUEST: "If flat, batsman will have advantage over bowlers"
    # Logic: Boosting the skill component if favorable to bat
    
    _weights = _gc_blending_weights()
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
    free_hit: bool = False
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
    batting = batter["batting_rating"]
    bowling = bowler["bowling_rating"]
    fielding = bowler["fielding_rating"]
    batting_hand = batter["batting_hand"]
    bowling_hand = bowler["bowling_hand"]
    bowling_type = bowler["bowling_type"]

    # 2) Get pitch-specific scoring matrix (ground_config with game mode applied, or hardcoded fallback)
    pitch_matrix = _gc_scoring_matrix(pitch) or PITCH_SCORING_MATRIX.get(pitch, DEFAULT_SCORING_MATRIX)
    # print(f"[calculate_outcome] Using scoring matrix for pitch: {pitch}")

    raw_weights = {}
    for outcome in pitch_matrix:
        base = pitch_matrix[outcome]
        # print(f"\n-- Computing weight for outcome: {outcome} (Base: {base}) --")

        if outcome == "Extras" and not allow_extras:
            raw_weights[outcome] = 0.0
            continue

        # Compute base weight via 60/40 blending
        if outcome in ("Dot", "Single", "Double", "Three", "Four", "Six"):
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak, batter_runs
            )
        elif outcome == "Wicket":
            # Additional left-arm vs right-hand boost on Green
            lr_boost = 1.0
            if (
                pitch == "Green"
                and bowling_hand == "Left"
                and batting_hand == "Right"
            ):
                lr_boost = 1.2
                # print(f"  LeftVsRightBoost applied: {lr_boost}")

            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak, batter_runs
            ) * lr_boost
            # print(f"  RawWeight after LeftVsRightBoost: {weight:.6f}")
        else:  # "Extras"
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak
            )

        # --- Phase boosts (apply to ALL outcome types, not just Extras) ---
        # Load configurable phase boosts with hardcoded fallbacks
        _phase = _gc_phase_boosts() or {}
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

        # Second innings death-over boosts
        if innings == 2 and in_death:
            if outcome in ("Single", "Double", "Three", "Four", "Six"):
                scoring_boost = _inn2_cfg.get("scoring_boost", 1.15)
                weight *= scoring_boost

            if outcome == "Wicket":
                wicket_boost_2nd = _inn2_cfg.get("wicket_boost", 1.1)
                weight *= wicket_boost_2nd

        # Ensure no negative weights
        weight = max(weight, 0.0)
        raw_weights[outcome] = weight
        # print(f"  FinalRawWeight[{outcome}]: {weight:.6f}")
    
    # 3.5) Calculate total weight first
    total_weight = sum(raw_weights.values())

    # 3.6) Apply pressure effects if provided
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

        # logger.debug(f"[calculate_outcome] RUN! Outcome: {chosen}, Runs: {result['runs']}, Description: {template}")

    logger.debug("=======================================================\n")
    return result


