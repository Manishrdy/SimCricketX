"""
End-to-end First-Class match.

Everything above this file tests the engine directly. This one drives the
whole stack the way the app actually does: real teams and FC squads in the
database, the match created through /match/setup, and then played out ball
by ball through /match/<id>/next-ball until a result — checking on the way
that the things a first-class match is made of actually reach the client.
"""
import json

import pytest

from database import db
from database.models import Team, TeamProfile, Player, Match as DBMatch


# A realistic first-class XI: specialist top six, a keeper, an all-rounder,
# then a genuine tail — with the FC-only rating axes populated, since the
# whole point is to prove they survive the trip from DB to engine.
FC_XI = [
    # name,          role,           bat bowl fld tech temp stam  hand   type
    ("Opener One",   "Batsman",       74, 20, 68, 76, 74, 55, "Right", "Medium"),
    ("Opener Two",   "Batsman",       72, 20, 68, 74, 72, 55, "Left",  "Medium"),
    ("Number Three", "Batsman",       78, 25, 70, 80, 78, 55, "Right", "Medium"),
    ("Number Four",  "Batsman",       76, 30, 66, 76, 76, 55, "Right", "Off spin"),
    ("Number Five",  "Batsman",       70, 20, 66, 70, 70, 55, "Left",  "Medium"),
    ("The Keeper",   "Wicketkeeper",  64, 30, 82, 64, 66, 55, "Right", "Medium"),
    ("All Rounder",  "All-rounder",   58, 68, 72, 58, 62, 68, "Right", "Fast-medium"),
    ("Strike Bowler","Bowler",        40, 74, 64, 42, 50, 72, "Right", "Fast"),
    ("Second Seam",  "Bowler",        28, 76, 62, 30, 45, 70, "Left",  "Fast-medium"),
    ("Off Spinner",  "Bowler",        20, 72, 60, 22, 40, 74, "Right", "Off spin"),
    ("Leg Spinner",  "Bowler",        12, 70, 58, 14, 38, 74, "Right", "Leg spin"),
]


def _make_fc_team(user_id, name, code):
    team = Team(user_id=user_id, name=name, short_code=code)
    db.session.add(team)
    db.session.flush()
    profile = TeamProfile(team_id=team.id, format_type="FC")
    db.session.add(profile)
    db.session.flush()
    for i, (pname, role, bat, bowl, fld, tech, temp, stam, hand, btype) in enumerate(FC_XI):
        db.session.add(Player(
            team_id=team.id, profile_id=profile.id,
            name=f"{code} {pname}", role=role,
            batting_rating=bat, bowling_rating=bowl, fielding_rating=fld,
            technique_rating=tech, temperament_rating=temp, stamina_rating=stam,
            batting_hand=hand, bowling_type=btype, bowling_hand="Right",
            is_captain=(i == 0), is_wicketkeeper=(i == 5),
        ))
    db.session.commit()
    return team


@pytest.fixture(autouse=True)
def _no_rate_limit():
    """Playing a match out takes thousands of next-ball calls; the per-user
    limiter is 100/minute. Clear its window between requests rather than
    raising the ceiling, so the decorator itself still runs."""
    import app as app_module
    store = app_module._rate_limit_store

    class _Clearing(dict):
        def __getitem__(self, key):
            value = super().__getitem__(key)
            value.clear()
            return value

    app_module._rate_limit_store = _Clearing(store)
    try:
        yield
    finally:
        app_module._rate_limit_store = store


@pytest.fixture
def fc_teams(app, regular_user):
    return (_make_fc_team(regular_user.id, "Red County", "RED"),
            _make_fc_team(regular_user.id, "Blue County", "BLU"))


def _xi_payload(code):
    """The XI as the match-setup page sends it: names plus who is bowling."""
    return [
        {"name": f"{code} {name}",
         "will_bowl": role in ("Bowler", "All-rounder")}
        for (name, role, *_rest) in FC_XI
    ]


