"""
Integration tests for the rain/DLS system: scripted weather events injected
into full engine simulations.

The weather script is deterministic test input (no RNG): each test pins an
event to a global over and asserts the engine's revision arithmetic against
the DLS module directly.
"""
import copy
import os
import sys
import uuid

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.match as match_module
from engine import dls
from engine.format_config import FORMAT_REGISTRY


def _squad(names):
    return [{
        "name": n,
        "role": "Bowler" if i >= 6 else "Batsman",
        "batting_rating": 70, "bowling_rating": 70, "fielding_rating": 70,
        "batting_hand": "Right", "bowling_type": "Medium", "bowling_hand": "Right",
        "will_bowl": i >= 6, "is_captain": i == 0, "is_wicketkeeper": i == 4,
    } for i, n in enumerate(names)]


HOME = _squad([f"HOME_P{i+1}" for i in range(11)])
AWAY = _squad([f"AWY_P{i+1}" for i in range(11)])


def _match(weather_script=None, match_format="T20"):
    overs = 20 if match_format == "T20" else 50
    data = {
        "match_id": str(uuid.uuid4()), "created_by": 1,
        "timestamp": "2026-08-09T12:00:00",
        "team_home": "HOM_1", "team_away": "AWY_1",
        "stadium": "Test Ground", "pitch": "Hard",
        "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
        "match_format": match_format, "overs": overs, "simulation_mode": "auto",
        "playing_xi": {"home": copy.deepcopy(HOME), "away": copy.deepcopy(AWAY)},
        "substitutes": {"home": [], "away": []},
    }
    if weather_script is not None:
        data["weather_forecast"] = weather_script.get("forecast", "storm_warning")
        data["weather_script"] = weather_script
    else:
        data["weather_forecast"] = "clear"
    return match_module.Match(data)


def _script(*events, forecast="storm_warning"):
    return {"forecast": forecast, "events": list(events)}


def _play_until(m, stop, limit=1500):
    """Advance the match ball by ball until stop(m, response) is truthy."""
    for _ in range(limit):
        r = m.next_ball()
        assert "error" not in r, r.get("error")
        if stop(m, r):
            return r
        if r.get("match_over") or m.innings >= 3:
            return r
    raise AssertionError("simulation did not reach the expected state")


def _play_to_completion(m, limit=1500):
    return _play_until(m, lambda mm, rr: rr.get("match_over") or mm.innings >= 4, limit)


# ── Baseline: clear skies is a strict no-op ───────────────────────────────────

def test_clear_forecast_generates_no_events_and_no_dls(app):
    m = _match()   # no script -> engine generates one from "clear"
    assert m.weather_script["events"] == []
    r = _play_to_completion(m)
    assert m.rain_affected is False
    if m.innings == 3:   # normal completion (not a super over)
        assert "DLS" not in (m.result or "")


# ── Chase reduced mid-innings ─────────────────────────────────────────────────

def test_chase_reduced_mid_innings_revises_target_and_quota(app):
    # Rain at global over 25 = 5 completed overs into the T20 chase, 6 lost.
    m = _match(_script({"at_global_over": 25, "overs_lost": 6}))
    r = _play_until(m, lambda mm, rr: mm.innings == 2 and mm.rain_affected)
    if m.innings >= 3:
        pytest.skip("chase ended before the scripted over (all out inside 5 overs)")

    assert m.rain_affected is True
    assert m.overs == 14                       # 20 - 6
    assert m.fmt.max_bowler_overs == 3         # ceil(14/5)
    assert m.data["rain_affected"] is True

    # Independent recomputation of the revised target from the ledgers.
    event = m.rain_events_log[-1]
    assert event["outcome"] == "resume"
    r1 = m.dls_ledger_innings1.available()
    r2 = m.dls_ledger_innings2.available()
    assert r1 == pytest.approx(dls.resources_remaining(20, 0))   # dry innings 1
    interruption = m.dls_ledger_innings2.interruptions[0]
    lost = (dls.resources_remaining(interruption["overs_remaining_at_stop"],
                                    interruption["wickets_lost"])
            - dls.resources_remaining(interruption["overs_remaining_at_resume"],
                                      interruption["wickets_lost"]))
    assert r2 == pytest.approx(dls.resources_remaining(20, 0) - lost)
    assert m.target == dls.compute_target(m.first_innings_score, r1, r2)

    # Live par + payload fields are exposed while the chase continues.
    if not r.get("match_over"):
        r2b = m.next_ball()
        if not r2b.get("match_over") and "dls_par" in r2b:
            assert r2b["rain_affected"] is True
            assert isinstance(r2b["dls_par"], int)

    # Quotas: nobody may exceed the revised cap for the rest of the chase.
    _play_to_completion(m)
    if m.innings == 3:
        assert max(m.bowler_manager._quota.values(), default=0) <= 3


