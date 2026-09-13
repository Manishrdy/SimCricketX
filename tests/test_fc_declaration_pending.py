"""The close-scorecard request must never wait for the captain's CPU work."""
import copy
import time

import pytest

from engine import fc_captain, fc_declaration
from tests.test_fc_format import _fc_match


class Pending:
    def __init__(self):
        self.done = False
        self.gets = 0

    def ready(self):
        return self.done

    def get(self):
        self.gets += 1
        assert self.done, "next-ball blocked on an unfinished decision"
        return {"declare_now": False, "review_after_overs": 10}


def test_next_ball_returns_pending_without_bowling_or_consuming_the_job():
    match = _fc_match(pitch="Hard")
    match.current_over = 30
    match.fc_sessions_taken_today = 1
    job = match._fc_declaration_job = Pending()
    before = (match.score, match.wickets, match.current_over, match.current_ball,
              match.fc_clock_minute, copy.deepcopy(match.batsman_stats))
    for _ in range(3):
        started = time.perf_counter()
        response = match.next_ball(wait_for_decision=False)
        assert time.perf_counter() - started < 0.1
        assert response["fc_decision_pending"]
        assert response["retry_after"] == 0.5
    after = (match.score, match.wickets, match.current_over, match.current_ball,
             match.fc_clock_minute, match.batsman_stats)
    assert after == before
    assert match._fc_declaration_job is job
    assert job.gets == 0
    job.done = True
    response = match.next_ball(wait_for_decision=False)
    assert not response.get("fc_decision_pending")
    assert match._fc_declaration_job is None
    assert job.gets == 1
    assert match._fc_declaration_review_over == 40


@pytest.mark.parametrize("boundary", ["stumps", "nine_down", "review"])
def test_non_interval_checks_also_return_pending(monkeypatch, boundary):
    import engine.match as match_module
    match = _fc_match(pitch="Hard")
    match.score = 450
    match.current_over = 40
    match.fc_sessions_taken_today = 1
    if boundary == "stumps":
        match.fc_force_day_end = True
    elif boundary == "nine_down":
        match.wickets = 9
    else:
        match._fc_declaration_review_over = 40
    job = Pending()
    monkeypatch.setattr(match_module, "start_offloaded", lambda *args, **kwargs: job)
    response = match._fc_pre_ball_checks(wait_for_decision=False)
    assert response["fc_decision_pending"]
    assert match._fc_declaration_job is job
    assert job.gets == 0


@pytest.mark.parametrize("score,overs", [(95, 30), (189, 58)])
def test_ordinary_day_one_scores_do_not_start_the_deep_forecast(monkeypatch, score, overs):
    match = _fc_match(days=5, pitch="Hard")
    match.score = score
    match.wickets = 5
    match.current_over = overs
    def forbidden(**kwargs):
        pytest.fail("ordinary early batting position entered expensive forecast")
    monkeypatch.setattr(fc_captain, "evaluate_declaration", forbidden)
    result = fc_declaration.declaration_decision(**match._fc_declaration_inputs())
    assert result == {"declare_now": False, "review_after_overs": None}


@pytest.mark.parametrize("late", ["days", "budget", "score"])
def test_fast_bat_on_rule_does_not_hide_a_live_declaration(monkeypatch, late):
    match = _fc_match(days=5, pitch="Hard")
    match.score = 189
    match.current_over = 60
    if late == "days":
        match.fc_day = 4
    elif late == "budget":
        match.current_over = 180
    else:
        match.score = 690
    seen = []
    def model(**kwargs):
        seen.append(kwargs)
        return {"declare_now": True, "best": {"horizon": 0, "value": 0.5},
                "now": {"win": 0.5, "draw": 0.5, "loss": 0}}
    monkeypatch.setattr(fc_captain, "evaluate_declaration", model)
    assert fc_declaration.declaration_decision(**match._fc_declaration_inputs())["declare_now"]
    assert len(seen) == 1


def test_http_pending_response_and_fc_template(app, authenticated_client, regular_user):
    import json
    from pathlib import Path
    import app as app_module
    match = _fc_match(pitch="Hard")
    match.data["created_by"] = regular_user.id
    match.current_over = 30
    match.fc_sessions_taken_today = 1
    match._fc_declaration_review_over = 30
    match._fc_declaration_job = Pending()
    match_id = match.data["match_id"]
    path = Path(app_module.PROJECT_ROOT) / "data" / "matches" / f"match_{match_id}.json"
    path.write_text(json.dumps(match.data))
    with app_module.MATCH_INSTANCES_LOCK:
        app_module.MATCH_INSTANCES[match_id] = match
    try:
        page = authenticated_client.get(f"/match/{match_id}")
        assert page.status_code == 200
        assert b'id="fc-captain-status"' in page.data
        for _ in range(2):
            app_module._rate_limit_store.clear()
            response = authenticated_client.post(f"/match/{match_id}/next-ball")
            assert response.status_code == 200
            assert response.json["fc_decision_pending"]
        assert match._fc_declaration_job.gets == 0
        assert match.current_over == 30
        snapshot = json.loads(path.read_text())["fc_snapshot"]
        assert snapshot["fc_declaration_review_over"] == 30
    finally:
        with app_module.MATCH_INSTANCES_LOCK:
            app_module.MATCH_INSTANCES.pop(match_id, None)
        path.unlink(missing_ok=True)


def test_live_worker_does_not_hold_the_close_request(monkeypatch):
    from utils.cpu_offload import _get_threadpool
    if _get_threadpool() is None:
        pytest.skip("requires the live gevent transport")
    from gevent.monkey import get_original
    sleep = get_original("time", "sleep")
    match = _fc_match()
    match.current_over = 40
    match.fc_sessions_taken_today = 1
    def slow(**kwargs):
        sleep(0.3)
        return {"declare_now": False, "review_after_overs": 10}
    monkeypatch.setattr(fc_declaration, "declaration_decision", slow)
    match._fc_start_declaration_decision()
    job = match._fc_declaration_job
    try:
        started = time.perf_counter()
        assert match.next_ball(wait_for_decision=False)["fc_decision_pending"]
        assert time.perf_counter() - started < 0.15
        assert match.current_over == 40
    finally:
        job.get()  # Drain the real worker so it cannot leak into another test.
    assert not match.next_ball(wait_for_decision=False).get("fc_decision_pending")
    assert match._fc_declaration_job is None