def _create_match(client, home, away, days=4, pitch="Hard", send_xi=True):
    """Create the match through the real setup route.

    send_xi mirrors the match-setup page, which always posts its own XI.
    Passing False exercises the route's default instead.
    """
    payload = {
        "team_home": home.id, "team_away": away.id,
        "match_format": "FC", "days": days,
        "stadium": "Lord's", "pitch": pitch,
        "toss_winner": "HOM", "toss_decision": "Bat",
        "simulation_mode": "auto", "weather_forecast": "clear",
    }
    if send_xi:
        payload["playing_xi"] = {"home": _xi_payload("RED"), "away": _xi_payload("BLU")}
    resp = client.post("/match/setup", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    return resp.get_json()


def test_fc_match_plays_through_to_a_result(app, authenticated_client, fc_teams):
    """Play a four-day match out over the live routes and check it reads like
    first-class cricket from the outside."""
    home, away = fc_teams
    created = _create_match(authenticated_client, home, away)
    match_id = created.get("match_id") or created.get("id")
    assert match_id, f"setup returned no match id: {created}"

    intervals, stumps, innings_ends = [], [], []
    declarations = 0
    result = None
    days_seen = set()

    for _ in range(40000):
        resp = authenticated_client.post(f"/match/{match_id}/next-ball")
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        data = resp.get_json()
        assert "error" not in data, data.get("error")

        if data.get("fc_day"):
            days_seen.add(data["fc_day"])
        if data.get("fc_interval"):
            intervals.append((data["day_number"], data["interval_name"],
                              data["session_number"], data["session_summary"]))
            assert data["scorecard_data"], "an interval must carry a scoreboard"
        if data.get("day_break"):
            stumps.append(data["day_number"])
            assert data["scorecard_data"], "stumps must carry a scoreboard"
        if data.get("innings_end"):
            innings_ends.append(data.get("innings_number"))
            assert data.get("scorecard_data"), "an innings end must carry a scoreboard"
        if data.get("match_over"):
            result = data.get("result") or data.get("commentary")
            break

    # --- it finished, as a real match ---
    assert result, "the match never reached a result"
    assert len(innings_ends) >= 2, f"only {len(innings_ends)} innings completed"
    assert innings_ends == sorted(innings_ends), "innings arrived out of order"

    # --- the day had a shape ---
    assert intervals, "no Lunch or Tea was ever taken"
    names = {i[1] for i in intervals}
    assert names <= {"Lunch", "Tea"}, f"unexpected interval names: {names}"
    for _day, _name, session_no, summary in intervals:
        assert session_no in (1, 2)
        assert summary["overs"] > 0
        assert summary["runs"] >= 0 and summary["wickets"] >= 0
    assert stumps, "the match never reached stumps"
    assert max(days_seen) >= 2, f"only reached day {max(days_seen)}"

    # --- and it was archived ---
    with app.app_context():
        archived = db.session.get(DBMatch, match_id)
        assert archived.match_format == "FC"
        assert archived.days == 4
        assert archived.match_status in ("completed", "drawn", "tied"), archived.match_status


def test_fc_ratings_reach_the_engine_through_match_setup(app, authenticated_client, fc_teams):
    """The plumbing fix: technique/temperament/stamina were declared on the
    player and read by the engine, but never passed by the setup route — so
    in a real match the defensive-technique blend, both pressure dampeners
    and per-bowler fatigue were all silently inert."""
    home, away = fc_teams
    created = _create_match(authenticated_client, home, away)
    match_id = created.get("match_id") or created.get("id")
    assert match_id, f"setup returned no match id: {created}"
    # One delivery is enough to build the live instance.
    resp = authenticated_client.post(f"/match/{match_id}/next-ball")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]

    # Read the XI the engine was actually handed.
    from app import MATCH_INSTANCES
    inst = MATCH_INSTANCES.get(match_id)
    assert inst is not None, "no live match instance after a delivery"
    for xi_name, xi in (("home", inst.home_xi), ("away", inst.away_xi)):
        for p in xi:
            for axis in ("technique_rating", "temperament_rating", "stamina_rating"):
                assert p.get(axis), f"{xi_name} {p['name']} reached the engine without {axis}"
        # ...and they are the real values, not a default fill.
        assert {p["technique_rating"] for p in xi} != {50}, (
            f"{xi_name} technique ratings look like defaults, not the squad's")


def test_default_xi_picks_an_attack_from_a_realistic_squad(app, authenticated_client, fc_teams):
    """A client that posts no playing_xi must still get a playable match.

    The default used to mark will_bowl only for bowling roles in the first
    five BATTING positions. A real side bats its bowlers at 7-11, so nobody
    was marked, and the match died on the first delivery with "Bowler
    selection failed at over 0.0".
    """
    from utils.squad_rules import MIN_BOWLING_OPTIONS
    from app import MATCH_INSTANCES

    home, away = fc_teams
    created = _create_match(authenticated_client, home, away, send_xi=False)
    match_id = created["match_id"]

    # It must survive the first ball — that is where it used to fall over.
    resp = authenticated_client.post(f"/match/{match_id}/next-ball")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "error" not in data, data.get("error")
    assert data.get("bowler"), "no bowler was selected for the first over"

    inst = MATCH_INSTANCES.get(match_id)
    for side, xi in (("home", inst.home_xi), ("away", inst.away_xi)):
        attack = [p for p in xi if p.get("will_bowl")]
        assert len(attack) == MIN_BOWLING_OPTIONS, (
            f"{side} default XI marked {len(attack)} bowlers, "
            f"expected {MIN_BOWLING_OPTIONS}")
        # ...and they are the actual bowling roles, wherever they bat.
        assert all(p["role"] in ("Bowler", "All-rounder") for p in attack), (
            f"{side} attack contains a specialist batter: "
            f"{[(p['name'], p['role']) for p in attack]}")


def test_scorecard_omits_bowlers_who_never_bowled(app, authenticated_client, fc_teams):
    """A real scorecard lists the bowlers who bowled, not everyone who might
    have. The card used to carry a row of empty strings for each unused
    bowler, which the UI rendered verbatim as a blank line — most visible in
    first-class cricket, where the fifth bowler often isn't needed."""
    home, away = fc_teams
    match_id = _create_match(authenticated_client, home, away)["match_id"]

    seen_cards = 0
    for _ in range(40000):
        data = authenticated_client.post(f"/match/{match_id}/next-ball").get_json()
        assert "error" not in data, data.get("error")
        card = data.get("scorecard_data")
        if card and card.get("bowlers"):
            seen_cards += 1
            for b in card["bowlers"]:
                assert b.get("overs") not in ("", None), (
                    f"{b.get('name')} is on the card without having bowled: {b}")
                assert b.get("runs") != "", f"blank figures for {b.get('name')}"
        if data.get("match_over"):
            break
    assert seen_cards, "no scorecards were produced to check"
