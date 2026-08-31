"""
Engine-level tests for the First-Class (FC) format: multi-day, up to 2
innings per side, continuous pitch wear, wear-interpolated bowling-style
wicket factors, rule-based declaration/follow-on, and draw/innings-win/
normal-win/tie result classification.

Follows the _squad()/_match()/_play_until() factory pattern established by
tests/test_rain_dls.py (pure-engine tests, no Flask app/DB needed).
"""
import copy
import os
import sys
import uuid

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.match as match_module
import engine.fc_declaration as fc_declaration_module
from engine.format_config import MULTIDAY_FORMAT_REGISTRY, get_any_format
from engine.fc_declaration import (
    should_declare, should_enforce_follow_on, estimate_lead_declaration_outcome,
    estimate_target_defence_outcome,
    declaration_window_open, compute_innings_time_budget_overs,
)
from engine import fc_bowler_workload
from engine import fc_weather
from engine.pressure_engine import FCPressureEngine


def _squad(prefix, pace=4, spin=2):
    """11 players: 5 specialist batters, then `pace` pace bowlers, then
    `spin` spin bowlers, remaining are batting all-rounders that don't bowl."""
    names = [f"{prefix}_P{i+1}" for i in range(11)]
    pace_types = ["Fast", "Fast-medium", "Medium-fast", "Medium"]
    spin_types = ["Off spin", "Leg spin", "Finger spin", "Wrist spin"]
    players = []
    bowler_idx = 0
    for i, n in enumerate(names):
        is_pace = 5 <= i < 5 + pace
        is_spin = 5 + pace <= i < 5 + pace + spin
        will_bowl = is_pace or is_spin
        if is_pace:
            bt = pace_types[bowler_idx % len(pace_types)]
        elif is_spin:
            bt = spin_types[bowler_idx % len(spin_types)]
        else:
            bt = "Medium"
        if will_bowl:
            bowler_idx += 1
        players.append({
            "name": n,
            "role": "Bowler" if will_bowl else "Batsman",
            "batting_rating": 70, "bowling_rating": 72, "fielding_rating": 65,
            "technique_rating": 65, "temperament_rating": 65, "stamina_rating": 60,
            "batting_hand": "Right", "bowling_type": bt, "bowling_hand": "Right",
            "will_bowl": will_bowl, "is_captain": i == 0, "is_wicketkeeper": i == 4,
        })
    return players


HOME = _squad("HOM")
AWAY = _squad("AWY")


def _fc_match(days=4, pitch="Hard", toss_winner="HOM", toss_decision="Bat",
               weather_forecast="clear", fc_weather_script=None):
    data = {
        "match_id": str(uuid.uuid4()), "created_by": 1,
        "timestamp": "2026-08-09T12:00:00",
        "team_home": "HOM_1", "team_away": "AWY_1",
        "stadium": "Test Ground", "pitch": pitch,
        "toss": "Heads", "toss_winner": toss_winner, "toss_decision": toss_decision,
        "match_format": "FC", "days": days, "simulation_mode": "auto",
        "playing_xi": {"home": copy.deepcopy(HOME), "away": copy.deepcopy(AWAY)},
        "substitutes": {"home": [], "away": []},
        "weather_forecast": weather_forecast,
    }
    if fc_weather_script is not None:
        data["fc_weather_script"] = fc_weather_script
    return match_module.Match(data)


def _play_until(m, stop, limit=20000):
    """Advance an FC match ball by ball until stop(m, response) is truthy,
    or the match ends. Raises if neither happens within `limit` balls."""
    for _ in range(limit):
        r = m.next_ball()
        assert "error" not in r, r.get("error")
        if stop(m, r):
            return r
        if r.get("match_over"):
            return r
    raise AssertionError("simulation did not reach the expected state within the ball limit")


def _play_to_completion(m, limit=20000):
    return _play_until(m, lambda mm, rr: rr.get("match_over"), limit)


# ---------------------------------------------------------------------------
# 1. Format plumbing / registry
# ---------------------------------------------------------------------------

def test_fc_registry_non_mutation(app):
    fmt5 = get_any_format("FC", days=5)
    assert fmt5.days == 5
    assert fmt5.follow_on_margin == 200
    assert MULTIDAY_FORMAT_REGISTRY["FC"].days == 4  # shared singleton untouched


def test_fc_match_constructs_and_sets_is_fc(app):
    m = _fc_match(days=4)
    assert m.is_fc is True
    assert m.fmt.format_family == "multi_day"
    assert m.fc_innings == 1
    assert m.fc_day == 1
    assert m.innings == 1  # never touched by FC — must stay at its init value


# ---------------------------------------------------------------------------
# 2. Full match smoke test — the primary integration check. If any shared
#    code path assumes a T20/ListA-shaped self.fmt, this is what surfaces it.
# ---------------------------------------------------------------------------

def test_fc_full_match_completes_without_error(app):
    m = _fc_match(days=4, pitch="Hard")
    result = _play_to_completion(m)
    assert result.get("match_over") is True
    assert m.match_status in ("completed", "drawn", "tied")
    # Continuous wear must have progressed (not stuck at 0, not per-innings-reset to 0).
    assert m.match_balls_bowled > 0


def test_fc_five_day_match_completes(app):
    m = _fc_match(days=5, pitch="Dry")
    result = _play_to_completion(m)
    assert result.get("match_over") is True


# ---------------------------------------------------------------------------
# 3. Continuous pitch wear
# ---------------------------------------------------------------------------

def test_pitch_wear_continuous_across_innings_boundary(app):
    m = _fc_match(days=4)
    # Drive to the first innings break.
    _play_until(m, lambda mm, rr: rr.get("innings_end") and rr.get("innings_number") == 1)
    balls_after_innings1 = m.match_balls_bowled
    assert balls_after_innings1 > 0
    # innings_balls_bowled must have reset; match_balls_bowled must NOT.
    assert m.innings_balls_bowled < balls_after_innings1
    assert m.match_balls_bowled == balls_after_innings1
    wear_at_start_of_innings2 = m._compute_pitch_wear()
    assert wear_at_start_of_innings2 > 0.0  # continuous — did not reset to 0


# ---------------------------------------------------------------------------
# 4. Pitch x bowling-style wear interaction (ground_config layer, already
#    unit-tested in isolation — this just confirms Match wires pitch_wear
#    through correctly for a Dry pitch across a long simulation).
# ---------------------------------------------------------------------------

def test_dry_pitch_spin_wicket_share_rises_late_in_match(app):
    from engine import ground_config as gc
    early = gc.get_fc_wicket_factor_for("Dry", "Off spin", pitch_wear=0.05)
    late = gc.get_fc_wicket_factor_for("Dry", "Off spin", pitch_wear=0.95)
    assert late > early
    early_pace = gc.get_fc_wicket_factor_for("Dry", "Fast", pitch_wear=0.05)
    late_pace = gc.get_fc_wicket_factor_for("Dry", "Fast", pitch_wear=0.95)
    assert late_pace < early_pace


# ---------------------------------------------------------------------------
# 5. Bowler workload
# ---------------------------------------------------------------------------

def test_fc_bowler_never_bowls_consecutive_overs(app):
    # Ground truth is Match.over_bowler_log ({over_index: bowler_name}), not
    # a current_ball-based heuristic — a wide/no-ball bowled after the first
    # legal delivery of an over holds current_ball at 1 without starting a
    # new over, which makes any "current_ball == 1 => fresh over" check fire
    # more than once per over and falsely flag the (correct) same bowler
    # continuing their own over as a "consecutive overs" violation.
    m = _fc_match(days=4)
    full_log = {}
    for _ in range(600):
        r = m.next_ball()
        full_log.update(m.over_bowler_log)  # accumulate before a transition can reset it
        if r.get("match_over") or r.get("innings_end") or r.get("day_break"):
            break
    assert len(full_log) >= 10  # sanity: the loop actually covered multiple overs
    prev = None
    for over_idx in sorted(full_log.keys()):
        bowler = full_log[over_idx]
        assert bowler != prev, f"same bowler ({bowler}) bowled overs {over_idx - 1} and {over_idx}"
        prev = bowler


def test_fc_bowler_fatigue_floor_and_monotonic(app):
    mgr = fc_bowler_workload.FCBowlerManager(HOME, fmt=None)
    name = HOME[5]["name"]
    prev = 1.0
    for i in range(40):
        cur = mgr.get_fatigue_mult(name, stamina_rating=40)
        assert cur <= prev + 1e-9
        assert cur >= 0.55
        prev = cur
        mgr.record_over_completion(name, 3)


# ---------------------------------------------------------------------------
# 6. Declaration / follow-on heuristic (unit-level, already covered by
#    direct calls in earlier manual verification — kept here as regression
#    coverage) plus one full-engine assertion that a declaration actually
#    truncates an innings.
# ---------------------------------------------------------------------------

def test_declaration_thresholds_unit():
    # wickets == 9 only opens the declaration window — it no longer forces
    # a yes. A 9-down side with nothing worth defending keeps batting down
    # to the 10th wicket instead of protecting a tail with nothing to
    # protect.
    assert should_declare(fc_innings=1, wickets=9, overs_bowled_this_innings=25,
                           score=200, lead=0, days_remaining=4) is False
    assert should_declare(fc_innings=1, wickets=9, overs_bowled_this_innings=25,
                           score=320, lead=0, days_remaining=4) is True
    # Innings 2/3 at 9 down with no lead: same principle — nothing to
    # protect while still behind (or only level), so keep batting.
    assert should_declare(fc_innings=2, wickets=9, overs_bowled_this_innings=25,
                           score=200, lead=0, days_remaining=4) is False
    assert should_declare(fc_innings=2, wickets=9, overs_bowled_this_innings=25,
                           score=200, lead=260, days_remaining=4) is True
    # Innings 4 (the chase) is never eligible — nothing to declare to.
    assert should_declare(fc_innings=4, wickets=9, overs_bowled_this_innings=25,
                           score=200, lead=0, days_remaining=4) is False
    assert should_declare(fc_innings=1, wickets=3, overs_bowled_this_innings=10,
                           score=50, lead=0, days_remaining=4) is False


def test_declaration_from_innings_2_uses_lead_not_score():
    # Innings 2 mirrors innings 3's "lead" metric, not innings 1's raw
    # score — a big score with a small/negative lead over the first
    # innings shouldn't trigger a time-forcing declaration.
    assert should_declare(fc_innings=2, wickets=4, overs_bowled_this_innings=65,
                           score=400, lead=20, days_remaining=2) is False
    # A healthy lead at the same stage does.
    assert should_declare(fc_innings=2, wickets=6, overs_bowled_this_innings=65,
                           score=400, lead=260, days_remaining=2) is True


