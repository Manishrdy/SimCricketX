import json
import os
import sys
import uuid

import pytest
from flask_login import UserMixin

# Add project root for imports when running from tests/
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from app import create_app
import engine.match as match_module


def _build_team_players(prefix: str):
    players = []
    for i in range(11):
        players.append(
            {
                "name": f"{prefix}_P{i+1}",
                "role": "Bowler" if i < 5 else "Batsman",
                "batting_rating": 60 + (10 - i),
                "bowling_rating": 70 - i,
                "fielding_rating": 65,
                "batting_hand": "Right",
                "bowling_type": "Fast-medium" if i < 5 else "Medium",
                "bowling_hand": "Right",
                "will_bowl": i < 5,
                "is_captain": i == 0,
            }
        )
    return players


def _build_match_data(user_id: str, simulation_mode: str = "manual"):
    match_id = str(uuid.uuid4())
    return {
        "match_id": match_id,
        "created_by": user_id,
        "team_home": f"HOM_{user_id}",
        "team_away": f"AWY_{user_id}",
        "stadium": "Test Ground",
        "pitch": "Flat",
        "toss": "Heads",
        "toss_winner": "HOM",
        "toss_decision": "Bat",
        "simulation_mode": simulation_mode,
        "playing_xi": {
            "home": _build_team_players("H"),
            "away": _build_team_players("A"),
        },
        "substitutes": {"home": [], "away": []},
    }


@pytest.fixture
def app_client():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["LOGIN_DISABLED"] = False
    client = app.test_client()

    user_id = f"manual_mode_{uuid.uuid4().hex}@example.com"

    class StubUser(UserMixin):
        def __init__(self, uid):
            self.id = uid

    @app.login_manager.user_loader
    def load_user(uid):
        return StubUser(uid)

    with client.session_transaction() as sess:
        sess["_user_id"] = user_id

    yield app, client, user_id

    # Cleanup in-memory matches for this user only.
    with app_module.MATCH_INSTANCES_LOCK:
        remove_ids = [mid for mid, m in app_module.MATCH_INSTANCES.items() if m.data.get("created_by") == user_id]
        for mid in remove_ids:
            del app_module.MATCH_INSTANCES[mid]

    # Cleanup generated match json files for this user.
    match_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "matches")
    if os.path.isdir(match_dir):
        for fn in os.listdir(match_dir):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(match_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if payload.get("created_by") == user_id:
                    os.remove(path)
            except Exception:
                continue


def test_manual_wicket_last_ball_then_next_bowler_decision(app_client, monkeypatch):
    app, client, user_id = app_client
    data = _build_match_data(user_id, simulation_mode="manual")
    match = match_module.Match(data)

    # Force wicket on last legal ball of over.
    match.current_ball = 5
    match.current_over = 0
    match.current_bowler = match.bowling_team[0]
    match.bowler_selected_for_over = 0

    def fake_wicket_outcome(**_kwargs):
        return {
            "runs": 0,
            "batter_out": True,
            "is_extra": False,
            "wicket_type": "Bowled",
            "description": "Castled!"
        }

    monkeypatch.setattr(match_module, "calculate_outcome", fake_wicket_outcome)

    with app_module.MATCH_INSTANCES_LOCK:
        app_module.MATCH_INSTANCES[data["match_id"]] = match

    resp = client.post(f"/match/{data['match_id']}/next-ball")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["decision_required"] is True
    assert payload["decision_type"] == "next_batter"
    assert payload["over"] == 1
    assert payload["ball"] == 0
    assert len(payload["decision_options"]) > 0

    selected_index = payload["decision_options"][-1]["index"]
    submit_resp = client.post(
        f"/match/{data['match_id']}/submit-decision",
        json={"type": "next_batter", "selected_index": selected_index},
    )
    assert submit_resp.status_code == 200
    assert submit_resp.get_json()["success"] is True

    # Next call must ask for bowler selection for the new over.
    next_resp = client.post(f"/match/{data['match_id']}/next-ball")
    assert next_resp.status_code == 200
    next_payload = next_resp.get_json()
    assert next_payload["decision_required"] is True
    assert next_payload["decision_type"] == "next_bowler"


def test_mode_switch_manual_to_auto_auto_resolves_pending_decision(app_client, monkeypatch):
    app, client, user_id = app_client
    data = _build_match_data(user_id, simulation_mode="manual")
    match = match_module.Match(data)
    match._create_next_bowler_decision()

    # Persist match file because set-simulation-mode updates JSON.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    match_dir = os.path.join(project_root, "data", "matches")
    os.makedirs(match_dir, exist_ok=True)
    match_path = os.path.join(match_dir, f"match_{data['match_id']}.json")
    with open(match_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    with app_module.MATCH_INSTANCES_LOCK:
        app_module.MATCH_INSTANCES[data["match_id"]] = match

    mode_resp = client.post(f"/match/{data['match_id']}/set-simulation-mode", json={"mode": "auto"})
    assert mode_resp.status_code == 200
    assert mode_resp.get_json()["mode"] == "auto"
    assert match.simulation_mode == "auto"

    def fake_dot_ball(**_kwargs):
        return {
            "runs": 0,
            "batter_out": False,
            "is_extra": False,
            "description": "Defended.",
        }

    monkeypatch.setattr(match_module, "calculate_outcome", fake_dot_ball)

    resp = client.post(f"/match/{data['match_id']}/next-ball")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("decision_required") is not True
    assert payload.get("error") is None


def test_submit_decision_rejects_invalid_index_and_type(app_client):
    app, client, user_id = app_client
    data = _build_match_data(user_id, simulation_mode="manual")
    match = match_module.Match(data)
    decision = match._create_next_bowler_decision()
    valid_index = decision["options"][0]["index"]

    with app_module.MATCH_INSTANCES_LOCK:
        app_module.MATCH_INSTANCES[data["match_id"]] = match

    bad_type_resp = client.post(
        f"/match/{data['match_id']}/submit-decision",
        json={"type": "next_batter", "selected_index": valid_index},
    )
    assert bad_type_resp.status_code == 400
    assert "Decision type mismatch" in bad_type_resp.get_json()["error"]

    bad_index_resp = client.post(
        f"/match/{data['match_id']}/submit-decision",
        json={"type": "next_bowler", "selected_index": 9999},
    )
    assert bad_index_resp.status_code == 400
    assert "valid option" in bad_index_resp.get_json()["error"]
