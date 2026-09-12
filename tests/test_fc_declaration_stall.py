"""Regressions for the FC declaration stall and the card it used to lose.

Production telemetry (310 balls, one match) recorded a median next_ball() of
14.6 ms, a p99 of 78.9 ms, and two calls of 52.6 s and 45.6 s — 93% of all
engine time in two deliveries. Both landed on the gate at
match.py's ``_fc_pre_ball_checks``: ``_day_over or _at_session_break or
wickets >= 9``, which is where the declaration model runs. The 52.6 s one was
Lunch, and the payload it produced carried the session scorecard.

Two things went wrong, and each has its own guard here:

1. The model ran inline on a single-worker gevent server, so for its whole
   duration nothing else ran — including Socket.IO's heartbeat. The client
   gave up at the default 45 s budget and tore the socket down *before* the
   server emitted the result.
2. The card was emitted exactly once, onto that dead socket, while the engine
   had already consumed the session that produced it. It could never be
   produced again.
"""
import json
import uuid

import pytest

from engine import fc_forecast
from engine.fc_forecast import frontier, innings_forecast
from utils.cpu_offload import run_offloaded


# --------------------------------------------------------------------------
# The hoisted frontier must stay identical to the function it was hoisted from
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pitch", ["Green", "Dry", "Hard", "Flat", "Dead"])
@pytest.mark.parametrize("aggression", [-1.0, 0.0, 0.5, 1.0])
def test_hoisted_frontier_matches_frontier(pitch, aggression):
    """innings_forecast factors exp(a+b) into exp(a)*exp(b) so the strength
    term can be hoisted out of the per-over loop. That is only legitimate
    while it reproduces frontier() exactly, so check the pieces directly."""
    (r_intercept, r_wear, r_ages, r_sd), (k_intercept, k_wear, k_ages, k_sd) = \
        fc_forecast._frontier_basis(pitch, aggression)
    import math
    for wear in (0.0, 0.17, 0.5, 0.83, 1.0):
        for age_overs in (0, 8, 26, 55, 79):
            bucket = fc_forecast.ball_age_bucket(age_overs)
            for strength in (-40.0, -12.5, 0.0, 9.0, 33.0):
                expected_rate, expected_wicket = frontier(
                    pitch, wear, age_overs, strength, aggression)
                hoisted_rate = max(fc_forecast.MIN_RATE, min(
                    fc_forecast.MAX_RATE,
                    math.exp(r_intercept + r_wear * wear + r_ages[bucket])
                    * math.exp(r_sd * strength / 100.0)))
                hoisted_wicket = max(fc_forecast.MIN_WICKET_RATE, min(
                    fc_forecast.MAX_WICKET_RATE,
                    math.exp(k_intercept + k_wear * wear + k_ages[bucket])
                    * math.exp(k_sd * strength / 100.0)))
                # The DP quantises to int(rate*100) / int(wicket_rate*1000)
                # before use, so agreement well inside one quantum is what
                # actually has to hold.
                assert abs(hoisted_rate - expected_rate) < 1e-9
                assert abs(hoisted_wicket - expected_wicket) < 1e-12


def test_wicket_pmf_array_is_not_writeable():
    """_as_pmf_array hands the same ndarray to every caller out of an LRU
    cache. A caller that wrote through it would corrupt every later forecast,
    so the array is frozen."""
    pmf = fc_forecast._as_pmf_array(fc_forecast._wicket_pmf(120, 6))
    assert not pmf.flags.writeable


def test_forecast_conserves_probability():
    """The DP's mass has to stay accounted for: survived + all out + chased."""
    result = innings_forecast(
        pitch="Hard", overs_available=90, wickets_in_hand=10,
        strength_by_wickets={w: -10 + w for w in range(1, 11)},
        aggression=0.0, wear_start=0.3, wear_end=0.7, target=300)
    assert result["total_mass"] == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------
# The offload
# --------------------------------------------------------------------------

def test_run_offloaded_runs_inline_without_gevent():
    """Tests and the bench scripts have no hub; the call must still work and
    must propagate exceptions unchanged."""
    assert run_offloaded(lambda a, b=0: a + b, 2, b=3) == 5
    with pytest.raises(ValueError, match="boom"):
        run_offloaded(lambda: (_ for _ in ()).throw(ValueError("boom")))


def test_declaration_model_is_offloaded():
    """should_declare must not call the model inline: run_offloaded is what
    keeps the gevent hub — and therefore the Socket.IO heartbeat — alive while
    it runs. Asserted against the source so the indirection cannot be quietly
    removed during a refactor."""
    import inspect
    from engine import fc_declaration
    source = inspect.getsource(fc_declaration.should_declare)
    assert "run_offloaded(" in source, (
        "should_declare calls evaluate_declaration inline again — a 45s+ call "
        "on the gevent hub kills the Socket.IO heartbeat and loses the "
        "interval scorecard"
    )
    assert "fc_captain.evaluate_declaration(" not in source


# --------------------------------------------------------------------------
# The card the stall used to lose
# --------------------------------------------------------------------------

def _fc_match():
    from scripts.bench_fc import _fc_match as build
    return build("Balanced", 11, days=5, forecast="clear")


def test_new_match_owes_no_card():
    assert _fc_match().pending_interval_card is None