# ---------------------------------------------------------------------------
# 6a. Per-innings time budget — replaces the old "only in the match's last
#     2 days" time-forcing gate with pressure that scales to whatever's
#     actually left in the match when THIS innings started. Motivated by
#     Flat/Dead-pitch innings running away to 650-1200+ runs: on those
#     pitches the wicket rate is low enough that days_remaining never drops
#     to <=2 before the innings has already run unbounded.
# ---------------------------------------------------------------------------

def test_time_budget_opens_window_independent_of_days_remaining():
    # Old behavior: days_remaining=4 (>2) keeps the window shut even at 200
    # overs, regardless of score, since the time-forcing branch requires
    # days_remaining <= 2.
    assert should_declare(fc_innings=1, wickets=3, overs_bowled_this_innings=200,
                           score=280, lead=0, days_remaining=4) is False
    # New: a 180-over budget opens the window at over 200 independent of
    # days_remaining, and 280 clears the decayed Hard-pitch threshold
    # (pitch_par_factor=1.0, base 300) 20 overs past budget.
    assert should_declare(fc_innings=1, wickets=3, overs_bowled_this_innings=200,
                           score=280, lead=0, days_remaining=4,
                           innings_time_budget_overs=180) is True


def test_time_budget_decay_eases_threshold_then_floors():
    kwargs = dict(fc_innings=1, wickets=3, days_remaining=4,
                  innings_time_budget_overs=180, lead=0)
    # Right at budget: undecayed threshold (300) not yet met by 280.
    assert should_declare(overs_bowled_this_innings=180, score=280, **kwargs) is False
    # 20 overs past budget (1/3 through the 60-over decay window): eased
    # enough for 280 to clear it.
    assert should_declare(overs_bowled_this_innings=200, score=280, **kwargs) is True
    # Fully decayed (60+ overs past budget): floor is 55% of 300 = 165.
    assert should_declare(overs_bowled_this_innings=245, score=170, **kwargs) is True
    assert should_declare(overs_bowled_this_innings=245, score=160, **kwargs) is False


def test_declaration_window_open_time_budget_branch():
    assert declaration_window_open(fc_innings=1, wickets=0, overs_bowled_this_innings=50,
                                    days_remaining=5, innings_time_budget_overs=180) is False
    assert declaration_window_open(fc_innings=1, wickets=0, overs_bowled_this_innings=180,
                                    days_remaining=5, innings_time_budget_overs=180) is True
    # A budget below _TIME_FORCING_OVERS_THRESHOLD (60) — e.g. a late-
    # starting innings 3 with little of the match left — still opens the
    # window early rather than waiting for the 60-over floor.
    assert declaration_window_open(fc_innings=3, wickets=0, overs_bowled_this_innings=40,
                                    days_remaining=5, innings_time_budget_overs=40) is True


def test_mc_overrun_ceiling_overrides_unfavorable_verdict():
    import random
    kwargs = dict(
        # lead must clear _MIN_LEAD_TO_DECLARE — no captain declares 50
        # ahead however long the innings has run; this test is about the
        # overrun ceiling, not the lead floor.
        fc_innings=3, wickets=6, score=0, lead=180, days_remaining=3,
        overs_remaining_in_match=40,  # tight -> MC should say no
        own_bowling_strength=40, own_batting_strength=50,
        opp_batting_strength=70, pitch_wear=0.7,
    )
    # Below the 1.5x ceiling (budget=180 -> 270): the MC model's unfavorable
    # verdict stands.
    assert should_declare(overs_bowled_this_innings=200, innings_time_budget_overs=180,
                           rng=random.Random(7), **kwargs) is False
    # Past the ceiling with a positive lead: overridden to declare
    # regardless of what the Monte Carlo model would have said.
    assert should_declare(overs_bowled_this_innings=275, innings_time_budget_overs=180,
                           rng=random.Random(7), **kwargs) is True


def test_compute_innings_time_budget_overs():
    assert compute_innings_time_budget_overs(450) == pytest.approx(180.0)
    assert compute_innings_time_budget_overs(100) == pytest.approx(40.0)


def test_fc_innings_1_time_budget_bounds_a_flat_pitch_innings(app):
    """Engine-level regression test for the actual reported bug: a Flat/Dead
    innings 1 must not run away unbounded — it should end (declared or all
    out) at or before roughly budget (180 overs for a fresh 5-day match) +
    the full decay window (60 overs), i.e. well under 250 overs, instead of
    the 650-1200+ run / 300+ over innings seen before this fix. Reads the
    ending innings' own figures from scorecard_data, generated BEFORE
    _fc_transition_to_next_innings() resets current_over/wickets for
    innings 2 — the top-level "over"/"wickets" response keys and m.current_over
    reflect the NEW innings by the time next_ball() returns, not the one that
    just ended (same gotcha scripts/bench_fc.py works around)."""
    m = _fc_match(days=5, pitch="Flat")
    result = _play_until(m, lambda mm, rr: rr.get("innings_end") and rr.get("innings_number") == 1)
    sc = result.get("scorecard_data") or {}
    overs_str = str(sc.get("overs", "0"))
    ending_overs = float(overs_str.split(".")[0]) if overs_str else 0.0
    ending_wickets = sc.get("wickets", 0)
    assert ending_overs <= 250 or ending_wickets >= 10


def test_fc_innings_time_budget_persists_across_snapshot_roundtrip(app):
    """The frozen per-innings budget must survive a serialize/restore cycle
    onto a freshly constructed Match — a fresh Match's __init__ always
    computes an innings-1 budget, which would silently be wrong for a
    resume into a later innings if this weren't persisted explicitly."""
    m = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    m._fc_first_batting_xi = m.home_xi
    m._fc_first_bowling_xi = m.away_xi
    m.fc_innings_totals[1] = {"score": 150, "wickets": 10, "overs_str": "60.0", "side": "home"}
    m.fc_day = 3  # simulate meaningful match progress before innings 2 starts
    m._fc_start_next_innings(2, m.away_xi, m.home_xi)

    budget_before = m.fc_innings_time_budget_overs
    assert budget_before != compute_innings_time_budget_overs(m.fmt.days * m.fmt.overs_per_day)

    snap = m.serialize_fc_snapshot()
    m2 = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    m2.restore_fc_snapshot(snap)
    assert m2.fc_innings_time_budget_overs == pytest.approx(budget_before)


def test_monte_carlo_declaration_probabilities_sum_to_one():
    import random
    rng = random.Random(1)
    win, draw, loss = estimate_lead_declaration_outcome(
        lead=200, overs_remaining_in_match=150,
        own_bowling_strength=70, own_batting_strength=70,
        opp_batting_strength=70, pitch_wear=0.4, trials=500, rng=rng,
    )
    assert abs((win + draw + loss) - 1.0) < 1e-9
    assert 0.0 <= win <= 1.0 and 0.0 <= draw <= 1.0 and 0.0 <= loss <= 1.0


def test_monte_carlo_declaration_favors_bigger_lead_and_more_time():
    import random
    common_kwargs = dict(own_bowling_strength=70, own_batting_strength=70,
                          opp_batting_strength=70, pitch_wear=0.4, trials=500)

    # Paired seeds (common random numbers) so the comparison isolates the
    # lead/overs variable instead of Monte Carlo sampling noise — same
    # technique as test_technique_rating_reduces_wicket_probability.
    seed = random.randrange(2**32)
    small_lead_win, _, _ = estimate_lead_declaration_outcome(
        lead=80, overs_remaining_in_match=150, rng=random.Random(seed), **common_kwargs,
    )
    big_lead_win, _, _ = estimate_lead_declaration_outcome(
        lead=350, overs_remaining_in_match=150, rng=random.Random(seed), **common_kwargs,
    )
    assert big_lead_win > small_lead_win

    tight_time_win, _, tight_time_loss = estimate_lead_declaration_outcome(
        lead=200, overs_remaining_in_match=70, rng=random.Random(seed), **common_kwargs,
    )
    ample_time_win, _, ample_time_loss = estimate_lead_declaration_outcome(
        lead=200, overs_remaining_in_match=220, rng=random.Random(seed), **common_kwargs,
    )
    assert ample_time_win > tight_time_win


def test_monte_carlo_declaration_zero_overs_remaining_is_all_draws():
    import random
    win, draw, loss = estimate_lead_declaration_outcome(
        lead=300, overs_remaining_in_match=1, own_bowling_strength=90,
        own_batting_strength=90, opp_batting_strength=10, pitch_wear=1.0,
        trials=300, rng=random.Random(2),
    )
    # No realistic time to even take the 10th wicket, regardless of lead
    # or strength mismatch — every trial should fall through to a draw.
    assert draw > 0.95 and win < 0.05 and loss < 0.05


class _FixedGaussRng:
    """Supply exact aggregate projections to a one-trial forecast."""

    def __init__(self, *values):
        self._values = iter(values)

    def gauss(self, _mean, _stddev):
        return next(self._values)


@pytest.mark.parametrize(
    ("dismiss_overs", "scoring_rate", "expected"),
    [
        (65, 250 / 65, (1.0, 0.0, 0.0)),   # 250 all out: defence wins
        (80, 250 / 80, (1.0, 0.0, 0.0)),   # all out on final ball: win
        (100, 270 / 80, (0.0, 1.0, 0.0)),  # 270/7 at stumps: draw
        (100, 311 / 70, (0.0, 0.0, 1.0)),  # target reached in 70 overs
        (65, 325 / 65, (0.0, 0.0, 1.0)),   # reaches 311 before 325 all out
        (100, 311 / 80, (0.0, 0.0, 1.0)),  # exactly 311: chase wins
        (100, 310 / 80, (0.0, 1.0, 0.0)),  # exactly 310 at stumps: draw
    ],
)
def test_innings_three_target_defence_scenarios(dismiss_overs, scoring_rate,
                                                 expected):
    outcome = estimate_target_defence_outcome(
        lead=310,
        overs_remaining_in_match=80,
        own_bowling_strength=70,
        opp_batting_strength=70,
        trials=1,
        rng=_FixedGaussRng(dismiss_overs, scoring_rate),
    )
    assert outcome == expected


