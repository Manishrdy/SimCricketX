"""
super_over_outcome.py
======================

Simulates each delivery in a Super Over on the same rating/matchup/pressure
architecture as a regular delivery (engine/ball_outcome.py), instead of a
separate rating-blind formula. SUPER_OVER_SCORING_MATRIX and the boundary/
wicket "excitement" multipliers are kept — a Super Over legitimately runs
hotter than an average over — but they are now a layer on top of a
skill-sensitive base, not the only thing driving the outcome.

Shared with the regular-ball engine (single source of truth, not copies):
  - compute_weighted_prob()      — pitch/skill blend, ground-config aware
  - compute_matchup_boost()      — spin-vs-hand / pace-vs-tail / angle
  - resolve_fielding_chance()    — catch/stumping drop by fielder rating
  - apply_pressure_effects_to_weights() — rating-aware pressure modifiers
  - _get_wicket_type_by_bowling(), _apply_pitch_wear()

Super-Over-specific (not reused, because the full-innings versions are
calibrated to a 120/300-ball innings and misfire at n=6 — see
PressureEngine.calculate_super_over_pressure() and
game_state_engine.apply_super_over_momentum() for the full rationale):
  - PressureEngine.calculate_super_over_pressure()
  - apply_super_over_momentum()
"""

import random
from engine.ball_outcome import (
    compute_weighted_prob,
    compute_matchup_boost,
    resolve_fielding_chance,
    apply_pressure_effects_to_weights,
    _get_wicket_type_by_bowling,
    _apply_pitch_wear,
)
from engine.game_state_engine import apply_super_over_momentum

# -----------------------------------------------------------------------------
# Super Over outcome probabilities (more exciting than a regular over)
# -----------------------------------------------------------------------------
SUPER_OVER_SCORING_MATRIX = {
    "Dot":     0.20,   # Reduced — high pressure, fewer leaves/blocks
    "Single":  0.25,   # Quick singles under pressure
    "Double":  0.10,   # Running twos
    "Three":   0.005,  # Almost never in a super over
    "Four":    0.18,   # Boundaries flow in super overs
    "Six":     0.12,   # Big hits under pressure
    "Wicket":  0.04,   # Pressure wickets
    "Extras":  0.045   # Bowler nerves
}

_RUN_OUTCOMES = ("Dot", "Single", "Double", "Three")
_BOUNDARY_OUTCOMES = ("Four", "Six")


