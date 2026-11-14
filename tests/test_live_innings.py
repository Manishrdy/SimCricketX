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
    # Panel card: the dismissed batter moves out with a score, the slot being
    # filled is not listed as at the crease, and the pick list is complete.
    by_state = {}
    for entry in pending["batters"]:
        by_state.setdefault(entry["state"], []).append(entry)
    out = by_state["out"][0]
    assert out["name"] == "HOME_P2" and "how" not in out
    assert (out["runs"], out["balls"]) == (match.batsman_stats["HOME_P2"]["runs"],
                                           match.batsman_stats["HOME_P2"]["balls"])
    assert [b["name"] for b in by_state["non_striker"]] == ["HOME_P1"]
    assert "striker" not in by_state
    assert "HOME_P3" in [b["name"] for b in by_state["yet"]]
    assert pending["bowlers"][0]["current"] and pending["bowlers"][0]["figures"].count("-") == 3
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
    final = client.post(url + "/next-ball").get_json()["live_innings"]
    # The match-ending ball carries the last card so the panel can keep it.
    assert final["status"] == "complete"
    assert final["batters"] and final["bowlers"]
    assert {b["state"] for b in final["batters"]} <= {"striker", "non_striker", "out", "yet"}


def test_win_probability_is_post_ball_and_survives_refresh(live_match, monkeypatch):
    client, register = live_match
    match = _match()
    url = register(match)
    first_innings = client.get(url + "/live-state").get_json()["win_probability"]
    assert first_innings is not None
    assert first_innings == match._calculate_win_probability()

    match.current_over = match.overs
    match.score = 100
    client.post(url + "/next-ball")
    scripted(monkeypatch, [delivery(4)])
    chase = client.post(url + "/next-ball").get_json()
    assert chase["innings_number"] == 2 and chase["score"] == 4

    # Probability of the position after the boundary, not before it.
    assert chase["win_probability"] == match._calculate_win_probability()
    assert chase["win_probability"] is not None
    state = client.get(url + "/live-state").get_json()
    assert state["win_probability"] == chase["win_probability"]


def test_live_state_returns_every_delivery_for_a_refreshed_match_center(live_match, monkeypatch):
    client, register = live_match
    match = _match()
    url = register(match)
    scripted(monkeypatch, [delivery(1), delivery(wicket="Bowled"), delivery(6)])
    sent = [client.post(url + "/next-ball").get_json()["ball_data"] for _ in range(3)]

    match.current_over = match.overs
    client.post(url + "/next-ball")
    break_state = client.get(url + "/live-state").get_json()
    assert break_state["deliveries"] == sent
    assert [d["delivery_id"] for d in sent] == ["1:1", "1:2", "1:3"]

    scripted(monkeypatch, [delivery(4)])
    sent.append(client.post(url + "/next-ball").get_json()["ball_data"])
    state = client.get(url + "/live-state").get_json()
    assert state["deliveries"] == sent
    assert [d["innings"] for d in state["deliveries"]] == [1, 1, 1, 2]
    assert sent[-1]["delivery_id"] == "2:4"


def test_ball_data_reports_batters_at_the_crease_after_the_ball(live_match, monkeypatch):
    client, register = live_match
    match = _match()
    url = register(match)
    scripted(monkeypatch, [delivery(1), delivery(wicket="Bowled")] + [delivery()] * 4)
    sent = [client.post(url + "/next-ball").get_json()["ball_data"] for _ in range(6)]

    def pairs(bd):
        return (bd["striker"], bd["non_striker"]), (bd["next_striker"], bd["next_non_striker"])

    # Odd runs rotate strike.
    assert pairs(sent[0]) == (("HOME_P1", "HOME_P2"), ("HOME_P2", "HOME_P1"))
    # The dismissed batter is replaced in the same delivery.
    assert pairs(sent[1]) == (("HOME_P2", "HOME_P1"), ("HOME_P3", "HOME_P1"))
    assert (sent[1]["partnership_runs"], sent[1]["partnership_balls"]) == (0, 0)
    # The over-ending ball swaps ends.
    assert pairs(sent[5]) == (("HOME_P3", "HOME_P1"), ("HOME_P1", "HOME_P3"))


def test_manual_batter_choice_updates_last_delivery_at_the_crease(live_match, monkeypatch):
    client, register = live_match
    match = _match()
    match.data["simulation_mode"] = match.simulation_mode = "manual"
    url = register(match)
    scripted(monkeypatch, [delivery(wicket="Bowled")])
    picked = client.post(url + "/next-ball").get_json()["decision_options"][0]
    client.post(url + "/submit-decision", json={"type": "next_bowler", "selected_index": picked["index"]})

    wicket = client.post(url + "/next-ball").get_json()
    assert wicket["ball_data"]["next_striker"] == "HOME_P3"
    applied = client.post(url + "/submit-decision", json={
        "type": "next_batter", "selected_index": 3,
    }).get_json()["applied"]
    assert (applied["next_striker"], applied["next_non_striker"]) == ("HOME_P4", "HOME_P2")
    last = client.get(url + "/live-state").get_json()["deliveries"][-1]
    assert (last["next_striker"], last["next_non_striker"]) == ("HOME_P4", "HOME_P2")


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