def test_target_defence_probabilities_and_cricket_invariants():
    import random

    common = dict(
        overs_remaining_in_match=120,
        own_bowling_strength=70,
        opp_batting_strength=70,
        pitch_wear=0.6,
        trials=800,
    )
    seed = 1729
    small_lead = estimate_target_defence_outcome(
        lead=180, rng=random.Random(seed), **common,
    )
    big_lead = estimate_target_defence_outcome(
        lead=320, rng=random.Random(seed), **common,
    )

    assert sum(small_lead) == pytest.approx(1.0)
    assert sum(big_lead) == pytest.approx(1.0)
    assert big_lead[0] >= small_lead[0]

    weak_attack = estimate_target_defence_outcome(
        lead=240, rng=random.Random(seed),
        **{**common, "own_bowling_strength": 40},
    )
    strong_attack = estimate_target_defence_outcome(
        lead=240, rng=random.Random(seed),
        **{**common, "own_bowling_strength": 90},
    )
    assert strong_attack[0] >= weak_attack[0]

    weak_batting = estimate_target_defence_outcome(
        lead=240, rng=random.Random(seed),
        **{**common, "opp_batting_strength": 35},
    )
    strong_batting = estimate_target_defence_outcome(
        lead=240, rng=random.Random(seed),
        **{**common, "opp_batting_strength": 90},
    )
    assert strong_batting[2] >= weak_batting[2]


def test_target_defence_with_almost_no_time_is_a_draw():
    import random

    outcome = estimate_target_defence_outcome(
        lead=310,
        overs_remaining_in_match=1,
        own_bowling_strength=90,
        opp_batting_strength=90,
        pitch_wear=1.0,
        trials=300,
        rng=random.Random(2),
    )
    assert outcome == (0.0, 1.0, 0.0)


def test_should_declare_uses_monte_carlo_when_inputs_supplied():
    """A lead below the flat _LEAD_BASE_THRESHOLD (250) but with a lot of
    match time left and a strong bowling attack against weak opposition
    batting should still declare under the Monte Carlo model — proving
    it's actually driving the decision, not just re-deriving the old flat
    threshold's answer."""
    import random
    base_kwargs = dict(
        fc_innings=3, wickets=6, overs_bowled_this_innings=65,
        score=0, lead=220, days_remaining=2,
    )
    # No MC inputs -> old flat-threshold path -> lead (220) < 250 -> False.
    assert should_declare(**base_kwargs) is False

    # MC inputs supplied, favorable matchup -> should now say True.
    assert should_declare(
        **base_kwargs,
        overs_remaining_in_match=180, own_bowling_strength=90,
        opp_batting_strength=25,
        pitch_wear=0.6, rng=random.Random(3),
    ) is True


def test_should_declare_routes_innings_two_and_three_forecasts(monkeypatch, caplog):
    calls = []

    def innings_two_forecast(**kwargs):
        calls.append(("two", kwargs))
        return 1.0, 0.0, 0.0

    def innings_three_forecast(**kwargs):
        calls.append(("three", kwargs))
        return 1.0, 0.0, 0.0

    monkeypatch.setattr(
        fc_declaration_module,
        "estimate_lead_declaration_outcome",
        innings_two_forecast,
    )
    monkeypatch.setattr(
        fc_declaration_module,
        "estimate_target_defence_outcome",
        innings_three_forecast,
    )
    common = dict(
        wickets=6,
        overs_bowled_this_innings=65,
        score=300,
        lead=180,
        days_remaining=2,
        overs_remaining_in_match=120,
        own_bowling_strength=75,
        opp_batting_strength=65,
    )

    with caplog.at_level("DEBUG", logger="engine.fc_declaration"):
        assert should_declare(
            fc_innings=2, own_batting_strength=68, **common,
        ) is True
        assert should_declare(
            fc_innings=3, own_batting_strength=None, **common,
        ) is True

    assert [name for name, _kwargs in calls] == ["two", "three"]
    assert calls[0][1]["own_batting_strength"] == 68
    assert "own_batting_strength" not in calls[1][1]
    assert "innings_two_bowl_then_chase" in caplog.text
    assert "innings_three_target_defence" in caplog.text


def test_innings_three_ignores_declaring_sides_batting_strength():
    import random

    common = dict(
        fc_innings=3,
        wickets=6,
        overs_bowled_this_innings=65,
        score=300,
        lead=240,
        days_remaining=2,
        overs_remaining_in_match=120,
        own_bowling_strength=75,
        opp_batting_strength=65,
        pitch_wear=0.6,
    )
    weak_batting_decision = should_declare(
        own_batting_strength=1,
        rng=random.Random(99),
        **common,
    )
    strong_batting_decision = should_declare(
        own_batting_strength=100,
        rng=random.Random(99),
        **common,
    )
    assert weak_batting_decision == strong_batting_decision


def test_follow_on_thresholds_unit():
    assert should_enforce_follow_on(deficit=210, follow_on_margin=200, days_remaining=3) is True
    assert should_enforce_follow_on(deficit=190, follow_on_margin=200, days_remaining=3) is False
    assert should_enforce_follow_on(deficit=250, follow_on_margin=200, days_remaining=1) is False


def test_declared_innings_2_does_not_trigger_follow_on(app):
    """Engine-level integration check: a team that DECLARES innings 2 while
    already ahead must never have the follow-on enforced on them — the
    deficit computation (a1 - b1) goes negative for a leading declarer, and
    should_enforce_follow_on's deficit>0 guard must catch that regardless of
    HOW innings 2 ended (all-out vs declared)."""
    m = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    # Skip playing out innings 1 — jump straight to a realistic innings-2
    # state via the real transition helper (so every invariant it sets up —
    # batsman_stats, current_striker, bowler_manager, etc. — is genuine).
    m._fc_first_batting_xi = m.home_xi
    m._fc_first_bowling_xi = m.away_xi
    m.fc_innings_totals[1] = {"score": 150, "wickets": 10, "overs_str": "60.0", "side": "home"}
    m._fc_start_next_innings(2, m.away_xi, m.home_xi)

    # Away is now well ahead of home's 150, deep enough into the innings and
    # match to be declaration-eligible (mirrors test_declaration_from_innings_2's
    # thresholds), with 2 days left.
    m.score = 420
    m.wickets = 6
    m.current_over = 65
    m.current_ball = 0
    m.fc_day = m.fmt.days - 1  # -> _fc_days_remaining() == 2
    # Declarations are taken at an interval (Lunch/Tea/stumps), so park the
    # match on the Lunch boundary — 30 of today's 90 overs bowled.
    m.fc_day_overs_bowled_today = 30
    m.fc_sessions_taken_today = 0

    result = m.next_ball()
    assert result.get("innings_end") is True
    assert result.get("innings_number") == 2
    assert m.follow_on_enforced is False  # never enforced on a side that's ahead
    assert m.fc_innings == 3
    # No follow-on -> innings 3 is the FIRST side's second innings (home,
    # which batted in innings 1) — same as any other non-follow-on
    # innings-2 ending, declared or not.
    assert m.batting_team is m.home_xi


# ---------------------------------------------------------------------------
# 6b. User-captained declaration/follow-on (manual simulation_mode) — AI
#     always decides in auto mode (tested above); manual mode instead
#     pauses with a pending_decision and leaves the call to the user.
# ---------------------------------------------------------------------------

def test_manual_mode_pauses_for_declaration_decision(app):
    m = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    m.simulation_mode = "manual"
    # Innings 1, deep enough in for the declaration window to be open
    # (mirrors test_declaration_thresholds_unit's wickets==9 trigger).
    m.wickets = 9
    m.current_over = 25
    m.current_ball = 0
    m.fc_day = 1

    result = m.next_ball()
    assert result.get("decision_required") is True
    assert result.get("decision_type") == "fc_declare"
    assert {opt["index"] for opt in result["decision_options"]} == {0, 1}
    assert m.fc_innings_declared is False  # AI never auto-applies in manual mode
    assert m.pending_decision is not None


def test_manual_mode_declaring_sets_the_flag_and_next_ball_transitions(app):
    m = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    m.simulation_mode = "manual"
    m.wickets = 9
    m.current_over = 25
    m.current_ball = 0
    m.fc_day = 1

    m.next_ball()  # raises the decision, doesn't advance play
    applied, status = m.submit_pending_decision(1)  # "Declare"
    assert status == 200
    assert applied["applied"]["declared"] is True
    assert m.fc_innings_declared is True
    assert m.pending_decision is None

    result = m.next_ball()  # now actually ends the innings
    assert result.get("innings_end") is True
    assert result.get("innings_number") == 1
    assert m.fc_innings == 2


def test_manual_mode_declining_declare_does_not_reask_same_over(app):
    m = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    m.simulation_mode = "manual"
    m.wickets = 9
    m.current_over = 25
    m.current_ball = 0
    m.fc_day = 1

    first = m.next_ball()
    assert first.get("decision_required") is True
    applied, status = m.submit_pending_decision(0)  # "Continue Batting"
    assert status == 200
    assert applied["applied"]["declared"] is False
    assert m._fc_declined_declare_over == 25

    # Same over boundary again (nothing about the match state that matters
    # to declaring has changed) — must NOT re-offer the declare decision,
    # or manual mode would deadlock. A next_bowler decision (a separate,
    # legitimate "who bowls this over" choice) is expected and fine here.
    second = m.next_ball()
    assert second.get("decision_type") != "fc_declare"
    assert m.wickets == 9  # unchanged by this non-decision ball either way


def test_manual_mode_pauses_for_follow_on_decision_only_when_deficit_positive(app):
    m = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    m.simulation_mode = "manual"
    m._fc_first_batting_xi = m.home_xi
    m._fc_first_bowling_xi = m.away_xi
    m.fc_innings_totals[1] = {"score": 400, "wickets": 10, "overs_str": "90.0", "side": "home"}
    m._fc_start_next_innings(2, m.away_xi, m.home_xi)
    m.score = 150  # well short -> deficit 250, a real follow-on choice
    m.wickets = 10
    m.current_ball = 0

    result = m.next_ball()
    assert result.get("decision_required") is True
    assert result.get("decision_type") == "fc_follow_on"
    assert result["decision_context"]["deficit"] == 250
    assert m.fc_innings == 2  # transition genuinely paused, not just the response


