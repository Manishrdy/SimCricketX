"""First-Class statistics integration coverage."""

from datetime import datetime

import pytest

from app import db
from database.models import (
    Match,
    MatchScorecard,
    Player,
    Team,
    TeamProfile,
    Tournament,
    TournamentFixture,
)
from engine.stats_service import StatsService


@pytest.fixture
def fc_statistics_data(app, regular_user):
    home = Team(user_id=regular_user.id, name="Red County", short_code="RED")
    away = Team(user_id=regular_user.id, name="Blue County", short_code="BLU")
    db.session.add_all([home, away])
    db.session.flush()

    home_profile = TeamProfile(team_id=home.id, format_type="FC")
    away_profile = TeamProfile(team_id=away.id, format_type="FC")
    db.session.add_all([home_profile, away_profile])
    db.session.flush()

    all_rounder = Player(
        team_id=home.id,
        profile_id=home_profile.id,
        name="A. Allrounder",
        role="All-rounder",
    )
    opponent = Player(
        team_id=away.id,
        profile_id=away_profile.id,
        name="B. Batter",
        role="Batsman",
    )
    db.session.add_all([all_rounder, opponent])
    db.session.flush()

    tournament = Tournament(
        user_id=regular_user.id,
        name="County Championship",
        mode="custom_series",
        format_type="FC",
    )
    db.session.add(tournament)
    db.session.flush()

    match = Match(
        id="fc-statistics-match",
        user_id=regular_user.id,
        home_team_id=home.id,
        away_team_id=away.id,
        tournament_id=tournament.id,
        match_format="FC",
        date=datetime(2026, 8, 20),
        match_status="drawn",
        result_description="Match drawn",
        home_team_score=350,
        home_team_wickets=10,
        home_team_overs="110.0",
        away_team_score=300,
        away_team_wickets=10,
        away_team_overs="105.0",
        home_team_score_innings2=180,
        home_team_wickets_innings2=5,
        home_team_overs_innings2="55.0",
        away_team_score_innings2=230,
        away_team_wickets_innings2=8,
        away_team_overs_innings2="70.0",
        toss_winner_team_id=home.id,
        toss_decision="Bat",
    )
    db.session.add(match)
    db.session.flush()

    db.session.add(TournamentFixture(
        tournament_id=tournament.id,
        home_team_id=home.id,
        away_team_id=away.id,
        match_id=match.id,
        status="Completed",
    ))

    db.session.add_all([
        MatchScorecard(
            match_id=match.id, player_id=all_rounder.id, team_id=home.id,
            innings_number=1, record_type="batting", runs=250, balls=400,
            fours=25, sixes=2, ones=80, twos=35, threes=2,
            dot_balls=180, is_out=True, catches=1,
        ),
        MatchScorecard(
            match_id=match.id, player_id=all_rounder.id, team_id=home.id,
            innings_number=3, record_type="batting", runs=320, balls=500,
            fours=32, sixes=4, ones=90, twos=40, threes=4,
            dot_balls=240, is_out=True,
        ),
        MatchScorecard(
            match_id=match.id, player_id=all_rounder.id, team_id=home.id,
            innings_number=2, record_type="bowling", balls_bowled=180,
            overs="30.0", runs_conceded=80, wickets=6, maidens=8,
            dot_balls_bowled=110, wickets_bowled=2, wickets_lbw=1,
            wides=3, noballs=1, byes=2, leg_byes=1, catches=1,
        ),
        MatchScorecard(
            match_id=match.id, player_id=all_rounder.id, team_id=home.id,
            innings_number=4, record_type="bowling", balls_bowled=120,
            overs="20.0", runs_conceded=70, wickets=5, maidens=4,
            dot_balls_bowled=70, wickets_bowled=1, wickets_lbw=2,
            wides=2, noballs=0, byes=1, leg_byes=2,
        ),
        # A persisted super-over-shaped row must not affect FC innings stats.
        MatchScorecard(
            match_id=match.id, player_id=all_rounder.id, team_id=home.id,
            innings_number=5, record_type="batting", runs=99, balls=10,
            is_out=True, is_super_over=True,
        ),
        MatchScorecard(
            match_id=match.id, player_id=opponent.id, team_id=away.id,
            innings_number=2, record_type="batting", runs=100, balls=180,
            is_out=True,
        ),
    ])
    db.session.commit()

    return {
        "user": regular_user,
        "home": home,
        "away": away,
        "player": all_rounder,
        "opponent": opponent,
        "tournament": tournament,
        "match": match,
    }


