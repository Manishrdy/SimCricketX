"""The /statistics partnership leaderboard: one query behind both the overall
and the tournament view, carrying the Nov 2026 partnership detail."""

import uuid
from datetime import datetime

from database import db
from database.models import Match, MatchPartnership, Player, Tournament, User
from engine.stats_service import StatsService


def _match(user_id, home, away, match_format="T20", tournament_id=None):
    match = Match(
        id=str(uuid.uuid4()), user_id=user_id, home_team_id=home.id, away_team_id=away.id,
        match_format=match_format, tournament_id=tournament_id, date=datetime(2026, 11, 14),
    )
    db.session.add(match)
    db.session.flush()
    return match


def _stand(match, team, runs, balls, wicket=1, innings=1, **detail):
    b1, b2 = Player.query.filter_by(team_id=team.id).order_by(Player.id).limit(2).all()
    row = MatchPartnership(
        match_id=match.id, innings_number=innings, wicket_number=wicket,
        batsman1_id=b1.id, batsman2_id=b2.id, runs=runs, balls=balls,
        batsman1_contribution=detail.pop("c1", 0), batsman2_contribution=detail.pop("c2", 0),
        **detail,
    )
    db.session.add(row)
    return row


def test_new_detail_is_returned(app, regular_user, test_team, test_team_2):
    match = _match(regular_user.id, test_team, test_team_2)
    _stand(match, test_team, 90, 60, wicket=2, c1=50, c2=36, batsman1_balls=33, batsman2_balls=27,
           fours=8, sixes=3, is_unbroken=True)
    db.session.commit()

    [row] = StatsService().get_partnership_leaderboard(regular_user.id, match_format="T20")

    assert row["team"] == test_team.name
    assert row["opponent"] == test_team_2.name
    assert (row["runs"], row["balls"], row["wicket"], row["innings"]) == (90, 60, 2, 1)
    assert row["run_rate"] == 9.0
    assert (row["fours"], row["sixes"]) == (8, 3)
    assert (row["batsman1_contribution"], row["batsman1_balls"]) == (50, 33)
    assert (row["batsman2_contribution"], row["batsman2_balls"]) == (36, 27)
    assert row["is_unbroken"] is True
    assert row["match_id"] == match.id
    assert row["date"] == "2026-11-14"


def test_legacy_row_reports_missing_detail_not_zeroes(app, regular_user, test_team, test_team_2):
    match = _match(regular_user.id, test_team, test_team_2)
    _stand(match, test_team, 120, 80)  # pre-Nov-2026 save: 0/0 split, no detail
    db.session.commit()

    [row] = StatsService().get_partnership_leaderboard(regular_user.id, match_format="T20")

    assert row["batsman1_contribution"] is None and row["batsman2_contribution"] is None
    assert row["fours"] is None and row["sixes"] is None
    assert row["is_unbroken"] is False


def test_zero_ball_placeholders_are_excluded_and_order_is_by_runs(app, regular_user, test_team, test_team_2):
    match = _match(regular_user.id, test_team, test_team_2)
    _stand(match, test_team, 1, 0, wicket=1)
    _stand(match, test_team, 40, 30, wicket=2)
    _stand(match, test_team, 75, 50, wicket=3)
    _stand(match, test_team, 75, 41, wicket=4)
    db.session.commit()

    rows = StatsService().get_partnership_leaderboard(regular_user.id, match_format="T20")

    # Quicker stand wins a tie on runs; the zero-ball row is gone.
    assert [(r["runs"], r["balls"]) for r in rows] == [(75, 41), (75, 50), (40, 30)]


def test_format_and_tournament_scoping(app, regular_user, test_team, test_team_2, test_tournament):
    t20 = _match(regular_user.id, test_team, test_team_2, "T20")
    fc = _match(regular_user.id, test_team, test_team_2, "FC")
    cup = _match(regular_user.id, test_team, test_team_2, "T20", tournament_id=test_tournament.id)
    _stand(t20, test_team, 50, 30)
    _stand(fc, test_team, 200, 400, innings=3)
    _stand(cup, test_team_2, 60, 36)
    db.session.commit()
    service = StatsService()

    overall = service.get_partnership_leaderboard(regular_user.id, match_format="T20")
    assert [(r["runs"], r["tournament"]) for r in overall] == [(60, test_tournament.name), (50, None)]

    [fc_row] = service.get_partnership_leaderboard(regular_user.id, match_format="FC")
    assert (fc_row["runs"], fc_row["innings"], fc_row["run_rate"]) == (200, 3, 3.0)

    [cup_row] = service.get_tournament_partnership_leaderboard(
        regular_user.id, test_tournament.id, match_format="T20")
    assert (cup_row["runs"], cup_row["team"], cup_row["opponent"]) == (60, test_team_2.name, test_team.name)


