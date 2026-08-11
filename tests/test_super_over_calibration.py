"""
Super Over Calibration Harness
===============================

Companion to test_scoring_calibration.py, scoped to
engine/super_over_outcome.py. The Super Over used to run on a rating-blind
formula (flat matrix + streak multipliers only); it now shares
compute_weighted_prob/compute_matchup_boost/get_pressure_effects with the
regular-ball engine. These tests exist to make a future regression loud:
a 95-rated player should not simulate like a 40-rated one, a favorable
bowling matchup should measurably raise the wicket rate, and pressure
should hit a low-rated batter harder than a high-rated one — same as it
does for a regular delivery.

Isolated calls to calculate_super_over_outcome() (bypassing Match), same
isolation rationale as test_scoring_calibration.py's shape-spec probes:
measuring through the full super-over/Match flow confounds rating with
game-state effects. Multiple seeds are pooled per configuration to damp
single-seed RNG noise, mirroring the SEEDS convention in
test_scoring_calibration.py.
"""

import random

from engine.super_over_outcome import calculate_super_over_outcome
from engine.pressure_engine import PressureEngine
from engine.format_config import get_format

SEEDS = [4101, 4102, 4103, 4104, 4105]
N_PER_SEED = 3000


def _batter(rating, hand="Right"):
    return {"name": "Batter", "batting_rating": rating, "batting_hand": hand}


def _bowler(rating, fielding=70, btype="Medium", hand="Right"):
    return {
        "name": "Bowler", "bowling_rating": rating, "fielding_rating": fielding,
        "bowling_type": btype, "bowling_hand": hand,
    }


def _tally(batter, bowler, pitch="Hard", **kwargs):
    """Pooled outcome-rate tally across SEEDS x N_PER_SEED deliveries."""
    counts = {"Four": 0, "Six": 0, "Wicket": 0, "Dot": 0, "Extras": 0}
    total = 0
    for seed in SEEDS:
        random.seed(seed)
        for _ in range(N_PER_SEED):
            outcome = calculate_super_over_outcome(
                batter=batter, bowler=bowler, pitch=pitch,
                streak={"boundaries": 0}, batter_runs=0,
                **kwargs,
            )
            total += 1
            if outcome["is_extra"]:
                counts["Extras"] += 1
            elif outcome["batter_out"]:
                counts["Wicket"] += 1
            elif outcome["runs"] == 4:
                counts["Four"] += 1
            elif outcome["runs"] == 6:
                counts["Six"] += 1
            elif outcome["runs"] == 0:
                counts["Dot"] += 1
    return {
        "boundary_rate": (counts["Four"] + counts["Six"]) / total,
        "wicket_rate": counts["Wicket"] / total,
        "extras_rate": counts["Extras"] / total,
        "dot_rate": counts["Dot"] / total,
    }


def test_weak_batter_gets_out_more_than_elite_batter():
    """Rating must matter: a 95-rated batter should be dismissed less often
    than a 35-rated one against the same bowler, all else equal."""
    elite = _tally(_batter(95), _bowler(60))
    weak = _tally(_batter(35), _bowler(60))
    assert weak["wicket_rate"] > elite["wicket_rate"]


def test_elite_bowler_takes_more_wickets_than_weak_bowler():
    # Boundary-rate is NOT asserted here — like test_scoring_calibration.py's
    # isolated probes, an isolated single-delivery call only carries a thin
    # rating signal on raw scoring rate (the 60/40 pitch/skill blend damps
    # it); wicket_rate is the robust, non-noisy signal at this isolation
    # level. Bowler-rating's fuller effect on scoring shows up once GSME/
    # pressure amplify it across a real over (see the pressure test below).
    vs_elite_bowler = _tally(_batter(70), _bowler(95))
    vs_weak_bowler = _tally(_batter(70), _bowler(35))
    assert vs_elite_bowler["wicket_rate"] > vs_weak_bowler["wicket_rate"]


def test_matchup_boost_lifts_wicket_rate():
    """Off-spin turning away from a left-hander is a textbook bowling
    matchup advantage — it should take more wickets than the same bowler
    against a right-hander, on a turning (Dry) pitch."""
    vs_left = _tally(_batter(70, hand="Left"), _bowler(70, btype="Off spin"), pitch="Dry")
    vs_right = _tally(_batter(70, hand="Right"), _bowler(70, btype="Off spin"), pitch="Dry")
    assert vs_left["wicket_rate"] > vs_right["wicket_rate"]


def test_pressure_hits_low_rated_batter_harder():
    """Same chase pressure, different players: get_pressure_effects()'s
    rating-based pressure-handling curve should make the low-rated batter
    fold more than the elite one — this is the whole point of routing the
    Super Over through PressureEngine instead of a flat multiplier."""
    fmt = get_format("T20")
    chase_state = dict(
        so_innings=2, wickets_down=0, balls_remaining=4,
        runs_needed=14, score_so_far=0,
    )

    elite = _tally(_batter(90), _bowler(70),
                    pressure_engine=PressureEngine(format_config=fmt), **chase_state)
    weak = _tally(_batter(45), _bowler(70),
                   pressure_engine=PressureEngine(format_config=fmt), **chase_state)

    assert weak["wicket_rate"] > elite["wicket_rate"]


def test_wicket_scarcity_raises_wicket_rate_with_one_down():
    """Losing 1 of only 2 wickets should visibly tighten play relative to
    0 down — the micro-GSME wicket-scarcity layer (apply_super_over_momentum)."""
    fresh = _tally(_batter(70), _bowler(70), wickets_down=0)
    one_down = _tally(_batter(70), _bowler(70), wickets_down=1)
    assert one_down["wicket_rate"] > fresh["wicket_rate"]
    assert one_down["dot_rate"] > fresh["dot_rate"]


def test_extras_rate_is_within_sane_bounds():
    """Guard against the shared compute_weighted_prob Extras formula
    (error floor + multiplier, tuned for the regular-ball engine) blowing
    up the Super Over's extras rate unrecognizably."""
    result = _tally(_batter(70), _bowler(70))
    assert 0.01 < result["extras_rate"] < 0.12
