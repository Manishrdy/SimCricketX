import json
from pathlib import Path

import pytest

import app as app_module
from scripts.dev_test_accounts import ACCOUNTS, logged_in_client, seed
from tests.test_fc_format import _fc_match
from tests.test_hundred_presentation import delivery, scripted
from tests.test_innings_change_reset import _match


@pytest.fixture
def live_match(app):
    seed(app)
    client = logged_in_client(app, "user1")
    registered = []

    def register(match):
        root = Path(app_module.PROJECT_ROOT)
        match_id = match.data["match_id"]
        match.data["created_by"] = ACCOUNTS["user1"]["email"]
        path = root / "data" / "matches" / f"match_{match_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(match.data), encoding="utf-8")
        app_module.MATCH_INSTANCES[match_id] = match
        registered.append((match_id, path))
        return f"/match/{match_id}"

    yield client, register
    for match_id, path in registered:
        app_module.MATCH_INSTANCES.pop(match_id, None)
        path.unlink(missing_ok=True)


def test_live_roles_follow_strike_wicket_and_manual_selection(live_match, monkeypatch):
    client, register = live_match
    match = _match()
    match.data["simulation_mode"] = match.simulation_mode = "manual"
    url = register(match)
    scripted(monkeypatch, [delivery(1), delivery(wicket="Bowled")])

    def advance():
        response = client.post(url + "/next-ball")
        assert response.status_code == 200
        return response.get_json()

    selection = advance()
    assert selection["live_innings"]["selecting_bowler"]
    assert selection["live_innings"]["bowler"] is None
    picked = selection["decision_options"][0]
    selected = client.post(url + "/submit-decision", json={
        "type": "next_bowler", "selected_index": picked["index"],
    }).get_json()
    assert selected["live_innings"]["bowler"] == picked["name"]

    rotated = advance()["live_innings"]
    assert (rotated["striker"], rotated["non_striker"]) == ("HOME_P2", "HOME_P1")
    assert rotated["next_batter"] == "HOME_P3"

    wicket = advance()
    pending = wicket["live_innings"]
    assert pending["striker"] is None
    assert pending["selecting_batter"] == "striker"
    assert pending["non_striker"] == "HOME_P1"
    state = client.get(url + "/live-state").get_json()
    assert state["live_innings"] == pending
    assert state["pending_selection"]["decision_type"] == "next_batter"

    selected = client.post(url + "/submit-decision", json={
        "type": "next_batter", "selected_index": 3,
    }).get_json()["live_innings"]
    assert selected["striker"] == "HOME_P4"
    assert selected["next_batter"] == "HOME_P3"
    match.remaining_batter_indices.clear()
    assert client.get(url + "/live-state").get_json()["live_innings"]["next_batter"] is None


def test_innings_break_reload_and_finalized_impact_lineup(live_match, monkeypatch):
    client, register = live_match
    match = _match()
    replacement = dict(match.away_xi[0], name="Impact batter")
    match.data["substitutes"]["away"] = [replacement]
    url = register(match)
    match.current_over = match.overs
    match.score = 100

    result = client.post(url + "/next-ball").get_json()
    assert result["live_innings"] == {"status": "break"}
    state = client.get(url + "/live-state").get_json()
    assert state["pending_card"]["card_id"] == result["pending_card_id"]
    assert state["live_innings"] == {"status": "break"}

    swapped = client.post(url + "/impact-player-swap", json={
        "away_swap": {"out_player_index": 0, "in_player_index": 0},
    })
    assert swapped.status_code == 200
    lineup = swapped.get_json()["updated_match_data"]["playing_xi"]["away"]
    lineup[0], lineup[2] = lineup[2], lineup[0]
    assert client.post(url + "/update-final-lineups", json={"away_final_xi": lineup}).status_code == 200
    assert client.get(url + "/live-state").get_json()["live_innings"] == {"status": "break"}

    scripted(monkeypatch, [delivery()])
    next_innings = client.post(url + "/next-ball").get_json()["live_innings"]
    assert next_innings["innings"] == 2
    assert next_innings["batting_team"] == "AWY"
    assert next_innings["striker"] == "AWAY_P3"
    assert next_innings["next_batter"] == "Impact batter"
    state = client.get(url + "/live-state").get_json()
    assert state["pending_card"] is None
    assert state["live_innings"] == next_innings

    match.target = 1
    scripted(monkeypatch, [delivery(1)])
    assert client.post(url + "/next-ball").get_json()["live_innings"] == {"status": "complete"}


def test_follow_on_break_survives_restore_with_same_batting_team(live_match, monkeypatch):
    client, register = live_match
    match = _fc_match(days=5)
    match.data["simulation_mode"] = match.simulation_mode = "manual"
    url = register(match)
    match._fc_first_batting_xi = match.home_xi
    match._fc_first_bowling_xi = match.away_xi
    match.fc_innings_totals[1] = {"score": 400, "wickets": 10, "overs_str": "90.0", "side": "home"}
    match._fc_start_next_innings(2, match.away_xi, match.home_xi)
    match.score = 150
    match.wickets = 10

    result = client.post(url + "/next-ball").get_json()
    assert result["decision_type"] == "fc_follow_on"
    assert result["live_innings"] == {"status": "break"}
    applied = client.post(url + "/submit-decision", json={
        "type": "fc_follow_on", "selected_index": 1,
    }).get_json()
    assert applied["live_innings"] == applied["transition"]["live_innings"] == {"status": "break"}

    app_module.MATCH_INSTANCES.pop(match.data["match_id"])
    page = client.get(url).get_data(as_text=True)
    assert "window.resumeMode = true" in page
    state = client.get(url + "/live-state").get_json()
    assert state["innings"] == 3
    assert state["batting_team_name"] == "AWY"
    assert state["pending_card"]["card_id"] == applied["transition"]["pending_card_id"]
    assert state["live_innings"] == {"status": "break"}

    # The next update may be a First-Class weather notice or a bowler choice.
    scripted(monkeypatch, [delivery()])
    resumed = client.post(url + "/next-ball").get_json()["live_innings"]
    assert resumed["innings"] == 3
    assert resumed["batting_team"] == "AWY"
    assert resumed["next_batter"] == "AWY_P3"