# ── First-innings interruption ────────────────────────────────────────────────

def test_innings1_interruption_shortens_both_innings(app):
    # Rain after 8 overs of innings 1, 6 overs lost -> 14 overs a side.
    m = _match(_script({"at_global_over": 8, "overs_lost": 6}))
    _play_until(m, lambda mm, rr: mm.rain_affected)
    assert m.innings == 1
    assert m.overs == 14
    assert m.fmt.max_bowler_overs == 3
    assert m.rain_events_log[-1]["outcome"] == "resume"

    # Innings 1 must end at (or before, if all out) the revised allocation.
    _play_until(m, lambda mm, rr: mm.innings == 2)
    assert m._innings1_overs_bowled <= 14

    # The chase target must come from the DLS branch, not S+1 pro-rating.
    r1 = m.dls_ledger_innings1.available()
    r2 = m.dls_ledger_innings2.available()
    expected = dls.compute_target(
        m.first_innings_score, r1, r2, m._dls_g50()
    )
    assert m.target == expected
    assert r1 < dls.resources_remaining(20, 0)   # innings 1 paid for the rain


# ── Abandonment paths ─────────────────────────────────────────────────────────

def test_storm_early_in_innings1_is_no_result(app):
    # Rain after 2 overs losing 19: revised 1 < the 5-over minimum.
    m = _match(_script({"at_global_over": 2, "overs_lost": 19}))
    r = _play_to_completion(m)
    assert r.get("match_over") is True
    assert m.match_status == "no_result"
    assert m.winner_is_home is None
    assert "abandoned" in m.result.lower()
    assert m.innings == 3
    # Second-innings stats must not be polluted by first-innings data.
    assert m.second_innings_batting_stats == {}


def test_chase_terminated_is_decided_on_dls_par(app):
    # Chase stopped dead after 6 overs (>= 5 minimum): par decides.
    m = _match(_script({"at_global_over": 26, "overs_lost": 20}))
    r = _play_to_completion(m)
    assert r.get("match_over") is True
    if not m.rain_affected:
        pytest.skip("chase ended inside 6 overs; scripted event never fired")
    assert m.match_status in ("completed", "tied")
    assert "DLS method" in m.result
    assert m.rain_events_log[-1]["outcome"] in ("chase_terminated", "resume")


# ── Pre-chase reduction (rain at the innings break) ───────────────────────────

def test_rain_at_innings_break_reduces_chase_before_it_starts(app):
    m = _match()   # clear script; we inject the event at the break
    _play_until(m, lambda mm, rr: mm.innings == 2)
    original_target = m.target
    m.weather_script = {
        "forecast": "rain_around",
        "events": [{"at_global_over": m._innings1_overs_bowled, "overs_lost": 6}],
    }

    r = m.next_ball()   # first call of the chase consumes the deferred event
    if r.get("match_over"):
        pytest.skip("match ended on the first chase ball")
    assert m.rain_affected is True
    assert m.overs == 14
    assert m.fmt.max_bowler_overs == 3
    # Reduced overs with all 10 wickets in hand: DLS demands more than
    # a naive overs-ratio would.
    naive = int(m.first_innings_score * 14 / 20) + 1
    assert m.target >= naive
    assert m.target <= original_target


# ── List A sanity ─────────────────────────────────────────────────────────────

def test_lista_chase_reduction_uses_standard_table(app):
    # Rain 10 completed overs into the List A chase, 7 lost -> 43 overs.
    m = _match(_script({"at_global_over": 60, "overs_lost": 7}), match_format="ListA")
    _play_until(m, lambda mm, rr: mm.innings == 2 and mm.rain_affected, limit=4000)
    if m.innings >= 3:
        pytest.skip("match ended before the scripted over")
    assert m.overs == 43
    assert m.fmt.max_bowler_overs == 9   # ceil(43/5)
    r1 = m.dls_ledger_innings1.available()
    r2 = m.dls_ledger_innings2.available()
    assert m.target == dls.compute_target(m.first_innings_score, r1, r2)
    _play_to_completion(m, limit=4000)
    if m.innings == 3:
        assert max(m.bowler_manager._quota.values(), default=0) <= 9


# ── Registry isolation ────────────────────────────────────────────────────────