def test_hundred_rate_is_per_100_balls(app, regular_user, test_team, test_team_2):
    match = _match(regular_user.id, test_team, test_team_2, "Hundred")
    _stand(match, test_team, 60, 40)
    db.session.commit()

    [row] = StatsService().get_partnership_leaderboard(regular_user.id, match_format="Hundred")
    assert row["run_rate"] == 150.0


def test_other_users_matches_are_never_listed(app, regular_user, admin_user, test_team, test_team_2):
    match = _match(admin_user.id, test_team, test_team_2)
    _stand(match, test_team, 99, 60)
    db.session.commit()

    assert StatsService().get_partnership_leaderboard(regular_user.id, match_format="T20") == []


def test_both_endpoints_share_one_shape(authenticated_client, regular_user, test_team, test_team_2, test_tournament):
    cup = _match(regular_user.id, test_team, test_team_2, "T20", tournament_id=test_tournament.id)
    _stand(cup, test_team, 88, 55, c1=40, c2=44, fours=6, sixes=4)
    db.session.commit()

    overall = authenticated_client.get("/api/partnerships?match_format=T20").get_json()
    tournament = authenticated_client.get(
        f"/api/tournament/{test_tournament.id}/partnerships?match_format=T20").get_json()

    assert overall["success"] and tournament["success"]
    assert overall["data"] == tournament["data"]
    assert overall["data"][0]["runs"] == 88 and overall["data"][0]["fours"] == 6



# ---- By wicket: the highest stand for each wicket ----

def test_wicket_records_pick_the_best_stand_per_wicket_in_order(app, regular_user, test_team, test_team_2):
    match = _match(regular_user.id, test_team, test_team_2)
    _stand(match, test_team, 30, 20, wicket=3)
    _stand(match, test_team, 80, 50, wicket=1)
    _stand(match, test_team_2, 95, 70, wicket=1)
    _stand(match, test_team_2, 95, 60, wicket=1, is_unbroken=True)
    _stand(match, test_team, 5, 0, wicket=2)  # zero-ball placeholder
    db.session.commit()

    records = StatsService().get_partnership_wicket_records(regular_user.id, match_format="T20")

    # Wicket order, one row each; the 2nd wicket has no real stand so is absent.
    assert [(r["wicket"], r["runs"], r["balls"]) for r in records] == [(1, 95, 60), (3, 30, 20)]
    assert records[0]["is_unbroken"] is True and records[0]["team"] == test_team_2.name


def test_wicket_records_respect_format_tournament_and_owner(app, regular_user, admin_user, test_team, test_team_2, test_tournament):
    cup = _match(regular_user.id, test_team, test_team_2, "T20", tournament_id=test_tournament.id)
    friendly = _match(regular_user.id, test_team, test_team_2, "T20")
    other_format = _match(regular_user.id, test_team, test_team_2, "ListA")
    someone_else = _match(admin_user.id, test_team, test_team_2, "T20")
    _stand(cup, test_team, 40, 30)
    _stand(friendly, test_team, 70, 45)
    _stand(other_format, test_team, 150, 120)
    _stand(someone_else, test_team, 300, 120)
    db.session.commit()
    service = StatsService()

    assert [r["runs"] for r in service.get_partnership_wicket_records(regular_user.id, match_format="T20")] == [70]
    assert [r["runs"] for r in service.get_partnership_wicket_records(
        regular_user.id, test_tournament.id, match_format="T20")] == [40]


# ---- Best pairs ----

