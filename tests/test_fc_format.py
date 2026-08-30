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
from engine.format_config import MULTIDAY_FORMAT_REGISTRY, get_any_format
from engine.fc_declaration import (
    should_declare, should_enforce_follow_on, estimate_lead_declaration_outcome,
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
        fc_innings=3, wickets=6, score=0, lead=50, days_remaining=3,
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


def test_should_declare_uses_monte_carlo_when_inputs_supplied():
    """A lead below the flat _LEAD_BASE_THRESHOLD (250) but with a lot of
    match time left and a strong bowling attack against weak opposition
    batting should still declare under the Monte Carlo model — proving
    it's actually driving the decision, not just re-deriving the old flat
    threshold's answer."""
    import random
    base_kwargs = dict(
        fc_innings=3, wickets=6, overs_bowled_this_innings=65,
        score=0, lead=180, days_remaining=2,
    )
    # No MC inputs -> old flat-threshold path -> lead (180) < 250 -> False.
    assert should_declare(**base_kwargs) is False

    # MC inputs supplied, favorable matchup -> should now say True.
    assert should_declare(
        **base_kwargs,
        overs_remaining_in_match=180, own_bowling_strength=90,
        own_batting_strength=70, opp_batting_strength=25,
        pitch_wear=0.6, rng=random.Random(3),
    ) is True


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
    script = {"forecast": "clear", "day_events": {1: {"reason": "rain", "overs_lost": 70}}}
    m = _fc_match(days=4, fc_weather_script=script)
    assert m._fc_effective_overs_today() == 20  # 90 - 70
    r = _play_until(m, lambda mm, rr: rr.get("day_break"))
    assert r.get("day_number") == 1
    assert m.fc_day == 2
    # Day 2 has no scripted event -> back to the full 90.
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


def test_fc_ball_overs_resets_at_new_innings_and_new_ball(app):
    m = _fc_match(days=5)
    assert m.fc_ball_overs_bowled == 0
    # Drive well past the (default) 80-over new-ball mark within one long innings.
    for _ in range(600):
        r = m.next_ball()
        if r.get("match_over") or r.get("innings_end") or r.get("day_break"):
            break
        # The ball-age counter must never reach or exceed new_ball_overs —
        # it auto-resets to 0 the moment it gets there.
        assert m.fc_ball_overs_bowled < m.fmt.new_ball_overs



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
    assert high["wicket_modifier"] > low["wicket_modifier"]


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