def test_manual_mode_no_follow_on_decision_when_deficit_not_positive(app):
    """Deficit <= 0 means there's nothing to decide (can't follow-on a side
    that's ahead) — manual mode must skip straight through like auto mode
    does, not pause for a decision with no real choice in it."""
    m = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    m.simulation_mode = "manual"
    m._fc_first_batting_xi = m.home_xi
    m._fc_first_bowling_xi = m.away_xi
    m.fc_innings_totals[1] = {"score": 150, "wickets": 10, "overs_str": "60.0", "side": "home"}
    m._fc_start_next_innings(2, m.away_xi, m.home_xi)
    m.score = 200  # ahead of home's 150 -> deficit negative
    m.wickets = 10
    m.current_ball = 0

    result = m.next_ball()
    assert result.get("decision_required") is not True
    assert result.get("innings_end") is True
    assert m.fc_innings == 3
    assert m.follow_on_enforced is False


def test_manual_mode_follow_on_decision_enforce_keeps_same_side_batting(app):
    m = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    m.simulation_mode = "manual"
    m._fc_first_batting_xi = m.home_xi
    m._fc_first_bowling_xi = m.away_xi
    m.fc_innings_totals[1] = {"score": 400, "wickets": 10, "overs_str": "90.0", "side": "home"}
    m._fc_start_next_innings(2, m.away_xi, m.home_xi)
    m.score = 150
    m.wickets = 10
    m.current_ball = 0

    m.next_ball()  # raises the fc_follow_on decision
    applied, status = m.submit_pending_decision(1)  # "Enforce Follow-on"
    assert status == 200
    assert applied["applied"]["enforce_fo"] is True
    assert applied["transition"]["innings_end"] is True
    assert applied["transition"]["follow_on_enforced"] is True
    assert m.follow_on_enforced is True
    assert m.fc_innings == 3
    assert m.batting_team is m.away_xi  # away bats straight on
    assert m.pending_decision is None


def test_manual_mode_follow_on_decision_decline_swaps_back_to_first_batting_side(app):
    m = _fc_match(days=5, toss_winner="HOM", toss_decision="Bat")
    m.simulation_mode = "manual"
    m._fc_first_batting_xi = m.home_xi
    m._fc_first_bowling_xi = m.away_xi
    m.fc_innings_totals[1] = {"score": 400, "wickets": 10, "overs_str": "90.0", "side": "home"}
    m._fc_start_next_innings(2, m.away_xi, m.home_xi)
    m.score = 150
    m.wickets = 10
    m.current_ball = 0

    m.next_ball()
    applied, status = m.submit_pending_decision(0)  # "Bat Again" (decline)
    assert status == 200
    assert applied["applied"]["enforce_fo"] is False
    assert m.follow_on_enforced is False
    assert m.fc_innings == 3
    assert m.batting_team is m.home_xi  # first side bats their 2nd innings


# ---------------------------------------------------------------------------
# 7. Result classification via full simulation — draws, and the innings
#    counter transitioning correctly through whatever path the match takes.
# ---------------------------------------------------------------------------

def test_fc_draw_via_single_day_match(app):
    # A 4-innings match cannot possibly finish in 1 day at ~90 overs/day —
    # forces a draw deterministically without needing to script scores.
    m = _fc_match(days=1, pitch="Flat")
    result = _play_to_completion(m, limit=3000)
    assert result.get("match_over") is True
    assert m.match_status == "drawn"
    assert m.margin_type is None


def test_fc_innings_never_exceeds_4(app):
    m = _fc_match(days=5)
    result = _play_to_completion(m)
    assert m.fc_innings <= 4
    assert m.innings == 1  # confirm never touched throughout the whole match


# ---------------------------------------------------------------------------
# 8. Weather / day-aware interruptions (Phase 2)
# ---------------------------------------------------------------------------

def test_weather_script_deterministic_for_same_seed():
    import random
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    s1 = fc_weather.generate_weather_script("storm_warning", 5, 90, rng=rng1)
    s2 = fc_weather.generate_weather_script("storm_warning", 5, 90, rng=rng2)
    assert s1 == s2


def test_weather_partial_loss_never_breaches_min_overs_last_hour():
    import random
    rng = random.Random(7)
    script = fc_weather.generate_weather_script(
        "storm_warning", 20, 90, min_overs_last_hour=15, rng=rng
    )
    for day, event in script["day_events"].items():
        if event["reason"] != "washed_out":
            effective = fc_weather.effective_overs_today(script, day, 90)
            assert effective >= 15


def test_weather_clear_forecast_rarely_interrupts():
    import random
    rng = random.Random(3)
    script = fc_weather.generate_weather_script("clear", 20, 90, rng=rng)
    # "clear" is a 5% chance per day — 20 days should see only a handful.
    assert len(script["day_events"]) <= 6


def test_weather_string_keys_tolerated_after_json_roundtrip():
    import json
    script = fc_weather.generate_weather_script(
        "storm_warning", 5, 90, rng=__import__("random").Random(1)
    )
    roundtripped = json.loads(json.dumps(script))
    for day in range(1, 6):
        # Must not raise, and must agree with the pre-roundtrip value.
        before = fc_weather.effective_overs_today(script, day, 90)
        after = fc_weather.effective_overs_today(roundtripped, day, 90)
        assert before == after


def test_fc_scripted_washout_day_ends_immediately_with_zero_overs(app):
    # Day 1 fully washed out; days 2+ clear.
    script = {"forecast": "clear", "day_events": {1: {"reason": "washed_out", "overs_lost": 90}}}
    m = _fc_match(days=4, fc_weather_script=script)
    r = m.next_ball()
    assert r.get("day_break") is True
    assert r.get("day_number") == 1
    assert m.fc_day_overs_bowled_today == 0  # not a single ball was possible
    assert m.current_over == 0
    assert "washed out" in (r.get("weather_note") or "").lower()


def test_fc_scripted_partial_loss_reduces_todays_overs(app):
    """Weather loss in isolation. The day's length is also moved by the
    over-rate model (_fc_compute_day_over_rate_adjust), so that is pinned to
    zero here to keep this a test of the weather arithmetic alone — see
    test_fc_over_rate_adjusts_days_length for the other half."""
    script = {"forecast": "clear", "day_events": {1: {"reason": "rain", "overs_lost": 70}}}
    m = _fc_match(days=4, fc_weather_script=script)
    m.fc_day_over_rate_adjust = 0
    assert m._fc_effective_overs_today() == 20  # 90 - 70
    r = _play_until(m, lambda mm, rr: rr.get("day_break"))
    assert r.get("day_number") == 1
    assert m.fc_day == 2
    # Day 2 has no scripted event -> back to the full 90.
    m.fc_day_over_rate_adjust = 0
    assert m._fc_effective_overs_today() == 90



# ---------------------------------------------------------------------------
# 9. New-ball / reverse-swing model (Phase 2)
# ---------------------------------------------------------------------------

def test_ball_condition_factor_boosts_pace_fresh_and_reverse_not_middle():
    from engine import ground_config as gc
    fresh = gc.get_fc_ball_condition_factor("Fast", 2, 80)
    middle = gc.get_fc_ball_condition_factor("Fast", 40, 80)
    reverse = gc.get_fc_ball_condition_factor("Fast", 72, 80)
    assert fresh > middle == 1.0
    assert reverse > middle
    # Reverse swing is a genuine-pace-only tool — medium pace gets nothing extra.
    assert gc.get_fc_ball_condition_factor("Medium", 72, 80) == 1.0


def test_fc_second_new_ball_can_be_delayed_then_is_forced(app):
    m = _fc_match(days=5)
    assert m.fc_ball_overs_bowled == 0
    m.current_over = m.fmt.new_ball_overs
    m.fc_ball_overs_bowled = m.fmt.new_ball_overs

    # A productive reverse-swing spell is a reason to retain the old ball.
    quick = next(p for p in m.bowling_team if p["bowling_type"] == "Fast")
    m.current_bowler = quick
    m.bowler_manager._last_bowler = quick["name"]
    m.bowler_manager._prev_over_runs[quick["name"]] = 0
    m.batsman_stats[m.current_striker["name"]]["balls"] = 40
    take, reason, _score = m._fc_should_take_new_ball()
    assert take is False
    assert "reverse_swing_working" in reason
    assert m._fc_consider_new_ball() is False
    assert m.fc_ball_overs_bowled == m.fmt.new_ball_overs

    # The captain cannot delay indefinitely: twenty overs after it became
    # available, the replacement ball is taken and its swing window begins.
    m.fc_ball_overs_bowled = m.fmt.new_ball_overs + 20
    assert m._fc_consider_new_ball() is True
    assert m.fc_ball_overs_bowled == 0
    assert m._fc_is_new_ball_window() is True
    assert "new ball is taken" in m.pending_pre_ball_commentary[-1].lower()


def test_fc_captain_takes_new_ball_immediately_for_a_long_stand(app):
    m = _fc_match(days=5)
    m.current_over = m.fmt.new_ball_overs
    m.fc_ball_overs_bowled = m.fmt.new_ball_overs
    m.current_partnership_balls = 240
    m.batsman_stats[m.current_striker["name"]]["balls"] = 80

    take, reason, score = m._fc_should_take_new_ball()
    assert take is True
    assert score >= 0.5
    assert "long_partnership" in reason


def test_delayed_new_ball_age_survives_snapshot_roundtrip(app):
    m = _fc_match(days=5)
    m.current_over = 87
    m.fc_ball_overs_bowled = 87
    snap = m.serialize_fc_snapshot()
    restored = _fc_match(days=5)
    restored.restore_fc_snapshot(snap)
    assert restored.fc_ball_overs_bowled == 87
    assert restored._fc_should_take_new_ball()[1] != "not_available"



# ---------------------------------------------------------------------------
# 10. technique_rating / temperament_rating wiring (Phase 2)
# ---------------------------------------------------------------------------

def test_technique_rating_reduces_wicket_probability():
    from engine.ball_outcome import calculate_outcome
    import collections
    import random
    fmt = get_any_format("FC", days=5)
    bowler = {"name": "B", "bowling_rating": 75, "bowling_hand": "Right", "bowling_type": "Medium", "fielding_rating": 60}

    def sample(technique, n, seed):
        random.seed(seed)
        streak = {"boundaries": 0}
        batter = {"name": "X", "batting_rating": 70, "batting_hand": "Right", "fielding_rating": 60, "technique_rating": technique}
        counts = collections.Counter()
        for _ in range(n):
            r = calculate_outcome(batter, bowler, "Hard", streak, 40, 20, innings=1, balls_faced=30,
                                   pitch_wear=0.3, format_config=fmt, batting_position=3)
            counts[r["type"]] += 1
        return counts["wicket"]

    # Independent sampling is too noisy for this at unit-test speed: technique's
    # effect on the "Wicket" skill_frac (compute_weighted_prob) is diluted by
    # normalization against the other 7 outcome weights down to a small shift
    # in the final per-ball probability (~1.5%/ball baseline) — empirically
    # this needs well over 1M deliveries per side before a plain independent
    # comparison stops flipping sign. Seeding both runs identically instead
    # pairs each delivery's random draw (common random numbers): a higher
    # technique_rating only narrows the Wicket slice of the cumulative
    # distribution, so a paired draw landing on "wicket" for the low-technique
    # run also lands on "wicket" for the high-technique run — the low-technique
    # count can only tie or exceed the high-technique one for a given seed,
    # which removes the sampling noise instead of trying to outrun it.
    state = random.getstate()
    try:
        seed = random.randrange(2**32)
        assert sample(90, n=20000, seed=seed) < sample(20, n=20000, seed=seed)
    finally:
        random.setstate(state)