def test_pending_card_survives_snapshot_round_trip():
    """An unacknowledged card has to outlive instance eviction and worker
    restart, not merely a dropped socket — the snapshot is the only thing
    that carries state across those."""
    match = _fc_match()
    outcome = None
    for _ in range(4000):
        result = match.next_ball()
        if result.get("scorecard_data") and not result.get("match_over"):
            outcome = result
            break
        if result.get("match_over"):
            break
    assert outcome is not None, "no card-bearing payload in a whole FC match"

    card_id = uuid.uuid4().hex
    outcome["pending_card_id"] = card_id
    match.pending_interval_card = {"card_id": card_id, "payload": outcome}

    snapshot = json.loads(json.dumps(match.serialize_fc_snapshot()))
    restored = _fc_match()
    restored.restore_fc_snapshot(snapshot)

    card = restored.pending_interval_card
    assert card is not None, "card lost across the snapshot"
    assert card["card_id"] == card_id
    assert "scorecard_data" in card["payload"]
    # The replayed copy must be acknowledgeable on its own.
    assert card["payload"]["pending_card_id"] == card_id


# --------------------------------------------------------------------------
# End to end over HTTP: the exact production sequence that lost the card
# --------------------------------------------------------------------------

def test_interval_card_survives_a_dropped_socket(app, authenticated_client,
                                                 regular_user):
    """Replays the production failure: the engine produces a card, the client
    never receives it (socket torn down by the heartbeat during the long
    declaration call), and the client reconnects.

    Before the fix the card was gone — the engine had already incremented
    fc_sessions_taken_today, so it would never be produced again. Now
    /live-state still owes it, and /ack-card clears it once drawn.
    """
    import copy
    import os

    import app as app_module
    import engine.match as match_module
    from tests.test_fc_resume import _fc_match_data

    match = match_module.Match(_fc_match_data(regular_user.id))
    match_id = match.match_data["match_id"]

    match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    path = os.path.join(match_dir, f"match_{match_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(copy.deepcopy(match.match_data), handle)

    with app_module.MATCH_INSTANCES_LOCK:
        app_module.MATCH_INSTANCES[match_id] = match

    try:
        # Drive the real route until it hands back a card. The response is
        # what the socket would have carried — and what the client misses.
        card_response = None
        for _ in range(4000):
            # /next-ball is rate limited to protect against a hammering
            # client; a test that drives thousands of balls as fast as it can
            # is exactly that, so the window is reset each time rather than
            # weakening the limit itself.
            app_module._rate_limit_store.clear()
            resp = authenticated_client.post(f"/match/{match_id}/next-ball")
            assert resp.status_code == 200
            body = resp.get_json()
            if body.get("scorecard_data") and not body.get("match_over"):
                card_response = body
                break
            if body.get("match_over"):
                break
        assert card_response is not None, "no card-bearing payload"
        assert card_response.get("pending_card_id"), (
            "the emitted card carries no id, so the client cannot ack it")
        card_id = card_response["pending_card_id"]

        # The client never saw that response. It reconnects and asks what it
        # is owed.
        state = authenticated_client.get(f"/match/{match_id}/live-state").get_json()
        assert state["status"] == "in_progress"
        pending = state.get("pending_card")
        assert pending is not None, "the card was lost with the socket"
        assert pending["card_id"] == card_id
        assert pending["payload"]["scorecard_data"] == card_response["scorecard_data"]

        # A stale ack from an older card must not clear the current one.
        stale = authenticated_client.post(
            f"/match/{match_id}/ack-card", json={"card_id": "not-the-card"})
        assert stale.get_json()["acked"] is False
        still = authenticated_client.get(
            f"/match/{match_id}/live-state").get_json()["pending_card"]
        assert still is not None and still["card_id"] == card_id

        # Drawn: ack it, and it is no longer owed.
        acked = authenticated_client.post(
            f"/match/{match_id}/ack-card", json={"card_id": card_id})
        assert acked.get_json()["acked"] is True
        after = authenticated_client.get(
            f"/match/{match_id}/live-state").get_json()
        assert after.get("pending_card") is None
    finally:
        app_module.MATCH_INSTANCES.pop(match_id, None)
        try:
            os.remove(path)
        except OSError:
            pass


def test_rate_limited_response_is_recoverable(app, authenticated_client,
                                              regular_user):
    """A 429 must tell the caller it can retry, and when.

    FC runs the ball loop with no delay between deliveries, so on the HTTP
    fallback transport it reaches the per-user limit as a matter of course.
    The client treats a bare `error` as fatal and stops, which strands the
    match — so the limiter has to say what kind of failure this is.
    """
    import copy
    import os

    import app as app_module
    import engine.match as match_module
    from tests.test_fc_resume import _fc_match_data

    match = match_module.Match(_fc_match_data(regular_user.id))
    match_id = match.match_data["match_id"]
    match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    path = os.path.join(match_dir, f"match_{match_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(copy.deepcopy(match.match_data), handle)
    with app_module.MATCH_INSTANCES_LOCK:
        app_module.MATCH_INSTANCES[match_id] = match

    try:
        app_module._rate_limit_store.clear()
        limited = None
        for _ in range(400):
            resp = authenticated_client.post(f"/match/{match_id}/next-ball")
            if resp.status_code == 429:
                limited = resp.get_json()
                break
        assert limited is not None, "never hit the limit"
        assert limited["rate_limited"] is True
        assert isinstance(limited["retry_after"], (int, float))
        assert 0 <= limited["retry_after"] <= 10
    finally:
        app_module._rate_limit_store.clear()
        app_module.MATCH_INSTANCES.pop(match_id, None)
        try:
            os.remove(path)
        except OSError:
            pass