def _player_row(rows, player_id):
    return next(row for row in rows if row["player_id"] == player_id)


def test_fc_direct_aggregation_uses_all_innings_and_native_milestones(fc_statistics_data):
    data = fc_statistics_data
    stats = StatsService().get_overall_stats(data["user"].id, "FC")

    batting = _player_row(stats["batting"], data["player"].id)
    assert batting["matches"] == 1
    assert batting["innings"] == 2
    assert batting["runs"] == 570
    assert batting["highest_score"] == 320
    assert batting["hundreds"] == 2
    assert batting["double_centuries"] == 1
    assert batting["triple_centuries"] == 1
    assert batting["average"] == 285.0

    bowling = _player_row(stats["bowling"], data["player"].id)
    assert bowling["innings"] == 2
    assert bowling["wickets"] == 11
    assert bowling["maidens"] == 12
    assert bowling["best"] == "6/80"
    assert bowling["best_match"] == "11/150"
    assert bowling["five_wicket_hauls"] == 2
    assert bowling["ten_wicket_matches"] == 1
    assert stats["leaderboards"]["highest_sr"] == []
    assert stats["leaderboards"]["best_match_figures"][0]["figures"] == "11/150"


def test_fc_tournament_view_matches_overall_aggregation(fc_statistics_data):
    """The tournament view reads the same scorecards as the overall view."""
    data = fc_statistics_data
    service = StatsService()
    tournament = service.get_tournament_stats(
        data["user"].id, data["tournament"].id, "FC"
    )
    assert tournament == service.get_overall_stats(data["user"].id, "FC")
    assert _player_row(tournament["batting"], data["player"].id)["runs"] == 570
    assert _player_row(tournament["bowling"], data["player"].id)["best_match"] == "11/150"


def test_fc_profile_team_and_h2h_keep_complete_match_shape(fc_statistics_data):
    data = fc_statistics_data
    service = StatsService()

    profile = service.get_player_profile(data["player"].id, data["user"].id, "FC")
    assert len(profile["match_log"]) == 1
    assert [i["innings_number"] for i in profile["match_log"][0]["batting_innings"]] == [1, 3]
    assert [i["innings_number"] for i in profile["match_log"][0]["bowling_innings"]] == [2, 4]
    assert profile["bowling"]["best_match_figures"] == "11/150"

    team = service.get_team_stats(data["user"].id, data["home"].id, "FC")
    assert team["summary"]["drawn"] == 1
    assert team["summary"]["avg_scored"] == 530.0
    assert team["summary"]["avg_conceded"] == 530.0
    assert team["recent"][0]["result"] == "D"
    assert team["recent"][0]["score"] == "350/10 & 180/5"

    h2h = service.get_head_to_head(
        data["user"].id, data["home"].id, data["away"].id, "FC"
    )
    assert h2h["summary"]["draws"] == 1
    assert h2h["summary"]["ties"] == 0
    assert h2h["matches"][0]["home_score"] == "350/10 & 180/5"
    assert h2h["matches"][0]["away_score"] == "300/10 & 230/8"

    insights = service.get_insights(data["user"].id, match_format="FC")
    assert insights["conditions"]["venues"][0]["avg_runs"] == 1060.0
    assert insights["conditions"]["venues"][0]["avg_wkts"] == 33.0
    batter_form = next(
        row for row in insights["form"]["batting"]
        if row["player"] == data["player"].name
    )
    assert batter_form["series"] == [250, 320]


def test_fc_hub_and_csv_render_fc_specific_fields(authenticated_client, fc_statistics_data):
    page = authenticated_client.get("/statistics?match_format=FC")
    assert page.status_code == 200
    assert b"First-Class" in page.data
    assert b"BBM" in page.data
    assert b"10WM" in page.data

    export = authenticated_client.get(
        "/statistics/export/batting/csv?match_format=FC"
    )
    assert export.status_code == 200
    assert "overall_FC_batting_stats.csv" in export.headers["Content-Disposition"]
    assert b"highest_score" in export.data
    assert b"double_centuries" in export.data
    assert b"triple_centuries" in export.data