def test_temperament_dampens_survival_wicket_reduction_and_collapse():
    fpe = FCPressureEngine()
    low = fpe.get_pressure_effects({
        "fc_innings": 4, "wickets": 5, "striker_balls_faced": 40, "days_remaining": 1,
        "recent_wickets": 0, "survival_mode": True, "striker_temperament": 10,
    })
    high = fpe.get_pressure_effects({
        "fc_innings": 4, "wickets": 5, "striker_balls_faced": 40, "days_remaining": 1,
        "recent_wickets": 0, "survival_mode": True, "striker_temperament": 95,
    })
    assert high["wicket_modifier"] < low["wicket_modifier"]


def test_temperament_protects_a_live_fourth_innings_chase():
    fpe = FCPressureEngine()
    base = {
        "fc_innings": 4, "wickets": 3, "striker_balls_faced": 50,
        "days_remaining": 2, "recent_wickets": 0, "survival_mode": False,
        "required_run_rate": 3.0,
    }
    rattled = fpe.get_pressure_effects({**base, "striker_temperament": 0})
    calm = fpe.get_pressure_effects({**base, "striker_temperament": 100})
    assert calm["wicket_modifier"] < rattled["wicket_modifier"]


def test_technique_has_more_identity_against_the_moving_new_ball():
    from engine.ball_outcome import compute_weighted_prob

    def wicket_weight(technique, technique_weight):
        return compute_weighted_prob(
            "Wicket", 0.02, batting=70, bowling=75, fielding=65,
            pitch="Green", bowling_type="Fast", streak={},
            format_name="FC", technique_rating=technique,
            technique_weight=technique_weight,
        )

    ordinary_gap = wicket_weight(20, 0.30) - wicket_weight(90, 0.30)
    new_ball_gap = wicket_weight(20, 0.45) - wicket_weight(90, 0.45)
    assert new_ball_gap > ordinary_gap > 0


def test_batter_stamina_only_separates_players_in_a_long_innings(app):
    low = {"stamina_rating": 0}
    high = {"stamina_rating": 100}
    assert match_module.Match._fc_batter_stamina_multiplier(low, 100) == 1.0
    assert match_module.Match._fc_batter_stamina_multiplier(high, 100) == 1.0
    assert match_module.Match._fc_batter_stamina_multiplier(low, 360) == pytest.approx(0.90)
    assert match_module.Match._fc_batter_stamina_multiplier(high, 360) == pytest.approx(1.10)


def test_fc_home_factor_is_situational_capped_and_non_mutating(app):
    m = _fc_match(days=5, pitch="Hard")
    original = m.home_xi[0]["batting_rating"]
    assert m._fc_home_advantage_factor(m.home_xi) == pytest.approx(1.04)
    assert m._fc_home_advantage_factor(m.away_xi) == 1.0

    m.pitch = "Green"
    assert m._fc_home_advantage_factor(m.home_xi) == pytest.approx(1.07)
    m.fc_innings = 3
    assert m._fc_home_advantage_factor(m.home_xi) == pytest.approx(1.10)
    assert m._fc_home_advantage_factor(m.home_xi) <= 1.10
    assert m._fc_home_skill_multiplier(m.home_xi) ** 4 == pytest.approx(1.10)
    assert m.home_xi[0]["batting_rating"] == original


def test_fc_home_factor_reaches_effective_rating_not_stored_player(monkeypatch, app):
    captured = {}

    def fixed_dot(**kwargs):
        captured["batting_rating"] = kwargs["batter"]["batting_rating"]
        captured["bowling_rating"] = kwargs["bowler"]["bowling_rating"]
        return {
            "type": "run", "runs": 0, "description": "Dot ball.",
            "wicket_type": None, "is_extra": False, "batter_out": False,
        }

    monkeypatch.setattr(match_module, "calculate_outcome", fixed_dot)
    m = _fc_match(days=5, pitch="Hard", toss_winner="HOM", toss_decision="Bat")
    raw_batting = m.current_striker["batting_rating"]
    m.next_ball()

    assert captured["batting_rating"] == pytest.approx(raw_batting * (1.04 ** 0.25))
    assert m.current_striker["batting_rating"] == raw_batting


def test_fc_toss_choice_has_no_permanent_boundary_modifier(monkeypatch, app):
    modifiers = []

    def fixed_dot(**kwargs):
        modifiers.append(kwargs["pressure_effects"]["boundary_modifier"])
        return {
            "type": "run", "runs": 0, "description": "Dot ball.",
            "wicket_type": None, "is_extra": False, "batter_out": False,
        }

    monkeypatch.setattr(match_module, "calculate_outcome", fixed_dot)
    _fc_match(pitch="Green", toss_winner="HOM", toss_decision="Bowl").next_ball()
    _fc_match(pitch="Green", toss_winner="HOM", toss_decision="Bat").next_ball()
    assert modifiers[0] == pytest.approx(modifiers[1])


def test_technique_dampens_settling_in_penalty():
    fpe = FCPressureEngine()
    low = fpe.get_pressure_effects({
        "fc_innings": 1, "wickets": 1, "striker_balls_faced": 3,
        "days_remaining": 4, "recent_wickets": 0, "striker_technique": 15,
    })
    high = fpe.get_pressure_effects({
        "fc_innings": 1, "wickets": 1, "striker_balls_faced": 3,
        "days_remaining": 4, "recent_wickets": 0, "striker_technique": 90,
    })
    assert high["boundary_modifier"] > low["boundary_modifier"]


def test_fc_match_completes_with_final_day_washed_out(app):
    # A washout scripted on the last day must not crash the draw/finalize
    # path regardless of whether the match reaches day 4 in this particular
    # random simulation.
    script = {"forecast": "clear", "day_events": {4: {"reason": "washed_out", "overs_lost": 90}}}
    m = _fc_match(days=4, fc_weather_script=script)
    result = _play_to_completion(m, limit=30000)
    assert result.get("match_over") is True
    assert m.match_status in ("completed", "drawn", "tied")


# ---------------------------------------------------------------------------
# 9. Handedness-specific rough-targeting (Phase 3)
# ---------------------------------------------------------------------------

def test_rough_targeting_neutral_on_a_fresh_pitch():
    from engine.ground_config import get_fc_rough_targeting_factor
    # A matching matchup (off-spin turning away from a left-hander) but no
    # wear yet — no footmark rough exists on ball one of the match.
    assert get_fc_rough_targeting_factor("Off spin", "Left", 0.0, "Dry") == 1.0


def test_rough_targeting_boosts_matching_handedness_at_full_wear():
    from engine.ground_config import get_fc_rough_targeting_factor
    # Off spin / Finger spin turning away from a left-hander.
    assert get_fc_rough_targeting_factor("Off spin", "Left", 1.0, "Dry") > 1.0
    assert get_fc_rough_targeting_factor("Finger spin", "Left", 1.0, "Dry") > 1.0
    # Leg spin / Wrist spin turning away from a right-hander.
    assert get_fc_rough_targeting_factor("Leg spin", "Right", 1.0, "Dry") > 1.0
    assert get_fc_rough_targeting_factor("Wrist spin", "Right", 1.0, "Dry") > 1.0


def test_rough_targeting_neutral_for_non_matching_handedness():
    from engine.ground_config import get_fc_rough_targeting_factor
    # Off spin into a right-hander (spinning INTO the bat, not away) gets no
    # rough-targeting bonus regardless of wear.
    assert get_fc_rough_targeting_factor("Off spin", "Right", 1.0, "Dry") == 1.0
    # Pace never targets footmark rough the way spin does.
    assert get_fc_rough_targeting_factor("Fast", "Left", 1.0, "Dry") == 1.0


def test_rough_targeting_ramps_linearly_with_wear():
    from engine.ground_config import get_fc_rough_targeting_factor
    half = get_fc_rough_targeting_factor("Off spin", "Left", 0.5, "Dry")
    full = get_fc_rough_targeting_factor("Off spin", "Left", 1.0, "Dry")
    assert 1.0 < half < full


def test_rough_targeting_scales_with_how_much_the_pitch_breaks_up():
    from engine.ground_config import get_fc_rough_targeting_factor
    # Dry is the "raging turner by day 5" pitch; Hard "stays close to
    # neutral throughout" — Dry's bonus must clearly exceed Hard's at the
    # same wear for the same matchup.
    dry = get_fc_rough_targeting_factor("Off spin", "Left", 1.0, "Dry")
    hard = get_fc_rough_targeting_factor("Off spin", "Left", 1.0, "Hard")
    assert dry > hard > 1.0


def test_rough_targeting_factor_reaches_calculate_outcome(monkeypatch):
    """Integration/wiring check: the factor is actually multiplied into
    calculate_outcome()'s Wicket weight, not just correct in isolation.

    Comparing two real wear levels here would conflate this new factor with
    FC's PRE-EXISTING general wear curve and bowling-style wicket-factor
    interpolation (both already ramp wicket odds up with wear, independent
    of batting hand) — a passing comparison wouldn't prove THIS factor is
    wired in at all. Monkeypatching the ground_config accessor to an
    extreme, unmistakable value isolates just the wiring, with no need for
    the large paired-seed samples a small real effect would require.
    """
    from engine.ball_outcome import calculate_outcome
    import collections
    import random
    fmt = get_any_format("FC", days=5)
    batter = {"name": "X", "batting_rating": 70, "batting_hand": "Left", "fielding_rating": 60}
    bowler = {"name": "B", "bowling_rating": 75, "bowling_hand": "Right", "bowling_type": "Off spin", "fielding_rating": 60}

    def sample(factor_value, n, seed):
        monkeypatch.setattr(
            "engine.ball_outcome._gc_fc_rough_targeting_factor",
            lambda *a, **k: factor_value,
        )
        random.seed(seed)
        streak = {"boundaries": 0}
        counts = collections.Counter()
        for _ in range(n):
            r = calculate_outcome(batter, bowler, "Dry", streak, 40, 20, innings=1, balls_faced=30,
                                   pitch_wear=0.5, format_config=fmt, batting_position=6)
            counts[r["type"]] += 1
        return counts["wicket"]

    state = random.getstate()
    try:
        seed = random.randrange(2**32)
        assert sample(1.0, n=3000, seed=seed) < sample(50.0, n=3000, seed=seed)
    finally:
        random.setstate(state)


