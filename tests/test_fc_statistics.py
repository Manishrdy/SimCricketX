"""First-Class statistics integration coverage."""

from datetime import datetime

import pytest
from sqlalchemy import text

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
from engine.tournament_engine import TournamentEngine
from migrations.extend_fc_statistics_cache import run_migration


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


def test_fc_tournament_cache_matches_direct_aggregation(fc_statistics_data):
    data = fc_statistics_data
    service = StatsService()
    direct = service.get_tournament_stats(
        data["user"].id, data["tournament"].id, "FC"
    )

    TournamentEngine().rebuild_player_stats_cache(
        data["tournament"].id, {data["player"].id, data["opponent"].id}
    )
    db.session.commit()
    cached = service.get_tournament_stats(
        data["user"].id, data["tournament"].id, "FC"
    )

    direct_bat = _player_row(direct["batting"], data["player"].id)
    cached_bat = _player_row(cached["batting"], data["player"].id)
    for key in (
        "matches", "innings", "runs", "balls", "highest_score", "hundreds",
        "double_centuries", "triple_centuries", "average",
    ):
        assert cached_bat[key] == direct_bat[key]

    direct_bowl = _player_row(direct["bowling"], data["player"].id)
    cached_bowl = _player_row(cached["bowling"], data["player"].id)
    for key in (
        "matches", "innings", "runs", "wickets", "maidens", "best",
        "best_match", "five_wicket_hauls", "ten_wicket_matches", "dots",
        "bowled", "lbw", "wides", "no_balls", "byes", "leg_byes",
        "average", "economy", "strike_rate",
    ):
        assert cached_bowl[key] == direct_bowl[key]

    assert {row["player_id"] for row in cached["batting"]} == {
        row["player_id"] for row in direct["batting"]
    }
    assert cached["fielding"] == direct["fielding"]
    assert cached["leaderboards"] == direct["leaderboards"]


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


def test_fc_cache_migration_is_idempotent_on_current_schema(app):
    # Recreate the cache table with its pre-FC-extension schema so the test
    # proves that dry-run is read-only and --apply performs the ALTERs.
    db.session.remove()
    with db.engine.begin() as conn:
        conn.execute(text("DROP TABLE tournament_player_stats_cache"))
        conn.execute(text("""
            CREATE TABLE tournament_player_stats_cache (
                id INTEGER PRIMARY KEY,
                tournament_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                matches_played INTEGER DEFAULT 0,
                innings_batted INTEGER DEFAULT 0,
                runs_scored INTEGER DEFAULT 0,
                balls_faced INTEGER DEFAULT 0,
                fours INTEGER DEFAULT 0,
                sixes INTEGER DEFAULT 0,
                not_outs INTEGER DEFAULT 0,
                highest_score INTEGER DEFAULT 0,
                fifties INTEGER DEFAULT 0,
                centuries INTEGER DEFAULT 0,
                batting_average FLOAT DEFAULT 0,
                batting_strike_rate FLOAT DEFAULT 0,
                innings_bowled INTEGER DEFAULT 0,
                overs_bowled VARCHAR(10) DEFAULT '0.0',
                runs_conceded INTEGER DEFAULT 0,
                wickets_taken INTEGER DEFAULT 0,
                maidens INTEGER DEFAULT 0,
                best_bowling_wickets INTEGER DEFAULT 0,
                best_bowling_runs INTEGER DEFAULT 0,
                five_wicket_hauls INTEGER DEFAULT 0,
                bowling_average FLOAT DEFAULT 0,
                bowling_economy FLOAT DEFAULT 0,
                bowling_strike_rate FLOAT DEFAULT 0,
                catches INTEGER DEFAULT 0,
                run_outs INTEGER DEFAULT 0,
                stumpings INTEGER DEFAULT 0
            )
        """))

    dry_run = run_migration(db, app)
    assert "double_centuries" in dry_run["missing_columns"]
    with db.engine.connect() as conn:
        after_dry_run = {
            row[1] for row in conn.execute(text(
                "PRAGMA table_info(tournament_player_stats_cache)"
            )).fetchall()
        }
    assert "double_centuries" not in after_dry_run

    first_apply = run_migration(db, app, apply=True)
    second_apply = run_migration(db, app, apply=True)
    assert first_apply["missing_columns"] == []
    assert second_apply["missing_columns"] == []