def test_rain_revision_never_mutates_shared_format_registry(app):
    before_overs = FORMAT_REGISTRY["T20"].overs
    before_quota = FORMAT_REGISTRY["T20"].max_bowler_overs
    m = _match(_script({"at_global_over": 8, "overs_lost": 6}))
    _play_until(m, lambda mm, rr: mm.rain_affected)
    assert m.fmt.overs == 14
    assert FORMAT_REGISTRY["T20"].overs == before_overs
    assert FORMAT_REGISTRY["T20"].max_bowler_overs == before_quota


@pytest.fixture
def scripted_match(monkeypatch):
    """Play real deliveries so wickets, batter availability and stats agree."""
    m = _match()
    monkeypatch.setattr(m, '_create_match_archive', lambda: True)
    delivery = {}

    def bowl(runs=0, wicket=False):
        delivery.clear()
        delivery.update(type='wicket' if wicket else 'run', runs=runs,
                        is_extra=False, batter_out=wicket,
                        wicket_type='Bowled' if wicket else None,
                        description='Scripted delivery')
        return m.next_ball()

    monkeypatch.setattr(match_module, 'calculate_outcome',
                        lambda **kwargs: copy.deepcopy(delivery))
    bowl(4)
    for _ in range(10):
        bowl(wicket=True)
    assert m.innings == 2 and m.target == 5
    assert m.wickets == 0 and len(m.remaining_batter_indices) == 9
    return m, bowl


def _rain_state(m):
    return copy.deepcopy((m.target, m.rain_affected, m.weather_next_event,
                          m.rain_events_log, m.dls_ledger_innings1.to_dict(),
                          m.dls_ledger_innings2.to_dict()
                          if m.dls_ledger_innings2 else None))


@pytest.mark.parametrize('shortened', [False, True])
@pytest.mark.parametrize('tie', [False, True])
def test_completed_chase_precedes_boundary_rain(scripted_match, shortened, tie):
    m, bowl = scripted_match
    base = m._innings1_overs_bowled
    events = ([{'at_global_over': base + 5, 'overs_lost': 10}]
              if shortened else [])
    length = 10 if shortened else 20
    events.append({'at_global_over': base + length, 'overs_lost': 20})
    m.weather_script = _script(*events)
    for _ in range(length * 6 - 1):
        assert not bowl().get("match_over")
    before = _rain_state(m)
    balls_before = sum(s['balls_bowled'] for s in m.bowler_stats.values())
    bowl(m.target - 1 if tie else 0)
    result = m.next_ball()
    assert _rain_state(m) == before
    assert sum(s['balls_bowled'] for s in m.bowler_stats.values()) == balls_before + 1
    if tie:
        assert m.innings == 4  # Preserve the normal super-over path.
    else:
        assert result['match_over']
        assert m.match_status == 'completed' and m.margin_value == m.target - 1
        assert m.winner_is_home is True
    saved_result = m.result
    m.next_ball()
    assert m.result == saved_result and _rain_state(m) == before


@pytest.mark.parametrize('winning', [False, True])
def test_decisive_delivery_before_minimum_precedes_rain(scripted_match, winning):
    m, bowl = scripted_match
    m.weather_script = _script({'at_global_over': m._innings1_overs_bowled + 4,
                               'overs_lost': 20})
    # End the chase on its 24th delivery, before the five-over minimum.
    for _ in range(14 if not winning else 23):
        bowl()
    if not winning:
        for _ in range(9):
            bowl(wicket=True)
        assert m.wickets == 9 and not m.remaining_batter_indices
    before = _rain_state(m)
    result = bowl(6) if winning else bowl(wicket=True)
    assert result['match_over'] and m.match_status == 'completed'
    assert m.winner_is_home is (not winning)
    assert _rain_state(m) == before
    m.next_ball()
    assert _rain_state(m) == before


def test_first_innings_rain_termination_defers_next_due_event(monkeypatch):
    m = _match(_script({'at_global_over': 5, 'overs_lost': 15},
                       {'at_global_over': 5, 'overs_lost': 1}))
    monkeypatch.setattr(m, '_create_match_archive', lambda: True)
    monkeypatch.setattr(match_module, 'calculate_outcome', lambda **kw: dict(
        type='run', runs=0, is_extra=False, batter_out=False,
        wicket_type=None, description='Dot'))
    for _ in range(30):
        m.next_ball()
    assert m.weather_next_event == 1
    assert m.innings == 1 and m.overs == 5
    assert m.next_ball()['innings_end']
    assert m.innings == 2 and m.weather_next_event == 1
    result = m.next_ball()
    assert result['match_over'] and m.match_status == 'no_result'
    assert m.weather_next_event == 2
    assert [e['innings'] for e in m.rain_events_log] == [1, 2]