# ---------------------------------------------------------------------------
# 12. Sessions, intervals and the over rate
# ---------------------------------------------------------------------------

def test_fc_day_is_played_in_three_sessions(app):
    """Lunch and Tea fall at the thirds of the day's schedulable overs.

    Weather is pinned to a clear script rather than left to the module's
    shared RNG: the day's length is what is under test, and an unrelated
    change upstream that shifts RNG consumption must not silently turn this
    into a rain-shortened day."""
    m = _fc_match(days=5, fc_weather_script={"forecast": "clear", "day_events": {}})
    m.fc_day_over_rate_adjust = 0
    assert m._fc_effective_overs_today() == 90
    assert m._fc_session_boundaries() == [30, 60]
    assert m._fc_current_session() == 1


def test_fc_session_boundaries_track_a_shortened_day(app):
    """A rain-shortened day still gets Lunch and Tea, in sensible places —
    not at a fixed over 30/60 that no longer exists."""
    script = {"forecast": "rain_around", "day_events": {1: {"reason": "rain", "overs_lost": 45}}}
    m = _fc_match(days=5, fc_weather_script=script)
    m.fc_day_over_rate_adjust = 0
    assert m._fc_session_boundaries() == [15, 30]   # thirds of 45


def test_fc_interval_emits_a_scorecard_and_session_summary(app):
    """Lunch/Tea pause the match on a scorecard the same way stumps does —
    following a first-class match means reading the score at the breaks."""
    m = _fc_match(days=5)
    r = _play_until(m, lambda mm, rr: rr.get("fc_interval"))
    assert r["interval_name"] == "Lunch"
    assert r["day_number"] == 1
    assert r["session_number"] == 1
    assert r["scorecard_data"]                      # the board itself
    assert r["match_over"] is False and r["innings_end"] is False
    summary = r["session_summary"]
    assert summary["overs"] > 0
    assert summary["runs"] == r["score"]            # first session of the match
    # Tea follows, and the session summary covers only the new session.
    r2 = _play_until(m, lambda mm, rr: rr.get("fc_interval"))
    assert r2["interval_name"] == "Tea"
    assert r2["session_number"] == 2
    assert r2["session_summary"]["runs"] == r2["score"] - summary["runs"]


def test_fc_stumps_reports_the_evening_session(app):
    m = _fc_match(days=5)
    r = _play_until(m, lambda mm, rr: rr.get("day_break"))
    assert r["session_number"] == 3
    assert "this session" in r["commentary"]


def test_fc_over_rate_adjusts_days_length(app):
    """A seam-heavy attack gets through fewer overs in a day than a
    spin-heavy one — a first-class day is rarely exactly 90."""
    m = _fc_match(days=5)
    all_pace = [{"will_bowl": True, "bowling_type": "Fast"} for _ in range(4)]
    all_spin = [{"will_bowl": True, "bowling_type": "Off spin"} for _ in range(4)]
    m.bowling_team = all_pace
    pace_adj = m._fc_compute_day_over_rate_adjust()
    m.bowling_team = all_spin
    spin_adj = m._fc_compute_day_over_rate_adjust()
    assert pace_adj < 0 < spin_adj
    assert pace_adj == m._FC_OVER_RATE_ALL_PACE
    assert spin_adj == m._FC_OVER_RATE_ALL_SPIN


def test_fc_over_rate_never_cuts_below_the_last_hour_minimum(app):
    script = {"forecast": "rain_around", "day_events": {1: {"reason": "rain", "overs_lost": 75}}}
    m = _fc_match(days=5, fc_weather_script=script)
    m.fc_day_over_rate_adjust = -9
    assert m._fc_effective_overs_today() >= m.fmt.min_overs_last_hour


# ---------------------------------------------------------------------------
# 13. Declaration / follow-on judgement
# ---------------------------------------------------------------------------

def test_declaration_requires_a_lead_worth_declaring_on(app):
    """A side that hasn't got its nose in front has nothing to declare on —
    closing the innings there hands over a lead for nothing."""
    import random
    kwargs = dict(fc_innings=3, wickets=8, overs_bowled_this_innings=120,
                  score=250, days_remaining=2, overs_remaining_in_match=150,
                  own_bowling_strength=75, own_batting_strength=70,
                  opp_batting_strength=70, pitch_wear=0.6)
    assert should_declare(lead=20, rng=random.Random(3), **kwargs) is False
    assert should_declare(lead=-40, rng=random.Random(3), **kwargs) is False


def test_follow_on_declined_by_a_spent_attack(app):
    """The main real-world reason a captain declines: his bowlers have just
    bowled the best part of two days."""
    base = dict(deficit=260, follow_on_margin=200, days_remaining=3)
    assert should_enforce_follow_on(**base) is True                 # no context: Law only
    assert should_enforce_follow_on(attack_overs_bowled=200, **base) is False
    # Tired but not spent, on a pitch that will be nasty to bat last on.
    assert should_enforce_follow_on(attack_overs_bowled=150,
                                    projected_final_wear=0.85, **base) is False


def test_follow_on_enforced_when_rain_threatens_the_time(app):
    """Weather about means time, not bowlers' legs, is the scarce resource."""
    assert should_enforce_follow_on(
        deficit=260, follow_on_margin=200, days_remaining=3,
        attack_overs_bowled=200, rain_risk=0.5) is True


def test_follow_on_carries_bowling_workload_into_the_next_innings(app):
    """Enforcing must not hand the attack a free reset — with a full wipe,
    the follow-on cost nothing at all."""
    mgr = fc_bowler_workload.FCBowlerManager(
        [{"name": "A", "will_bowl": True, "bowling_type": "Fast"}], fmt=None)
    for _ in range(40):
        mgr.record_over_completion("A", 3)
    assert mgr.overs_bowled("A") == 40
    mgr.reset([{"name": "A", "will_bowl": True, "bowling_type": "Fast"}],
              carry_fraction=0.45)
    assert mgr.overs_bowled("A") == 18          # 40 * 0.45
    assert mgr.get_fatigue_mult("A", stamina_rating=50) < 1.0
    # The ordinary innings change still resets to fresh.
    mgr.reset([{"name": "A", "will_bowl": True, "bowling_type": "Fast"}])
    assert mgr.overs_bowled("A") == 0


# ---------------------------------------------------------------------------
# 14. Bowling spells
# ---------------------------------------------------------------------------

def _spell_mgr(stamina=60):
    xi = [
        {"name": "Quick", "will_bowl": True, "bowling_type": "Fast",
         "bowling_rating": 78, "stamina_rating": stamina},
        {"name": "Seamer", "will_bowl": True, "bowling_type": "Fast-medium",
         "bowling_rating": 74, "stamina_rating": stamina},
        {"name": "Spinner", "will_bowl": True, "bowling_type": "Off spin",
         "bowling_rating": 72, "stamina_rating": stamina},
    ]
    return fc_bowler_workload.FCBowlerManager(xi, fmt=None), xi


def test_fc_weighted_bowler_choice_is_seeded_and_uses_all_four_preferences(app):
    import random

    mgr, xi = _spell_mgr(stamina=70)
    mgr._prev_over_runs["Quick"] = 0
    mgr._prev_over_runs["Seamer"] = 12
    scores = mgr.weighted_selection_scores(
        xi, pitch_wear=0.1, fc_day=1, new_ball_window=True,
    )

    quick = scores["Quick"]
    assert quick["composite"] == pytest.approx(
        quick["ability"] * 0.50
        + quick["conditions"] * 0.25
        + quick["freshness"] * 0.15
        + quick["recent"] * 0.10
    )
    assert quick["recent"] > scores["Seamer"]["recent"]
    assert quick["conditions"] > scores["Spinner"]["conditions"]

    def sequence(seed):
        rng = random.Random(seed)
        return [
            mgr.choose_weighted_bowler(
                xi, pitch_wear=0.1, fc_day=1,
                new_ball_window=True, rng=rng,
            )["name"]
            for _ in range(40)
        ]

    assert sequence(991) == sequence(991)


def test_new_ball_weighting_prefers_fresh_strike_pace_without_hard_filtering(app):
    import collections
    import random

    mgr, xi = _spell_mgr(stamina=80)
    rng = random.Random(31415)
    picks = collections.Counter(
        mgr.choose_weighted_bowler(
            xi, pitch_wear=0.8, fc_day=4,
            new_ball_window=True, rng=rng,
        )["name"]
        for _ in range(1000)
    )
    assert picks["Quick"] + picks["Seamer"] > picks["Spinner"]
    assert picks["Spinner"] > 0  # preference, not an illegal hard filter


def test_spell_length_differs_by_bowling_type(app):
    """A quick runs in for five to eight overs; a spinner wheels away for
    two or three times that."""
    mgr, _ = _spell_mgr()
    assert 5 <= mgr.max_spell_overs("Quick") <= 8
    assert 10 <= mgr.max_spell_overs("Spinner") <= 18
    # Stamina lengthens a spell.
    strong, _ = _spell_mgr(stamina=100)
    weak, _ = _spell_mgr(stamina=0)
    assert strong.max_spell_overs("Quick") > weak.max_spell_overs("Quick")


def test_bowler_tires_through_a_spell_and_recovers_when_rested(app):
    """The old model decayed a bowler in a straight line from his first over
    to his fortieth with no way back. Resting must actually restore him."""
    mgr, _ = _spell_mgr()
    for _ in range(6):
        mgr.record_over_completion("Quick", 3)
    tired = mgr.get_fatigue_mult("Quick")
    assert tired < 1.0

    # Someone else bowls while he puts his sweater on.
    for _ in range(10):
        mgr.record_over_completion("Spinner", 2)
    assert mgr.get_fatigue_mult("Quick") > tired, "a rest must refresh him"
    # ...and a long enough breather ends the spell, so he can come back.
    assert mgr.spell_overs("Quick") == 0
    assert mgr.is_spell_spent("Quick") is False


