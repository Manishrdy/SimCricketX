import random

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
    "Green": 0.70,   # run-suppressing → 150–170 average
    "Dry":   0.70,   # spin-friendly → 150–170 average
    "Hard":  1.10,   # balanced (batting edge) → 160–180
    "Flat":  1.20,   # batting paradise → 180–200
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
    """
    factor = PITCH_RUN_FACTOR.get(pitch, 1.0)
    print(f"[get_pitch_run_multiplier] Pitch: {pitch}, RunFactor: {factor}")
    return factor

def get_pitch_wicket_multiplier(pitch: str, bowling_type: str) -> float:
    """
    Returns the wicket-friendly multiplier for the given pitch and bowling type.
    """
    slot = PITCH_WICKET_FACTOR.get(pitch, {})
    factor = slot.get(bowling_type, slot.get("default", 1.0))
    print(f"[get_pitch_wicket_multiplier] Pitch: {pitch}, BowlingType: {bowling_type}, WicketFactor: {factor}")
    return factor

# -----------------------------------------------------------------------------
# 3) Base outcome probabilities (raw frequencies)
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# 3) Pitch-specific outcome probabilities (realistic scoring patterns)
# -----------------------------------------------------------------------------
PITCH_SCORING_MATRIX = {
    "Green": {
        # Pace-friendly, lower scoring (6-7 runs/over average)
        # More dots, fewer boundaries, moderate wickets for pace bowlers
        "Dot":     0.40,
        "Single":  0.34,  # Changed from 0.32 to 0.34 (+0.02)
        "Double":  0.06,
        "Three":   0.01,
        "Four":    0.06,
        "Six":     0.02,
        "Wicket":  0.06,
        "Extras":  0.05
        # Sum: 1.00 ✅
    },
    "Dry": {
        # Spin-friendly, lower scoring (6-7 runs/over average)  
        # More dots, controlled scoring, moderate wickets for spin bowlers
        "Dot":     0.34,
        "Single":  0.33,
        "Double":  0.09,
        "Three":   0.04,
        "Four":    0.06,
        "Six":     0.02,
        "Wicket":  0.06,  # Reduced from 0.07 to prevent too many wickets
        "Extras":  0.06   # Increased to balance the total
        # Sum: 1.00 ✅
    },
    "Hard": {
        # Balanced pitch, moderate scoring (7-8 runs/over average)
        # Even distribution, slight batting advantage
        "Dot":     0.28,
        "Single":  0.31,
        "Double":  0.11,
        "Three":   0.06,
        "Four":    0.09,
        "Six":     0.04,
        "Wicket":  0.055,
        "Extras":  0.055  # Adjusted to make total = 1.0
        # Sum: 1.00 ✅
    },
    "Flat": {
        "Dot":     0.24,   # slightly fewer dots
        "Single":  0.30,   # ↑ more 1s
        "Double":  0.10,   # ↓ fewer 2s
        "Three":   0.04,   # ↓ much fewer 3s
        "Four":    0.18,   # ↑ more boundaries
        "Six":     0.06,   # same
        "Wicket":  0.04,   # same or maybe slightly ↑ for realism
        "Extras":  0.04
        # Sum: 1.00 ✅
    },
    "Dead": {
        "Dot":     0.15,
        "Single":  0.29,   # ↑ more 1s
        "Double":  0.12,   # ↓ fewer 2s
        "Three":   0.02,   # ↓
        "Four":    0.22,   # ↑ more 4s
        "Six":     0.14,   # ↑ more 6s if you want fireworks
        "Wicket":  0.02,   # slightly ↑ if required
        "Extras":  0.04
        # Sum: 1.00 ✅
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
    print("🔍 Validating pitch scoring matrices:")
    
    for pitch_type, matrix in PITCH_SCORING_MATRIX.items():
        total = sum(matrix.values())
        print(f"  {pitch_type}: {total:.3f} {'✅' if abs(total - 1.0) < 0.001 else '❌'}")
        
        if abs(total - 1.0) >= 0.001:
            print(f"    Warning: {pitch_type} matrix doesn't sum to 1.0!")
    
    # Validate default matrix too
    default_total = sum(DEFAULT_SCORING_MATRIX.values())
    print(f"  DEFAULT: {default_total:.3f} {'✅' if abs(default_total - 1.0) < 0.1 else '❌'}")

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
    streak: dict
) -> float:
    """
    Returns a raw weight for one outcome (Dot/Single/Double/Three/Four/Six/Wicket/Extras),
    combining 60% pitch-influence + 40% player-skill.
    Includes print statements to trace the computation.
    """
    print(f"\n[compute_weighted_prob] Outcome: {outcome_type}")
    print(f"  BaseProb: {base_prob}")
    print(f"  PlayerStats -> Batting: {batting}, Bowling: {bowling}, Fielding: {fielding}")
    print(f"  Pitch: {pitch}, BowlingType: {bowling_type}, Streak: {streak}")

    # 1) Player-skill fraction
    if outcome_type in ("Dot", "Single", "Double", "Three", "Four", "Six"):
        if (batting + bowling) > 0:
            skill_frac = batting / (batting + bowling)
        else:
            skill_frac = 0.5
        print(f"  SkillFrac (run): {skill_frac:.4f}")
    elif outcome_type == "Wicket":
        if (batting + bowling) > 0:
            skill_frac = (bowling / (batting + bowling)) * (fielding / 100.0)
        else:
            skill_frac = 0.5
        print(f"  SkillFrac (wicket): {skill_frac:.4f}")
    else:  # "Extras"
        skill_frac = None
        print(f"  SkillFrac (extra): N/A")

    # 2) Pitch-influence fraction
    if outcome_type in ("Dot", "Single", "Double", "Three", "Four", "Six"):
        pitch_frac = get_pitch_run_multiplier(pitch)
        print(f"  PitchFrac (run): {pitch_frac:.4f}")
    elif outcome_type == "Wicket":
        pitch_frac = get_pitch_wicket_multiplier(pitch, bowling_type)
        print(f"  PitchFrac (wicket): {pitch_frac:.4f}")
    else:  # "Extras"
        pitch_frac = None
        print(f"  PitchFrac (extra): N/A")

    # 3) Compute raw weight
    if outcome_type in ("Dot", "Single", "Double", "Three", "Four", "Six"):
        # Boundary streak penalty for Four/Six
        boundary_penalty = 1.0
        if outcome_type in ("Four", "Six") and streak.get("boundaries", 0) >= 2:
            boundary_penalty = 0.8
            print(f"  BoundaryPenalty applied: {boundary_penalty}")

        blended_frac = 0.4 * skill_frac + 0.6 * pitch_frac
        raw_weight = base_prob * blended_frac * boundary_penalty
        print(f"  BlendedFrac (run): {blended_frac:.4f}")
        print(f"  RawWeight (run): {raw_weight:.6f}")
        return raw_weight

    elif outcome_type == "Wicket":
        # Boundary streak boost for wicket
        boundary_boost = 1.0
        if streak.get("boundaries", 0) >= 2:
            boundary_boost = 1.5
            print(f"  BoundaryBoost applied: {boundary_boost}")

        blended_frac = 0.4 * skill_frac + 0.6 * pitch_frac
        raw_weight = base_prob * blended_frac * boundary_boost
        print(f"  BlendedFrac (wicket): {blended_frac:.4f}")
        print(f"  RawWeight (wicket): {raw_weight:.6f}")
        return raw_weight

    else:  # "Extras"
        # Extras depend solely on bowler error (no pitch component)
        raw_weight = base_prob * ((100 - bowling) / 100.0)
        print(f"  RawWeight (extra): {raw_weight:.6f}")
        return raw_weight

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
    innings: int = 1
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
    print("\n==================== New Delivery ====================")
    print(f"Ball context -> Over: {over_number + 1}, BatterRunsSoFar: {batter_runs}")
    print(f"Batter: {batter['name']}, BattingRating: {batter['batting_rating']}, BattingHand: {batter['batting_hand']}")
    print(f"Bowler: {bowler['name']}, BowlingRating: {bowler['bowling_rating']}, FieldingRating: {bowler['fielding_rating']}, BowlingHand: {bowler['bowling_hand']}, BowlingType: {bowler['bowling_type']}")
    print(f"Pitch type: {pitch}, Current Streak: {streak}")

    # 1) Unpack numeric ratings & attributes
    batting = batter["batting_rating"]
    bowling = bowler["bowling_rating"]
    fielding = bowler["fielding_rating"]
    batting_hand = batter["batting_hand"]
    bowling_hand = bowler["bowling_hand"]
    bowling_type = bowler["bowling_type"]

    # 2) Get pitch-specific scoring matrix
    pitch_matrix = PITCH_SCORING_MATRIX.get(pitch, DEFAULT_SCORING_MATRIX)
    print(f"[calculate_outcome] Using scoring matrix for pitch: {pitch}")

    raw_weights = {}
    for outcome in pitch_matrix:
        base = pitch_matrix[outcome]
        print(f"\n-- Computing weight for outcome: {outcome} (Base: {base}) --")

        # Compute base weight via 60/40 blending
        if outcome in ("Dot", "Single", "Double", "Three", "Four", "Six"):
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak
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
                print(f"  LeftVsRightBoost applied: {lr_boost}")

            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak
            ) * lr_boost
            print(f"  RawWeight after LeftVsRightBoost: {weight:.6f}")
        else:  # "Extras"
            weight = compute_weighted_prob(
                outcome, base,
                batting, bowling, fielding,
                pitch, bowling_type, streak
            )

        # 3) Death-over adjustments (overs 17–20 → over_number 16–19)
        if over_number >= 16:
            # Boundaries (4, 6) boost
            if outcome in ("Four", "Six"):
                if pitch in ("Flat", "Dead"):
                    boundary_boost = 1.3
                elif pitch == "Hard":
                    boundary_boost = 1.2
                else:  # Green or Dry
                    boundary_boost = 1.1
                print(f"  DeathOver: Boosting boundary ({outcome}) on {pitch} by factor {boundary_boost}")
                weight *= boundary_boost

            # Wicket boost (slight increase)
            if outcome == "Wicket":
                wicket_boost = 1.2
                print(f"  DeathOver: Boosting wicket on {pitch} by factor {wicket_boost}")
                weight *= wicket_boost

            # 4) Second innings special boosts (last 4 overs)
            if innings == 2:
                # Scoring boost by 15% for all run-scoring outcomes
                if outcome in ("Single", "Double", "Three", "Four", "Six"):
                    second_innings_scoring_boost = 1.15
                    print(f"  SecondInnings: Boosting scoring ({outcome}) by factor {second_innings_scoring_boost}")
                    weight *= second_innings_scoring_boost
                
                # Wicket boost by 3% additional
                if outcome == "Wicket":
                    second_innings_wicket_boost = 1.03
                    print(f"  SecondInnings: Additional wicket boost by factor {second_innings_wicket_boost}")
                    weight *= second_innings_wicket_boost

        # Ensure no negative weights
        weight = max(weight, 0.0)
        raw_weights[outcome] = weight
        print(f"  FinalRawWeight[{outcome}]: {weight:.6f}")

    # 4) Normalize weights into probabilities
    total_weight = sum(raw_weights.values())
    print(f"\n[calculate_outcome] Total raw weight sum: {total_weight:.6f}")
    if total_weight <= 0:
        # Fallback in pathological case
        chosen = "Dot"
        print("[calculate_outcome] Warning: Total weight <= 0, defaulting to Dot ball")
    else:
        normalized_weights = [raw_weights[o] / total_weight for o in raw_weights]
        print(f"[calculate_outcome] Normalized weights:")
        for o, nw in zip(raw_weights.keys(), normalized_weights):
            print(f"  {o}: {nw:.4f}")
        chosen = random.choices(list(raw_weights.keys()), weights=normalized_weights)[0]

    print(f"[calculate_outcome] Chosen outcome: {chosen}")

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

        # Decide wicket type with 40/30/20/10 weighting
        types = ["Caught", "Bowled", "LBW", "Run Out"]
        weights_pct = [0.4, 0.3, 0.2, 0.1]
        wicket_choice = random.choices(types, weights=weights_pct)[0]

        result["wicket_type"] = wicket_choice

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

        print(f"[calculate_outcome] WICKET! Type: {wicket_choice}, Description: {template}")

    elif chosen == "Extras":
        result["type"] = "extra"
        result["is_extra"] = True
        result["runs"] = 1  # one run per extra

        extra_types = ["Wide", "No Ball", "Leg Bye", "Byes"]
        extra_choice = random.choice(extra_types)
        template = random.choice(commentary_templates["Extras"])
        result["description"] = f"{template} ({extra_choice})"

        print(f"[calculate_outcome] EXTRA! Type: {extra_choice}, Description: {result['description']}")

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

        print(f"[calculate_outcome] RUN! Outcome: {chosen}, Runs: {result['runs']}, Description: {template}")

    print("=======================================================\n")
    return result