def test_pairs_add_up_stands_whichever_way_round_the_batters_were_recorded(app, regular_user, test_team, test_team_2):
    match = _match(regular_user.id, test_team, test_team_2)
    first = _stand(match, test_team, 120, 80, wicket=1)
    _stand(match, test_team, 55, 40, wicket=2)
    b1, b2 = first.batsman1_id, first.batsman2_id
    db.session.add(MatchPartnership(  # same two batters, recorded the other way round
        match_id=match.id, innings_number=1, wicket_number=3,
        batsman1_id=b2, batsman2_id=b1, runs=25, balls=30))
    db.session.add(MatchPartnership(  # zero-ball placeholder: not a stand
        match_id=match.id, innings_number=1, wicket_number=4,
        batsman1_id=b1, batsman2_id=b2, runs=1, balls=0))
    _stand(match, test_team_2, 60, 36)
    db.session.commit()

    pairs = StatsService().get_partnership_pairs(regular_user.id, match_format="T20")

    assert [(p["runs"], p["stands"]) for p in pairs] == [(200, 3), (60, 1)]
    top = pairs[0]
    assert {top["batsman1_id"], top["batsman2_id"]} == {b1, b2}
    assert top["team"] == test_team.name
    assert (top["balls"], top["best"], top["fifty_stands"], top["hundred_stands"]) == (150, 120, 2, 1)
    assert top["average"] == 66.67
    assert top["run_rate"] == 8.0


def test_pairs_respect_tournament_owner_and_limit(app, regular_user, admin_user, test_team, test_team_2, test_tournament):
    cup = _match(regular_user.id, test_team, test_team_2, "T20", tournament_id=test_tournament.id)
    friendly = _match(regular_user.id, test_team, test_team_2, "T20")
    someone_else = _match(admin_user.id, test_team, test_team_2, "T20")
    _stand(cup, test_team, 40, 30)
    _stand(friendly, test_team, 70, 45)
    _stand(friendly, test_team_2, 20, 18)
    _stand(someone_else, test_team, 300, 120)
    db.session.commit()
    service = StatsService()

    assert [p["runs"] for p in service.get_partnership_pairs(regular_user.id, match_format="T20")] == [110, 20]
    assert [p["runs"] for p in service.get_partnership_pairs(regular_user.id, match_format="T20", limit=1)] == [110]
    assert [p["runs"] for p in service.get_partnership_pairs(
        regular_user.id, test_tournament.id, match_format="T20")] == [40]


def test_hundred_pair_rate_is_per_100_balls(app, regular_user, test_team, test_team_2):
    match = _match(regular_user.id, test_team, test_team_2, "Hundred")
    _stand(match, test_team, 60, 40)
    db.session.commit()

    [pair] = StatsService().get_partnership_pairs(regular_user.id, match_format="Hundred")
    assert pair["run_rate"] == 150.0


# ---- Endpoints ----

def test_by_wicket_and_pairs_endpoints(authenticated_client, regular_user, test_team, test_team_2, test_tournament):
    cup = _match(regular_user.id, test_team, test_team_2, "T20", tournament_id=test_tournament.id)
    friendly = _match(regular_user.id, test_team, test_team_2, "T20")
    _stand(cup, test_team, 40, 30, wicket=2)
    _stand(friendly, test_team, 70, 45, wicket=1)
    db.session.commit()

    by_wicket = authenticated_client.get("/api/partnerships/by-wicket?match_format=T20").get_json()
    assert by_wicket["success"] and [r["wicket"] for r in by_wicket["data"]] == [1, 2]

    cup_only = authenticated_client.get(
        f"/api/partnerships/by-wicket?tournament_id={test_tournament.id}").get_json()
    assert [r["runs"] for r in cup_only["data"]] == [40]

    pairs = authenticated_client.get("/api/partnerships/pairs?match_format=T20").get_json()
    assert pairs["success"] and pairs["count"] == 1 and pairs["data"][0]["runs"] == 110

    cup_pairs = authenticated_client.get(
        f"/api/partnerships/pairs?tournament_id={test_tournament.id}").get_json()
    assert cup_pairs["data"][0]["runs"] == 40

    assert authenticated_client.get("/api/partnerships/pairs?limit=500").status_code == 400
    assert authenticated_client.get("/api/partnerships/pairs?scheduled_overs=banana").status_code == 400


def test_new_endpoints_require_login(client):
    for path in ("/api/partnerships/by-wicket", "/api/partnerships/pairs"):
        assert client.get(path).status_code in (302, 401)


