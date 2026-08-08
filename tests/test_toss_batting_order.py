"""The toss decides who bats.

`/spin-toss` used to patch a cached Match instance with
`batting_team = home_xi if toss_decision == "Bat" else away_xi` — the toss
winner was never consulted, so an away side that won the toss and elected to
bat did not get to bat. The mapping now lives in `engine.toss` and every call
site shares it; `/spin-toss` no longer hand-patches a cached instance at all
(it drops it, so the next request rebuilds through `Match.__init__`), and a
re-toss is refused once the match is underway.
"""
import json
import os
import random
import sys
import uuid

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.match as match_module
from engine.toss import home_bats_first, innings_teams


def _build_xi(prefix):
    return [{
        "name": f"{prefix}_P{i+1}",
        "role": "Bowler" if i < 5 else "Batsman",
        "batting_rating": 70, "bowling_rating": 70, "fielding_rating": 65,
        "batting_hand": "Right", "bowling_type": "Medium", "bowling_hand": "Right",
        "will_bowl": i < 5, "is_captain": i == 0,
    } for i in range(11)]


def _match_data(user_id, toss_winner="TW", toss_decision="Bat", call="Heads"):
    """A ready-to-toss match: TW (home) vs TC (away)."""
    return {
        "match_id": str(uuid.uuid4()), "created_by": user_id,
        "timestamp": "2026-08-07T12:00:00",
        "team_home": f"TW_{user_id}", "team_away": f"TC_{user_id}",
        "stadium": "Test Ground", "pitch": "Flat",
        "toss": call, "toss_winner": toss_winner, "toss_decision": toss_decision,
        "match_format": "T20", "overs": 20, "simulation_mode": "auto",
        "rain_probability": 0.0,
        "playing_xi": {"home": _build_xi("H"), "away": _build_xi("A")},
        "substitutes": {"home": [], "away": []},
    }


def _names(xi):
    return [p["name"] for p in xi]


# ── 1. The mapping itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("winner,decision,expected_home_first", [
    ("TW", "Bat",  True),   # home won, batted
    ("TW", "Bowl", False),  # home won, bowled → away bats
    ("TC", "Bat",  False),  # away won, batted → away bats  (the old bug)
    ("TC", "Bowl", True),   # away won, bowled → home bats  (the old bug)
])
def test_home_bats_first_all_four_outcomes(winner, decision, expected_home_first):
    assert home_bats_first(winner, decision, "TW") is expected_home_first


@pytest.mark.parametrize("decision", ["Bat", "bat", "BAT", " bat "])
def test_home_bats_first_normalises_decision_case(decision):
    assert home_bats_first("TC", decision, "TW") is False


@pytest.mark.parametrize("winner,decision", [
    (None, None), ("", "Bat"), ("TC", None), ("TC", ""),
])
def test_home_bats_first_defaults_to_home_without_toss_data(winner, decision):
    """Legacy match files with no recorded toss have always been read as
    home-bats-first; that fallback is preserved."""
    assert home_bats_first(winner, decision, "TW") is True


def test_innings_teams_swaps_sides_for_the_second_innings():
    home, away = ["H"], ["A"]
    assert innings_teams("TC", "Bat", "TW", home, away, innings=1) == (away, home)
    assert innings_teams("TC", "Bat", "TW", home, away, innings=2) == (home, away)


# ── 2. Engine contract ───────────────────────────────────────────────────────

@pytest.mark.parametrize("winner,decision,first_bat_prefix", [
    ("TW", "Bat",  "H"),
    ("TW", "Bowl", "A"),
    ("TC", "Bat",  "A"),
    ("TC", "Bowl", "H"),
])
def test_match_opens_with_the_side_the_toss_sent_in(app, regular_user, winner, decision, first_bat_prefix):
    m = match_module.Match(_match_data(regular_user.id, winner, decision))
    expected = m.home_xi if first_bat_prefix == "H" else m.away_xi
    assert m.batting_team is expected
    assert m.bowling_team is (m.away_xi if expected is m.home_xi else m.home_xi)
    # Everything __init__ derives from the toss must agree with it.
    assert m.current_striker["name"].startswith(first_bat_prefix)
    assert set(m.batsman_stats) == set(_names(expected))
    # bowler_history aliases the BowlerManager quota, so it proves the manager
    # was built from the bowling side and not the batting one.
    assert set(m.bowler_history) <= set(_names(m.bowling_team))
    assert set(m.bowler_stats) <= set(_names(m.bowling_team))