def test_spent_bowler_is_rotated_out_but_never_deadlocks(app):
    """Being spent is a soft signal: it drops a bowler down the ranking, but
    a small attack must still always have someone to bowl."""
    mgr, xi = _spell_mgr()
    for _ in range(mgr.max_spell_overs("Quick")):
        mgr.record_over_completion("Quick", 3)
    assert mgr.is_spell_spent("Quick") is True

    ranked = mgr.rank_by_style_preference(xi, pitch_wear=0.0, fc_day=1)
    assert ranked[-1]["name"] == "Quick", "the spent bowler goes to the back"

    # Even with every bowler spent, ranking still returns a full attack.
    for name in ("Seamer", "Spinner"):
        for _ in range(mgr.max_spell_overs(name)):
            mgr.record_over_completion(name, 3)
    assert len(mgr.rank_by_style_preference(xi, pitch_wear=0.9, fc_day=4)) == len(xi)


def test_fatigue_never_falls_below_the_effectiveness_floor(app):
    mgr, _ = _spell_mgr(stamina=0)
    for _ in range(200):
        mgr.record_over_completion("Quick", 4)
    assert mgr.get_fatigue_mult("Quick") == pytest.approx(0.55, abs=1e-9)


def test_spell_state_survives_a_resume(app):
    """A bowler mid-spell must still be mid-spell after a save/restore —
    otherwise resuming silently hands the captain a fresh attack."""
    m = _fc_match(days=5)
    # Stop as soon as somebody is carrying fatigue and BEFORE any innings
    # ends — an innings change resets the attack, so snapshotting after one
    # would be testing a fresh manager rather than a mid-spell one.
    for _ in range(600):
        r = m.next_ball()
        if r.get("innings_end") or r.get("match_over"):
            break
        if any(v > 0 for v in m.bowler_manager._fatigue.values()):
            break
    before = (dict(m.bowler_manager._spell_overs),
              dict(m.bowler_manager._rest_overs),
              dict(m.bowler_manager._fatigue))
    assert any(v > 0 for v in before[2].values()), "someone should be tired by now"

    import json
    snap = json.loads(json.dumps(m.serialize_fc_snapshot()))
    restored = _fc_match(days=5)
    restored.restore_fc_snapshot(snap)
    assert dict(restored.bowler_manager._spell_overs) == before[0]
    assert dict(restored.bowler_manager._rest_overs) == before[1]
    assert dict(restored.bowler_manager._fatigue) == before[2]


# ---------------------------------------------------------------------------
# 15. The last hour, and the nightwatchman
# ---------------------------------------------------------------------------

def _park_near_stumps(m, overs_left=3):
    """Put the match that many overs from the close of play."""
    m.fc_day_over_rate_adjust = 0
    m.fc_day_overs_bowled_today = m._fc_effective_overs_today() - overs_left


def test_last_hour_is_a_real_passage_of_play(app):
    """Nobody wants to be the man out with ten minutes left."""
    m = _fc_match(days=5, fc_weather_script={"forecast": "clear", "day_events": {}})
    m.fc_day_overs_bowled_today = 20
    m.fc_day_over_rate_adjust = 0
    assert m._fc_build_match_state()["last_hour"] is False

    _park_near_stumps(m, overs_left=5)
    assert m._fc_build_match_state()["last_hour"] is True

    engine = FCPressureEngine()
    close = engine.get_pressure_effects({"fc_innings": 1, "wickets": 3,
                                         "striker_balls_faced": 40,
                                         "days_remaining": 4, "last_hour": True})
    mid = engine.get_pressure_effects({"fc_innings": 1, "wickets": 3,
                                       "striker_balls_faced": 40,
                                       "days_remaining": 4, "last_hour": False})
    assert close["dot_bonus"] > mid["dot_bonus"]
    assert close["boundary_modifier"] < mid["boundary_modifier"]
    assert close["wicket_modifier"] < mid["wicket_modifier"]


def test_last_hour_does_not_apply_to_a_live_chase(app):
    """A side going for the win doesn't shut up shop at six o'clock."""
    m = _fc_match(days=5, fc_weather_script={"forecast": "clear", "day_events": {}})
    m.fc_innings = 4
    m.target = m.score + 40          # gettable -> not survival
    _park_near_stumps(m, overs_left=3)
    assert m._fc_build_match_state()["last_hour"] is False


def test_nightwatchman_is_sent_in_late_to_protect_a_specialist(app):
    m = _fc_match(days=5, fc_weather_script={"forecast": "clear", "day_events": {}})
    _park_near_stumps(m, overs_left=3)
    m.wickets = 3
    m.remaining_batter_indices = set(range(4, 11))

    promoted = m._auto_pick_next_batter_index()
    assert promoted != 4, "the specialist at 5 should have been protected"
    assert m.batting_team[promoted].get("will_bowl"), "a bowler goes in"
    assert m.fc_nightwatchman_used is True

    # Only one per innings — a captain does not keep promoting bowlers.
    m.remaining_batter_indices.discard(promoted)
    assert m._auto_pick_next_batter_index() == min(m.remaining_batter_indices)


def test_nightwatchman_not_used_mid_day_or_with_the_last_pair(app):
    m = _fc_match(days=5, fc_weather_script={"forecast": "clear", "day_events": {}})
    m.remaining_batter_indices = set(range(4, 11))
    m.wickets = 3

    m.fc_day_overs_bowled_today = 30          # middle of the day
    m.fc_day_over_rate_adjust = 0
    assert m._auto_pick_next_batter_index() == 4
    assert m.fc_nightwatchman_used is False

    # Late, but nine down: there is nobody left to protect.
    _park_near_stumps(m, overs_left=2)
    m.wickets = 8
    assert m._auto_pick_next_batter_index() == 4
    assert m.fc_nightwatchman_used is False


def test_nightwatchman_not_used_for_the_lower_order(app):
    """From seven down the next man in is already a lower-order player."""
    m = _fc_match(days=5, fc_weather_script={"forecast": "clear", "day_events": {}})
    _park_near_stumps(m, overs_left=2)
    m.wickets = 6
    m.remaining_batter_indices = set(range(7, 11))
    assert m._auto_pick_next_batter_index() == 7
    assert m.fc_nightwatchman_used is False


# ---------------------------------------------------------------------------
# 16. First-class commentary
# ---------------------------------------------------------------------------

def _fc_comm_state(**over):
    state = {"is_fc": True, "fc_day": 3, "fc_innings": 2, "current_over": 40,
             "current_ball": 0, "batter_runs": 20, "partnership_runs": 30,
             "fc_ball_overs_bowled": 20, "pitch_wear": 0.2, "score": 200}
    state.update(over)
    return state


def _fired(engine, context, state):
    return engine._fc_narratives(context, state, batter="B", bowler="W",
                                 team="T", fielding_team="F")


def test_fc_commentary_speaks_the_long_games_language(app):
    from engine.commentary_engine import CommentaryEngine
    eng = CommentaryEngine()

    # Second new ball: ball age back to zero, mid-innings.
    assert _fired(eng, {"runs": 0}, _fc_comm_state(fc_ball_overs_bowled=0))

    # Going past the opposition's total.
    eng2 = CommentaryEngine()
    assert _fired(eng2, {"runs": 2}, _fc_comm_state(fc_lead_before=-1))
    # ...but not while still well behind.
    assert not _fired(CommentaryEngine(), {"runs": 2},
                      _fc_comm_state(fc_lead_before=-80))

    # A wearing pitch, with a spinner on.
    assert _fired(CommentaryEngine(), {"runs": 1, "bowling_type": "Off spin"},
                  _fc_comm_state(pitch_wear=0.7))
    # A seamer on the same pitch gets no such line.
    assert not _fired(CommentaryEngine(), {"runs": 1, "bowling_type": "Fast"},
                      _fc_comm_state(pitch_wear=0.7))


def test_fc_ambient_commentary_is_said_once_not_every_over(app):
    """The last hour and a turning pitch are standing conditions. Remarking
    on them at the top of every over is noise, not commentary."""
    from engine.commentary_engine import CommentaryEngine
    eng = CommentaryEngine()
    state = _fc_comm_state(last_hour=True)
    assert _fired(eng, {"runs": 0}, state), "said the first time"
    assert not _fired(eng, {"runs": 0}, state), "and not again that day"
    # A new day earns it again.
    assert _fired(eng, {"runs": 0}, _fc_comm_state(last_hour=True, fc_day=4))


def test_fc_maidens_are_remarked_on_as_a_run_not_one_at_a_time(app):
    """A maiden is roughly one over in eight in this format — commonplace.
    A sequence of them is what strangles an innings."""
    from engine.commentary_engine import CommentaryEngine
    eng = CommentaryEngine()
    base = dict(is_maiden_over=True)
    assert not _fired(eng, {"runs": 0}, _fc_comm_state(fc_consecutive_maidens=1, **base))
    assert not _fired(eng, {"runs": 0}, _fc_comm_state(fc_consecutive_maidens=2, **base))
    assert _fired(eng, {"runs": 0}, _fc_comm_state(fc_consecutive_maidens=3, **base))


def test_limited_overs_narratives_never_fire_in_an_fc_match(app):
    """Powerplays, death overs, big overs and single maidens are all
    limited-overs concepts that used to leak into first-class commentary."""
    from engine.commentary_engine import CommentaryEngine
    eng = CommentaryEngine()
    for state in (
        {"is_fc": True, "current_over": 0, "current_ball": 0},
        {"is_fc": True, "current_over": 16, "current_ball": 0},
        {"is_fc": True, "current_over_runs": 16, "current_ball": 5},
        {"is_fc": True, "current_over_runs": 13, "current_ball": 5},
        {"is_fc": True, "is_maiden_over": True, "current_ball": 5},
    ):
        text = eng._check_narratives({"runs": 0, "type": "run"}, state) or ""
        for banned in ("Powerplay", "powerplay", "death overs", "maiden over"):
            assert banned not in text, f"{banned!r} leaked into FC: {text!r}"


def test_every_fc_narrative_template_formats_cleanly(app):
    """A template referencing a placeholder the engine doesn't supply falls
    back to raw text with a visible {brace} in it."""
    from engine.commentary_engine import CommentaryEngine
    eng = CommentaryEngine()
    kwargs = dict(batter="B", bowler="W", team="T", fielding_team="F")
    for key, templates in eng.narratives.items():
        if not key.startswith("fc_"):
            continue
        for text in templates:
            formatted = text.format(**kwargs)
            assert "{" not in formatted, f"{key}: unresolved placeholder in {text!r}"


# ---------------------------------------------------------------------------
# 17. A captain who reads the conditions
# ---------------------------------------------------------------------------