# ---- Player profile ----

def _pair(team):
    return Player.query.filter_by(team_id=team.id).order_by(Player.id).limit(2).all()


def test_player_stats_are_seen_from_the_players_side(app, regular_user, test_team, test_team_2):
    match = _match(regular_user.id, test_team, test_team_2)
    b1, b2 = _pair(test_team)
    _stand(match, test_team, 90, 60, wicket=2, c1=50, c2=36, batsman1_balls=33, batsman2_balls=27,
           fours=8, sixes=3, is_unbroken=True)
    _stand(match, test_team, 120, 80, wicket=1)   # legacy: no split, no detail
    _stand(match, test_team, 1, 0, wicket=3)      # zero-ball placeholder
    db.session.commit()

    stats = StatsService().get_player_partnership_stats(b2.id, regular_user.id, None, "T20")

    assert (stats["total_partnerships"], stats["total_runs"], stats["best"]) == (2, 210, 120)
    assert stats["average_partnership"] == 105.0
    assert stats["milestones"] == {"50+": 2, "100+": 1, "150+": 0, "200+": 0}
    assert stats["partners"] == [
        {"id": b1.id, "name": b1.name, "count": 2, "total_runs": 210, "best": 120, "avg_runs": 105.0}]

    legacy, detailed = stats["best_partnerships"]
    assert legacy["runs"] == 120 and legacy["player_contribution"] is None and legacy["fours"] is None
    # b2 was recorded as batsman 2, so their own share is the second one.
    assert (detailed["player_contribution"], detailed["player_balls"]) == (36, 27)
    assert (detailed["partner_contribution"], detailed["partner_balls"]) == (50, 33)
    assert detailed["partner_id"] == b1.id and detailed["is_unbroken"] is True
    assert detailed["opponent"] == test_team_2.name and detailed["run_rate"] == 9.0


def test_player_stats_scope_and_ownership(app, regular_user, admin_user, test_team, test_team_2):
    b1, _ = _pair(test_team)
    _stand(_match(regular_user.id, test_team, test_team_2, "T20"), test_team, 40, 30)
    _stand(_match(regular_user.id, test_team, test_team_2, "ListA"), test_team, 150, 120)
    _stand(_match(admin_user.id, test_team, test_team_2, "T20"), test_team, 300, 120)
    db.session.commit()
    service = StatsService()

    assert service.get_player_partnership_stats(b1.id, regular_user.id, None, "T20")["total_runs"] == 40
    assert service.get_player_partnership_stats(b1.id, regular_user.id, None, "ListA")["total_runs"] == 150

    empty = service.get_player_partnership_stats(b1.id, regular_user.id, None, "FC")
    assert empty["total_partnerships"] == 0 and empty["best_partnerships"] == [] and empty["partners"] == []

    assert service.get_player_partnership_stats(b1.id, admin_user.id, None, "T20") == {"error": "Unauthorized"}
    assert service.get_player_partnership_stats(999999, regular_user.id, None, "T20") == {"error": "Player not found"}


def test_profile_page_shows_partnerships(authenticated_client, regular_user, test_team, test_team_2):
    match = _match(regular_user.id, test_team, test_team_2)
    b1, b2 = _pair(test_team)
    _stand(match, test_team, 90, 60, wicket=2, c1=50, c2=36, batsman1_balls=33, batsman2_balls=27,
           fours=8, sixes=3, is_unbroken=True)
    db.session.commit()

    html = authenticated_client.get(f"/player/{b1.id}?match_format=T20").get_data(as_text=True)

    assert 'id="pp-partnerships"' in html
    assert f'href="/player/{b2.id}?match_format=T20">{b2.name}</a>' in html
    assert ">90*</td>" in html and "50 (33) / 36 (27)" in html and ">2nd</td>" in html
    assert f'href="/match/{match.id}/scoreboard"' in html


def test_profile_page_without_partnerships_says_so(authenticated_client, test_team):
    b1, _ = _pair(test_team)
    html = authenticated_client.get(f"/player/{b1.id}?match_format=T20").get_data(as_text=True)
    assert 'id="pp-partnerships"' in html and "No partnerships yet" in html
    assert 'id="pp-best-stands"' not in html