# ── 3. /spin-toss ────────────────────────────────────────────────────────────

@pytest.fixture
def match_file(app, regular_user):
    """Writes a match JSON and cleans up the file + any cached instance."""
    import app as app_module

    created = []

    def _write(data):
        match_dir = os.path.join(app_module.PROJECT_ROOT, "data", "matches")
        os.makedirs(match_dir, exist_ok=True)
        path = os.path.join(match_dir, f"match_{data['match_id']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        created.append((data["match_id"], path))
        return data["match_id"], path

    yield _write

    for match_id, path in created:
        app_module.MATCH_INSTANCES.pop(match_id, None)
        try:
            os.remove(path)
        except OSError:
            pass


def test_spin_toss_away_win_and_bat_puts_the_away_side_in(app, authenticated_client, regular_user, match_file, monkeypatch):
    """The regression case: away calls correctly and elects to bat. The old
    in-memory patch handed the innings to the home side anyway."""
    import app as app_module

    data = _match_data(regular_user.id, toss_winner=None, toss_decision=None, call="Heads")
    match_id, path = match_file(data)

    # A cached instance exists before the toss (e.g. a live-state poll built
    # one) — this is the path that used to be corrupted.
    app_module.MATCH_INSTANCES[match_id] = match_module.Match(json.loads(json.dumps(data)))

    # First element of each choice list: coin "Heads" (matches the away call,
    # so away wins) and decision "Bat".
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    resp = authenticated_client.post(f"/match/{match_id}/spin-toss")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["toss_winner"] == "TC"
    assert body["toss_decision"] == "Bat"

    with open(path, encoding="utf-8") as f:
        persisted = json.load(f)
    assert persisted["toss_winner"] == "TC"
    assert persisted["toss_decision"] == "Bat"

    # The stale instance is dropped rather than patched...
    assert match_id not in app_module.MATCH_INSTANCES

    # ...so the rebuild from the persisted toss sends the away side in to bat.
    rebuilt = match_module.Match(persisted)
    assert rebuilt.batting_team is rebuilt.away_xi
    assert rebuilt.current_striker["name"].startswith("A_")

    # End to end: the instance the app actually serves on the first delivery
    # must be batting the away side too. This is the user-visible symptom —
    # the old code served a cached instance with the home side batting.
    monkeypatch.undo()
    assert authenticated_client.post(f"/match/{match_id}/next-ball").status_code == 200
    served = app_module.MATCH_INSTANCES[match_id]
    assert served.batting_team is served.away_xi
    assert set(served.batsman_stats) == set(_names(served.away_xi))


def test_spin_toss_is_refused_once_the_match_is_underway(app, authenticated_client, regular_user, match_file):
    import app as app_module

    data = _match_data(regular_user.id, toss_winner="TW", toss_decision="Bat")
    match_id, path = match_file(data)

    live = match_module.Match(json.loads(json.dumps(data)))
    live.current_over = 5
    app_module.MATCH_INSTANCES[match_id] = live

    resp = authenticated_client.post(f"/match/{match_id}/spin-toss")
    assert resp.status_code == 409

    # The live match is left completely alone.
    assert app_module.MATCH_INSTANCES.get(match_id) is live
    assert live.current_over == 5
    with open(path, encoding="utf-8") as f:
        persisted = json.load(f)
    assert persisted["toss_winner"] == "TW"
    assert persisted["toss_decision"] == "Bat"


def test_spin_toss_is_refused_for_a_completed_match(app, authenticated_client, regular_user, match_file):
    data = _match_data(regular_user.id)
    data["current_state"] = "completed"
    match_id, _path = match_file(data)

    resp = authenticated_client.post(f"/match/{match_id}/spin-toss")
    assert resp.status_code == 409


def test_spin_toss_allowed_before_the_first_ball(app, authenticated_client, regular_user, match_file):
    """A cached but not-yet-started instance must not block the toss."""
    import app as app_module

    data = _match_data(regular_user.id, toss_winner=None, toss_decision=None)
    match_id, _path = match_file(data)
    app_module.MATCH_INSTANCES[match_id] = match_module.Match(json.loads(json.dumps(data)))

    resp = authenticated_client.post(f"/match/{match_id}/spin-toss")
    assert resp.status_code == 200
    assert resp.get_json()["toss_winner"] in {"TW", "TC"}
