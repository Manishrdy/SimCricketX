"""
First ball after the toss must not strand the match.

spin-toss / set-toss deliberately evict the cached Match instance so every
toss-derived field (batting/bowling side, BowlerManager, openers, stat dicts)
is rebuilt by Match.__init__ from the JSON. That means the FIRST delivery of
every match reaches the server with nothing in MATCH_INSTANCES.

The client asks for that first ball through GET /live-state?delivery=1, because
startMatch() has no delivery token yet. /live-state used to rebuild an absent
instance only when the match JSON carried a super-over or FC snapshot, so a
normal match got {"status": "not_in_memory"} — a status recoverDelivery()
cannot act on, which surfaced as "Live match state is unavailable. Reload to
resume." right after the toss commentary.
"""
import copy
import json
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_fc_format import _squad

HOME = _squad("HOM")
AWAY = _squad("AWY")


def _t20_match_data(user_id):
    return {
        "match_id": str(uuid.uuid4()), "created_by": user_id,
        "timestamp": "2026-09-19T12:00:00",
        "team_home": "HOM_1", "team_away": "AWY_1",
        "stadium": "Test Ground", "pitch": "Flat",
        "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
        "match_format": "T20", "overs": 20, "simulation_mode": "auto",
        "rain_probability": 0.0,
        "playing_xi": {"home": copy.deepcopy(HOME), "away": copy.deepcopy(AWAY)},
        "substitutes": {"home": [], "away": []},
        "weather_forecast": "clear",
    }


def _write_match(app_module, data):
    match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    path = os.path.join(match_dir, f"match_{data['match_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def test_delivery_state_rebuilds_a_match_that_is_not_in_memory(
    app, authenticated_client, regular_user
):
    """The post-toss first ball: on disk, nothing in memory, no snapshot."""
    import app as app_module

    data = _t20_match_data(regular_user.id)
    match_id = data["match_id"]
    path = _write_match(app_module, data)

    try:
        assert match_id not in app_module.MATCH_INSTANCES
        assert "super_over_snapshot" not in data and "fc_snapshot" not in data

        resp = authenticated_client.get(f"/match/{match_id}/live-state?delivery=1")
        assert resp.status_code == 200
        body = resp.get_json()
        # recoverDelivery() only understands delivery_ready / delivery_recovery
        # / completed. Anything else stops the match with a reload prompt.
        assert body["status"] == "delivery_ready", body
        assert body.get("delivery_token")
        assert app_module.MATCH_INSTANCES.get(match_id) is not None
    finally:
        app_module.MATCH_INSTANCES.pop(match_id, None)
        try:
            os.remove(path)
        except OSError:
            pass


def test_plain_live_state_still_reports_a_match_it_has_not_started(
    app, authenticated_client, regular_user
):
    """Page load must keep its old meaning: no live instance => offer the toss.

    match_detail.js treats any status other than 'in_progress' as "let the user
    start fresh", so a non-delivery poll must NOT resurrect an instance.
    """
    import app as app_module

    data = _t20_match_data(regular_user.id)
    match_id = data["match_id"]
    path = _write_match(app_module, data)

    try:
        resp = authenticated_client.get(f"/match/{match_id}/live-state")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "not_in_memory"
        assert app_module.MATCH_INSTANCES.get(match_id) is None
    finally:
        app_module.MATCH_INSTANCES.pop(match_id, None)
        try:
            os.remove(path)
        except OSError:
            pass


def test_toss_then_first_ball_is_the_exact_sequence_that_used_to_strand(
    app, authenticated_client, regular_user
):
    """Full client sequence: spin the toss, then ask for the first delivery.

    spin-toss evicts the instance on purpose; startMatch() then asks for a
    delivery token because it has none. This is the pair of requests behind
    "Live match state is unavailable. Reload to resume." right after the toss
    commentary.
    """
    import app as app_module

    data = _t20_match_data(regular_user.id)
    data.pop("toss_winner", None)
    data.pop("toss_decision", None)
    match_id = data["match_id"]
    path = _write_match(app_module, data)

    try:
        toss = authenticated_client.post(f"/match/{match_id}/spin-toss")
        assert toss.status_code == 200, toss.get_data(as_text=True)
        assert toss.get_json()["toss_winner"]

        # The toss dropped any cached instance so it rebuilds from the JSON.
        assert app_module.MATCH_INSTANCES.get(match_id) is None

        state = authenticated_client.get(f"/match/{match_id}/live-state?delivery=1")
        assert state.status_code == 200
        assert state.get_json()["status"] == "delivery_ready", state.get_json()

        # And the token it hands back actually bowls a ball.
        token = state.get_json()["delivery_token"]
        ball = authenticated_client.post(
            f"/match/{match_id}/next-ball", json={"delivery_token": token}
        )
        assert ball.status_code == 200, ball.get_data(as_text=True)
        assert "error" not in ball.get_json()
    finally:
        app_module.MATCH_INSTANCES.pop(match_id, None)
        try:
            os.remove(path)
        except OSError:
            pass
