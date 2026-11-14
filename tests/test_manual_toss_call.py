"""Manual mode lets the user make the toss call and explains the decision."""
import json
import random

import routes.match_routes as match_routes
from engine.toss import explain_decision
from tests.test_toss_batting_order import _match_data, match_file  # noqa: F401


def _spin(client, match_id, **payload):
    return client.post(f"/match/{match_id}/spin-toss", json=payload)


def test_user_call_overrides_setup_call_and_is_persisted(
        app, authenticated_client, regular_user, match_file, monkeypatch):
    data = _match_data(regular_user.id, toss_winner=None, toss_decision=None, call="Heads")
    match_id, path = match_file(data)
    monkeypatch.setattr(random, "choice", lambda seq: "Tails")  # coin lands Tails
    monkeypatch.setattr(match_routes, "decide_toss", lambda *a, **k: "Bowl")

    resp = _spin(authenticated_client, match_id, call="Tails")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["toss_call"] == "Tails" and body["toss_coin"] == "Tails"
    assert body["toss_winner"] == "TC"          # away called correctly
    assert body["toss_reason"]
    assert json.load(open(path))["toss"] == "Tails"


def test_wrong_call_goes_to_the_home_side(
        app, authenticated_client, regular_user, match_file, monkeypatch):
    data = _match_data(regular_user.id, toss_winner=None, toss_decision=None, call="Heads")
    match_id, _ = match_file(data)
    monkeypatch.setattr(random, "choice", lambda seq: "Heads")
    resp = _spin(authenticated_client, match_id, call="Tails")
    assert resp.get_json()["toss_winner"] == "TW"


def test_invalid_call_is_ignored_and_setup_call_kept(
        app, authenticated_client, regular_user, match_file, monkeypatch):
    data = _match_data(regular_user.id, toss_winner=None, toss_decision=None, call="Heads")
    match_id, _ = match_file(data)
    monkeypatch.setattr(random, "choice", lambda seq: "Heads")
    body = _spin(authenticated_client, match_id, call="Sideways").get_json()
    assert body["toss_call"] == "Heads"


def test_explanation_distinguishes_following_and_defying_conditions():
    from engine.toss import correct_decision
    from engine.format_config import get_any_format
    fmt = get_any_format("T20")
    fav = correct_decision(fmt, "Hard")
    other = "Bowl" if fav == "Bat" else "Bat"
    assert "judgement call" not in explain_decision(fav, fmt, "Hard")
    assert "judgement call" in explain_decision(other, fmt, "Hard")


def test_manual_flip_then_user_picks_bat_or_bowl(
        app, authenticated_client, regular_user, match_file, monkeypatch):
    data = _match_data(regular_user.id, toss_winner=None, toss_decision=None, call="Heads")
    match_id, path = match_file(data)
    monkeypatch.setattr(random, "choice", lambda seq: "Tails")
    # The engine must not be consulted for the user's pick.
    monkeypatch.setattr(match_routes, "decide_toss", lambda *a, **k: (_ for _ in ()).throw(AssertionError))

    flip = _spin(authenticated_client, match_id, call="Tails", phase="flip").get_json()
    assert flip["toss_winner"] == "TC" and flip["toss_recommended"] in ("Bat", "Bowl")
    saved = json.load(open(path))
    assert not saved.get("toss_decision") and saved["toss_pending"]["winner"] == "TC"

    monkeypatch.setattr(random, "choice", lambda seq: "Heads")  # a re-flip would change the winner
    other = "Bowl" if flip["toss_recommended"] == "Bat" else "Bat"
    done = _spin(authenticated_client, match_id, decision=other).get_json()
    assert done["toss_winner"] == "TC" and done["toss_decision"] == other
    saved = json.load(open(path))
    assert saved["toss_decision"] == other and "toss_pending" not in saved


def test_pick_without_a_flip_is_rejected(app, authenticated_client, regular_user, match_file):
    data = _match_data(regular_user.id, toss_winner=None, toss_decision=None)
    match_id, _ = match_file(data)
    assert _spin(authenticated_client, match_id, decision="Bat").status_code == 400