def calculate_super_over_outcome(
    batter: dict,
    bowler: dict,
    pitch: str,
    streak: dict,
    batter_runs: int,
    *,
    balls_faced: int = 0,
    so_innings: int = 1,
    wickets_down: int = 0,
    balls_remaining: int = 6,
    runs_needed: int = None,
    score_so_far: int = 0,
    history: list = None,
    pitch_wear: float = 0.0,
    fielding_team: list = None,
    pressure_engine=None,
    ground_config_override: dict = None,
) -> dict:
    """
    Simulates one delivery in a Super Over.

    Parameters
    ----------
    batter, bowler : player dicts, same shape as calculate_outcome().
    pitch          : one of {"Green", "Flat", "Dry", "Hard", "Dead"}.
    streak         : {"boundaries": int} — boundaries hit so far this over
                     by the current batter. Also drives compute_weighted_prob's
                     own >=2-boundary streak penalty/boost.
    batter_runs, balls_faced : this Super Over innings only.
    so_innings     : 1 (setting the target) or 2 (chasing it).
    wickets_down   : 0 or 1 (the innings ends at 2).
    balls_remaining: 1-6, including the ball about to be bowled.
    runs_needed    : innings 2 only — runs required to win.
    score_so_far   : this Super Over innings' score before this ball.
    history        : list[dict] of this Super Over's own prior deliveries
                     (game_state_engine.make_ball_event() shape) — NOT the
                     main innings' ball history.
    pitch_wear     : carried over from the end of the main match — the
                     pitch doesn't reset just because a new contest starts.
    fielding_team  : the bowling XI, for fielder-rating-driven catch drops.
    pressure_engine: a PressureEngine instance (reused across balls is fine —
                     the Super Over path doesn't touch its recent_events).
    ground_config_override : per-match ground config snapshot, same as
                     calculate_outcome()'s ground_config_override.

    Returns: same result-dict shape as calculate_outcome() /
    the previous calculate_super_over_outcome().
    """
    batting = batter["batting_rating"]
    bowling = bowler["bowling_rating"]
    fielding = bowler["fielding_rating"]
    batting_hand = batter["batting_hand"]
    bowling_hand = bowler["bowling_hand"]
    bowling_type = bowler["bowling_type"]

    matchup_boost, boundary_suppression = compute_matchup_boost(
        bowling_type, bowling_hand, batting_hand, batting, pitch
    )

    consecutive_dots = 0
    for event in reversed(history or []):
        if event.get("label") == "Dot":
            consecutive_dots += 1
        else:
            break

    so_state = {
        "wickets_down": wickets_down,
        "so_innings": so_innings,
        "balls_remaining": balls_remaining,
        "runs_needed": runs_needed,
        "consecutive_dots": consecutive_dots,
    }

    pressure_effects = None
    if pressure_engine is not None:
        pressure_score = pressure_engine.calculate_super_over_pressure(so_state)
        pressure_effects = pressure_engine.get_pressure_effects(
            pressure_score, batting, bowling, pitch
        )

    # 1) Base weights via the shared pitch/skill blend (same alpha/beta,
    #    Hard-pitch skew, new-batter vulnerability as a regular delivery).
    raw_weights = {}
    for outcome, base_prob in SUPER_OVER_SCORING_MATRIX.items():
        weight = compute_weighted_prob(
            outcome, base_prob, batting, bowling, fielding, pitch, bowling_type,
            streak, batter_runs, balls_faced,
            format_name=None, config=ground_config_override,
        )

        if outcome in _BOUNDARY_OUTCOMES:
            # Super-over excitement: boundaries fly more freely than an
            # average over, tempered by a bowler's favorable matchup.
            weight *= 1.2
            if boundary_suppression < 1.0:
                weight *= boundary_suppression
        elif outcome == "Wicket":
            weight *= matchup_boost * 1.3  # pressure-wicket excitement

        raw_weights[outcome] = max(weight, 0.0)

    # 2) Pitch wear carried over from the main match.
    if pitch_wear > 0.0:
        raw_weights = _apply_pitch_wear(raw_weights, pitch, pitch_wear)

    # 3) Super Over micro-GSME: momentum from this over's own deliveries,
    #    wicket scarcity (1 of only 2 down), required rate vs the neutral
    #    baseline — see apply_super_over_momentum()'s docstring for why this
    #    isn't just apply_game_state_to_probs() with Super Over numbers.
    raw_weights = apply_super_over_momentum(raw_weights, {
        **so_state,
        "history": history or [],
        "score_so_far": score_so_far,
    })

    total_weight = sum(raw_weights.values())

    # 4) Rating-aware pressure effects (same get_pressure_effects() a
    #    regular delivery uses — a 95-rated player resists this far more
    #    than a 60-rated one).
    if pressure_effects:
        raw_weights, total_weight = apply_pressure_effects_to_weights(
            raw_weights, pressure_effects, total_weight
        )

    # 5) Normalize and choose.
    outcomes = list(raw_weights.keys())
    if total_weight <= 0:
        chosen = "Dot"
    else:
        normalized = [raw_weights[o] / total_weight for o in outcomes]
        chosen = random.choices(outcomes, weights=normalized, k=1)[0]

    result = {
        "type": None,
        "runs": 0,
        "description": "",
        "wicket_type": None,
        "is_extra": False,
        "batter_out": False
    }

    commentary_templates = {
        "Dot": [
            "Pressure delivery! No run.",
            "Dot ball under pressure."
        ],
        "Single": [
            "Quick single under pressure.",
            "Rotates strike in super over."
        ],
        "Double": [
            "Pushed into the gap for two!",
            "Great running, two runs."
        ],
        "Three": [
            "Excellent placement for three!",
            "Superb running between wickets!"
        ],
        "Four": [
            "BOUNDARY! Crucial four in super over!",
            "What a shot under pressure! FOUR!"
        ],
        "Six": [
            "MASSIVE SIX! Gone into the stands!",
            "HUGE hit! Six runs in super over!"
        ],
        "Wicket": [
            "WICKET! Pressure gets to batsman!",
            "OUT! Crucial breakthrough!"
        ],
        "Extras": [
            "Extra runs under pressure.",
            "Pressure gets to bowler - extras."
        ]
    }

    if chosen == "Wicket":
        result["type"] = "wicket"
        result["runs"] = 0
        result["batter_out"] = True

        wicket_types, weights_pct = _get_wicket_type_by_bowling(bowling_type)
        chosen_wicket = random.choices(wicket_types, weights=weights_pct, k=1)[0]
        result["wicket_type"] = chosen_wicket
        result["description"] = random.choice(commentary_templates["Wicket"])

        # Run Out can happen attempting 1, 2, or 3 runs.
        if chosen_wicket == "Run Out":
            result["runs"] = random.choices([0, 1, 2], weights=[0.30, 0.60, 0.10], k=1)[0]

        # Fielding: same catch-drop mechanic as a regular delivery — the
        # fielder is picked first, then THEIR rating drives the drop odds.
        if chosen_wicket in ("Caught", "Stumped"):
            dropped, fielder_name, drop_runs = resolve_fielding_chance(
                fielding_team, bowler.get("name"), chosen_wicket
            )
            if fielder_name:
                result["fielder_name"] = fielder_name
            if dropped:
                result["type"] = "run"
                result["batter_out"] = False
                result["wicket_type"] = None
                result["runs"] = drop_runs
                result["dropped_catch"] = True
                if fielder_name:
                    result["description"] = f"DROPPED! {fielder_name} spills a sitter under Super Over pressure!"
                else:
                    result["description"] = "DROPPED! The chance goes begging under Super Over pressure!"

    elif chosen == "Extras":
        result["type"] = "extra"
        result["is_extra"] = True

        extra_types   = ["Wide", "No Ball", "Leg Bye", "Byes"]
        extra_weights = [0.40,   0.25,      0.20,      0.15]
        extra_choice  = random.choices(extra_types, weights=extra_weights)[0]

        if extra_choice == "Wide":
            result["runs"] = 1
        elif extra_choice == "No Ball":
            result["runs"] = 1
        elif extra_choice == "Leg Bye":
            result["runs"] = random.choices([1, 2], weights=[0.80, 0.20])[0]
        elif extra_choice == "Byes":
            result["runs"] = random.choices([1, 2, 4], weights=[0.85, 0.10, 0.05])[0]

        result["extra_type"] = extra_choice
        result["description"] = f"{random.choice(commentary_templates['Extras'])} ({extra_choice})"

    else:
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
        result["description"] = random.choice(commentary_templates[chosen])

    return result