def test_declaration_bar_moves_with_the_conditions(app):
    """A first-innings declaration is not a number being reached. The same
    score means different things in different conditions."""
    from engine.fc_declaration import _conditions_threshold_multiplier as cond

    assert cond() == 1.0, "no information -> the flat threshold, unchanged"

    # Rain about: overs are the scarce resource, so declare sooner.
    assert cond(rain_risk=0.6) < 1.0
    assert cond(rain_risk=0.6) < cond(rain_risk=0.2)

    # A pitch that will be unplayable by the fourth innings — declare and
    # let THEM bat last on it.
    assert cond(projected_final_wear=0.9) < 1.0
    assert cond(projected_final_wear=0.3) == 1.0, "a good surface changes nothing"

    # A spent attack is a reason to bat on: no point setting up a
    # declaration your bowlers cannot enforce.
    assert cond(attack_freshness=0.1) > 1.0
    assert cond(attack_freshness=1.0) == 1.0

    # Always clamped — this is a captain weighing what he sees, not a
    # different heuristic.
    assert 0.70 <= cond(rain_risk=1.0, projected_final_wear=1.0) <= 1.25
    assert 0.70 <= cond(attack_freshness=0.0) <= 1.25


def test_rain_makes_a_captain_declare_on_less(app):
    """End to end through should_declare: the same score declares under a
    threatening forecast and does not under a clear one."""
    # days_remaining=2 so declaration_window_open()'s time-forcing gate is
    # satisfied; this test is about the threshold, not the window.
    kwargs = dict(fc_innings=1, wickets=7, overs_bowled_this_innings=110,
                  lead=0, days_remaining=2, pitch_par_factor=1.0)
    assert should_declare(score=270, **kwargs) is False
    assert should_declare(score=270, rain_risk=0.8, **kwargs) is True


def test_a_spent_attack_makes_a_captain_bat_on(app):
    kwargs = dict(fc_innings=1, wickets=7, overs_bowled_this_innings=110,
                  lead=0, days_remaining=2, pitch_par_factor=1.0)
    assert should_declare(score=310, **kwargs) is True
    assert should_declare(score=310, attack_freshness=0.0, **kwargs) is False


def test_attack_freshness_is_reported_for_the_side_that_must_bowl(app):
    """After declaring, the batting side has to bowl — so it is THEIR
    bowlers' freshness that matters, not the side currently in the field."""
    m = _fc_match(days=5)
    for _ in range(300):
        r = m.next_ball()
        if r.get("innings_end") or r.get("match_over"):
            break
    fresh = m._fc_declaring_side_freshness()
    assert fresh is None or 0.0 <= fresh <= 1.0
    # The declaring side's bowlers have not been bowling, so they should be
    # fresher than the attack that has been in the field all innings.
    fielding = m._fc_attack_freshness()
    if fresh is not None and fielding is not None:
        assert fresh >= fielding


# ---------------------------------------------------------------------------
# 18. Partnerships and collapses
# ---------------------------------------------------------------------------

def test_an_established_stand_wears_the_attack_down(app):
    """Partnership data was recorded for the archiver from the day FC was
    built, but fed nothing — so a 200-run stand changed nothing about the
    game. It is ramped by BALLS: a watchful 60 off 200 demoralises an attack
    far more than a breezy 60 off 90."""
    eng = FCPressureEngine()
    assert eng.partnership_grind(60) == 0.0, "a new stand has told nobody anything"
    assert eng.partnership_grind(120) == 0.0
    assert 0.0 < eng.partnership_grind(240) < 1.0
    assert eng.partnership_grind(480) == 1.0
    assert eng.partnership_grind(900) == 1.0, "clamped"

    base = {"fc_innings": 1, "wickets": 2, "striker_balls_faced": 40,
            "days_remaining": 4}
    fresh = eng.get_pressure_effects({**base, "partnership_balls": 0})
    ground = eng.get_pressure_effects({**base, "partnership_balls": 480})
    assert ground["wicket_modifier"] < fresh["wicket_modifier"]
    assert ground["boundary_modifier"] > fresh["boundary_modifier"]


def test_a_collapse_accelerates_rather_than_being_a_flat_bump(app):
    """The old model applied 1.25x however many had just gone, so three for
    twelve looked the same as one loose shot."""
    eng = FCPressureEngine()
    two = eng.collapse_severity(2)
    three = eng.collapse_severity(3)
    four = eng.collapse_severity(4)
    assert 1.0 < two < three < four, "it must cascade"
    assert eng.collapse_severity(9) == four, "and then plateau"

    # Walking in mid-collapse is the most vulnerable moment there is.
    assert eng.collapse_severity(3, striker_balls_faced=1) > \
           eng.collapse_severity(3, striker_balls_faced=50)

    # Temperament blunts it; the floor is always a no-op, never a discount.
    calm = eng.collapse_severity(4, temperament_rating=100)
    rattled = eng.collapse_severity(4, temperament_rating=0)
    assert calm < rattled
    assert eng.collapse_severity(4, temperament_rating=100) >= 1.0


def test_partnership_balls_reach_the_pressure_engine(app):
    """The wiring that was missing: recorded on Match, never handed over."""
    m = _fc_match(days=5)
    for _ in range(120):
        r = m.next_ball()
        if r.get("innings_end") or r.get("match_over"):
            break
    state = m._fc_build_match_state()
    assert "partnership_balls" in state
    assert state["partnership_balls"] == m.current_partnership_balls
    assert state["partnership_runs"] == m.current_partnership_runs


# ---------------------------------------------------------------------------
# 16. Session clock, the match-situation line, and auto-only simulation
# ---------------------------------------------------------------------------

def test_fc_intervals_do_not_move_with_the_over_rate(app):
    """Lunch is at 30 and Tea at 60 on a full day whatever the over rate.

    Sessions are two-hour blocks. A pace-heavy attack getting through 86 of
    the day's 90 overs loses those four overs at the END of the day; it does
    not drag Lunch back to over 29. The intervals used to be thirds of the
    over-rate-adjusted total, which produced "End of over 29, Lunch"."""
    script = {"forecast": "clear", "day_events": {}}
    m = _fc_match(days=5, fc_weather_script=script)
    for adjust in (-9, -4, 0, 5):
        m.fc_day_over_rate_adjust = adjust
        assert m._fc_scheduled_overs_today() == 90
        assert m._fc_effective_overs_today() == 90 + adjust
        assert m._fc_session_boundaries() == [30, 60], (
            f"over-rate adjust {adjust:+d} moved the intervals")


def test_fc_intervals_still_compress_on_a_weather_shortened_day(app):
    """Weather removes scheduled playing time, so it DOES move the intervals
    — the distinction the over rate does not get."""
    script = {"forecast": "rain_around",
              "day_events": {1: {"reason": "rain", "overs_lost": 45}}}
    m = _fc_match(days=5, fc_weather_script=script)
    m.fc_day_over_rate_adjust = 0
    assert m._fc_session_boundaries() == [15, 30]      # thirds of 45


def test_fc_session_summary_survives_an_innings_ending_inside_it(app):
    """A declaration is taken AT the interval, so the innings ends and the
    interval card is built moments later. The card used to report "0/0 in 0
    overs this session" because the session baseline was re-taken when the
    score reset."""
    m = _fc_match(days=5)
    m.fc_day_overs_bowled_today = 20
    m._fc_snapshot_session_start()
    m.fc_day_overs_bowled_today = 30
    m.score, m.wickets = 62, 3

    m._fc_start_next_innings(2, m.bowling_team, m.batting_team)

    summary = m._fc_session_summary()
    assert summary["overs"] == 10, "the session clock kept running"
    assert summary["runs"] == 62 and summary["wickets"] == 3, (
        "what the session produced before the innings ended still counts")


def test_fc_first_innings_says_nothing_about_a_target(app):
    """There is no target in the first innings, and the limited-overs line
    read "need None runs from 450 overs" when it tried to name one."""
    m = _fc_match(days=5)
    assert m.fc_innings == 1
    assert m._fc_match_situation() is None
    assert m._format_innings_complete_summary("Lunch, Day 1") == ""


def test_fc_match_situation_reads_like_a_scoreboard(app):
    """Lead / trail / a chase, stated the way the game states them."""
    m = _fc_match(days=5)
    home, away = m._get_team_name(m.home_xi), m._get_team_name(m.away_xi)

    # Innings 2: the side batting is behind, so the side in front is named.
    m.fc_innings_totals[1] = {"score": 380, "wickets": 10}
    m.fc_innings = 2
    m.batting_team, m.bowling_team = m.away_xi, m.home_xi
    m.score = 242
    assert m._fc_match_situation() == f"{home} lead by 138 runs"
    m.score = 380
    assert m._fc_match_situation() == "The scores are level"
    m.score = 381
    assert m._fc_match_situation() == f"{away} lead by 1 run"

    # Innings 3 after a follow-on: the side batting again is the one behind,
    # and a side following on trails rather than being trailed.
    m.fc_innings_totals[2] = {"score": 150, "wickets": 10}
    m.fc_innings = 3
    m.follow_on_enforced = True
    m.score = 90
    assert m._fc_match_situation() == f"{away} trail by 140 runs"
    m.score = 260
    assert m._fc_match_situation() == f"{away} lead by 30 runs"

    # Innings 3 without a follow-on: the first-innings side bats again, so
    # its two innings are measured against the other side's one.
    m.follow_on_enforced = False
    m.batting_team, m.bowling_team = m.home_xi, m.away_xi
    m.score = 40
    assert m._fc_match_situation() == f"{home} lead by 270 runs"

    # The last innings is simply a chase — no overs, no run rate.
    m.fc_innings = 4
    m.target = 246
    m.batting_team, m.bowling_team = m.away_xi, m.home_xi
    m.score = 200
    assert m._fc_match_situation() == f"{away} need 46 runs to win"
    m.score = 245
    assert m._fc_match_situation() == f"{away} need 1 run to win"


def test_fc_is_always_simulated_in_auto_mode(app):
    """Naming the next batter and bowler several thousand times over four
    days is not a mode anyone wants, so FC pins it — including for a match
    saved before the setup control was removed."""
    data = _fc_match(days=4).match_data
    data = copy.deepcopy(data)
    data["match_id"] = str(uuid.uuid4())
    data["simulation_mode"] = "manual"
    m = match_module.Match(data)
    assert m.simulation_mode == "auto"
    assert m._is_manual_mode() is False
    assert data["simulation_mode"] == "auto", "and the saved file is corrected"
