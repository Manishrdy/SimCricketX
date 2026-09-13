"""Live captain decisions have deadlines and never strand the next delivery."""
import random
import time

import pytest

from engine import fc_captain, fc_declaration, fc_decision_budget
from engine.fc_forecast import innings_forecast
from scripts.bench_fc import _squad
from tests.test_fc_format import _fc_match


def position(score=690, innings=1, lead=0):
    strengths = fc_captain.strengths_by_wickets(_squad("H"), _squad("A"))
    return dict(fc_innings=innings, wickets=9, overs_bowled_this_innings=150,
                score=score, lead=lead, days_remaining=2, pitch="Dead",
                pitch_par_factor=1.3, innings_time_budget_overs=120,
                overs_remaining_in_match=150, own_strengths=strengths,
                opposition_strengths=strengths, pitch_wear=0.5)


@pytest.mark.parametrize("innings,score,lead,declared", [
    (1, 690, 0, True), (1, 910, 0, True), (1, 100, 0, False),
    (2, 450, -50, False), (3, 250, 40, False), (3, 500, 500, True),
    (4, 600, 400, False),
])
def test_expired_budget_uses_safe_quick_policy_without_rng(innings, score, lead, declared):
    before = random.getstate()
    result = fc_declaration.declaration_decision(
        deadline=time.monotonic() - 1, **position(score, innings, lead))
    assert result["declare_now"] == declared
    assert result["review_after_overs"] == (0 if declared else 10)
    assert random.getstate() == before


def test_cold_live_forecast_returns_within_one_second():
    fc_captain._forecast.cache_clear()
    fc_captain._first_innings_curves.cache_clear()
    started = time.monotonic()
    result = fc_declaration.declaration_decision(**position())
    assert time.monotonic() - started < 1.0
    assert isinstance(result["declare_now"], bool)


def test_forecast_checks_the_deadline_inside_its_over_loop(monkeypatch):
    clock = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(fc_decision_budget, "monotonic", lambda: next(clock))
    with pytest.raises(fc_decision_budget.DecisionBudgetExceeded):
        with fc_decision_budget.decision_budget(1.0):
            innings_forecast(pitch="Dead", overs_available=400, wickets_in_hand=10,
                             strength_by_wickets={w: 0 for w in range(1, 11)}, aggression=0)
    # A cancelled request must not poison the next caller's context.
    fc_decision_budget.check_budget()


def test_expired_worker_is_discarded_and_cannot_apply_a_late_result():
    class Stalled:
        done = False
        def ready(self):
            return self.done
        def get(self):
            pytest.fail("expired worker result should never be consumed")
    match = _fc_match(days=5, pitch="Dead")
    match.score = 910
    match.wickets = 9
    match.current_over = 180
    match.fc_day = 4
    match._fc_prepare_declaration()
    job = match._fc_declaration_job = Stalled()
    match._fc_declaration_deadline = time.monotonic() - 1
    assert match._fc_settle_declaration_decision(wait=False)
    assert match.fc_innings_declared
    assert match._fc_declaration_job is None
    job.done = True
    assert match._fc_settle_declaration_decision(wait=False)
    assert match.fc_innings_declared


def test_discussion_reports_match_facts_and_the_actual_verdict():
    match = _fc_match(days=5, pitch="Dead")
    match.score, match.wickets, match.current_over = 410, 6, 110
    match._fc_prepare_declaration()
    coach = match.take_fc_captain_discussion()
    assert len(coach) == 1 and coach[0]["speaker"] == "Coach"
    assert "410/6" in coach[0]["text"]
    assert match.take_fc_captain_discussion() == []
    match._fc_apply_declaration_result({"declare_now": False, "review_after_overs": 10})
    captain = match.take_fc_captain_discussion()
    assert captain[0]["speaker"] == "Captain"
    assert "Bat on" in captain[0]["text"] and "10 overs" in captain[0]["text"]
    assert captain[0]["id"] != coach[0]["id"]
    assert match.take_fc_captain_discussion() == []


def test_bat_on_does_not_restart_decision_at_same_boundary(monkeypatch):
    match = _fc_match()
    match.wickets = 9
    match._fc_apply_declaration_result({"declare_now": False, "review_after_overs": 10})
    calls = []
    monkeypatch.setattr(match, "_fc_start_declaration_decision", lambda: calls.append(True))
    monkeypatch.setattr(match, "_fc_settle_declaration_decision", lambda **kwargs: True)
    assert match._fc_check_declaration_and_follow_on(wait_for_decision=False) is None
    assert not calls
    match.current_over += 1
    match._fc_check_declaration_and_follow_on(wait_for_decision=False)
    assert calls == [True]
