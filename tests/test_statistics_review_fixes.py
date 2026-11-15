"""Regression tests for the Nov 2026 statistics review.

Each test pins one defect from that review; the docstring says what used to
happen. Kept in one module so the set can be re-run as a unit.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from app import db
from database.models import Match, MatchScorecard, Player, Team, TeamProfile
from engine.stats_aggregator import StatsAggregator
from engine.stats_service import StatsService

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------

@pytest.fixture
def league(app, regular_user):
    """Two T20 sides with one player of each squad role on the home side."""
    home = Team(user_id=regular_user.id, name="Home XI", short_code="HOM")
    away = Team(user_id=regular_user.id, name="Away XI", short_code="AWY")
    db.session.add_all([home, away])
    db.session.flush()
    home_profile = TeamProfile(team_id=home.id, format_type="T20")
    away_profile = TeamProfile(team_id=away.id, format_type="T20")
    db.session.add_all([home_profile, away_profile])
    db.session.flush()

    def player(name, role, team=home, profile=home_profile):
        row = Player(team_id=team.id, profile_id=profile.id, name=name, role=role)
        db.session.add(row)
        db.session.flush()
        return row

    data = {
        "user": regular_user, "home": home, "away": away,
        "batter": player("B. Batter", "Batsman"),
        "bowler": player("S. Seamer", "Bowler"),
        "keeper": player("K. Keeper", "Wicketkeeper"),
        "allrounder": player("A. Round", "All-rounder"),
        "rival": player("R. Rival", "Batsman", away, away_profile),
    }
    db.session.commit()
    return data


_match_seq = [0]


def add_match(data, *, date, fmt="T20", status="completed", winner=None,
              home=(160, 6), away=(150, 8), toss=None, decision=None, **extra):
    _match_seq[0] += 1
    match = Match(
        id=f"stats-review-{_match_seq[0]:03d}", user_id=data["user"].id,
        home_team_id=data["home"].id, away_team_id=data["away"].id,
        match_format=fmt, date=date, match_status=status,
        winner_team_id=winner.id if winner else None,
        home_team_score=home[0], home_team_wickets=home[1],
        away_team_score=away[0], away_team_wickets=away[1],
        toss_winner_team_id=toss.id if toss else None, toss_decision=decision,
        venue=extra.pop("venue", "Review Oval"), pitch_type=extra.pop("pitch", "Hard"),
        **extra,
    )
    db.session.add(match)
    db.session.flush()
    return match


def bat(match, player, runs, balls=20, out=True, innings=1, **extra):
    db.session.add(MatchScorecard(
        match_id=match.id, player_id=player.id, team_id=player.team_id,
        innings_number=innings, record_type="batting", runs=runs, balls=balls,
        is_out=out, **extra,
    ))


def bowl(match, player, wickets, runs, balls=24, innings=2, **extra):
    db.session.add(MatchScorecard(
        match_id=match.id, player_id=player.id, team_id=player.team_id,
        innings_number=innings, record_type="bowling", wickets=wickets,
        runs_conceded=runs, balls_bowled=balls, overs=f"{balls // 6}.{balls % 6}",
        **extra,
    ))


# --------------------------------------------------------------------------
# 2. Stumpings count towards impact, MOTM and head-to-head
# --------------------------------------------------------------------------

def test_stumpings_score_in_impact_index():
    """A stumping was worth 0; a run-out was worth 10."""
    assert StatsService._impact_index(0, 0, 0, 0, 3) == 30
    assert StatsService._impact_index(10, 1, 1, 1, 1) == 10 + 20 + 8 + 10 + 10


def test_stumpings_reach_match_impact_and_head_to_head(league):
    match = add_match(league, date=datetime(2026, 11, 1), winner=league["home"])
    bat(match, league["keeper"], 5, stumpings=3)
    bat(match, league["batter"], 20)
    db.session.commit()

    scores = {row["player_id"]: row for row in StatsService().compute_match_impact_scores(match.id)}
    assert scores[league["keeper"].id]["stumpings"] == 3
    assert scores[league["keeper"].id]["impact_score"] == 35
    # 35 beats the batter's 20: the keeper is now the top performer.
    assert max(scores.values(), key=lambda r: r["impact_score"])["player_id"] == league["keeper"].id

    h2h = StatsService().get_head_to_head(
        league["user"].id, league["home"].id, league["away"].id, "T20"
    )
    top = h2h["top_performers"]["team1"][0]
    assert (top["name"], top["stumpings"], top["impact"]) == ("K. Keeper", 3, 35)

    insights = StatsService().get_insights(league["user"].id, match_format="T20")
    assert insights["impact"][0]["player"] == "K. Keeper"
    assert insights["impact"][0]["stumpings"] == 3


# --------------------------------------------------------------------------
# 3. Form Pulse ranks on recent innings
# --------------------------------------------------------------------------

def test_form_pulse_ranks_last_five_innings_not_career_total(league):
    """Three all-time leaders were picked first, then their last five shown."""
    veteran, hot = league["batter"], league["allrounder"]
    # Veteran: 10 old innings of 60, then five failures.
    for day in range(1, 11):
        bat(add_match(league, date=datetime(2026, 1, day)), veteran, 60)
    for day in range(1, 6):
        bat(add_match(league, date=datetime(2026, 6, day)), veteran, 2)
    # In-form player: five recent fifties, nothing before.
    for day in range(1, 6):
        bat(add_match(league, date=datetime(2026, 6, day)), hot, 50)
    db.session.commit()

    form = StatsService().get_insights(league["user"].id, match_format="T20")["form"]["batting"]
    assert [row["player"] for row in form] == ["A. Round", "B. Batter"]
    assert form[0]["series"] == [50] * 5 and form[0]["total"] == 250
    assert form[1]["series"] == [2] * 5 and form[1]["total"] == 10


def test_form_series_keeps_same_date_matches_apart(league):
    """Sorting on (date, innings) interleaved two matches played the same day."""
    day = datetime(2026, 6, 1)
    first = add_match(league, date=day, fmt="FC")
    second = add_match(league, date=day, fmt="FC")
    assert first.id < second.id
    bat(first, league["batter"], 11, innings=1)
    bat(first, league["batter"], 12, innings=3)
    bat(second, league["batter"], 21, innings=1)
    bat(second, league["batter"], 22, innings=3)
    db.session.commit()

    form = StatsService().get_insights(league["user"].id, match_format="FC")["form"]["batting"]
    assert form[0]["series"] == [11, 12, 21, 22]


# --------------------------------------------------------------------------
# 4. Comparison dropdown uses the squad role
# --------------------------------------------------------------------------

def test_compare_player_list_uses_squad_role(league, authenticated_client):
    """Every XI member has a batting row, so any bowler became an all-rounder
    and nobody was ever a Wicketkeeper or a pure Bowler."""
    match = add_match(league, date=datetime(2026, 11, 1))
    for key in ("batter", "bowler", "keeper", "allrounder"):
        # did-not-bat rows exist for the whole XI
        bat(match, league[key], 0, balls=0, out=False)
    bowl(match, league["bowler"], 2, 20)
    bowl(match, league["allrounder"], 1, 30)
    db.session.commit()

    response = authenticated_client.get("/api/compare-players?player_ids=&match_format=T20")
    assert response.status_code == 200
    roles = {p["name"]: p["role"] for p in response.get_json()["available_players"]}
    assert roles == {
        "B. Batter": "Batsman", "S. Seamer": "Bowler",
        "K. Keeper": "Wicketkeeper", "A. Round": "All-rounder",
    }


def test_compare_request_uses_resolved_scope():
    """The compare call re-read the URL and fell back to T20 for a non-T20
    tournament whose link carried no match_format."""
    source = (ROOT / "templates" / "statistics.html").read_text(encoding="utf-8")
    body = source.split("function comparePlayers()", 1)[1].split("\nfunction ", 1)[0]
    assert "RESOLVED_FORMAT" in body and "RESOLVED_TOURNAMENT" in body
    assert "params.get('match_format')" not in body


# --------------------------------------------------------------------------
# 5. Player profile
# --------------------------------------------------------------------------

def test_hundred_profile_uses_ball_units_without_format_argument(league):
    """Bowling metrics got the raw (None) format, so a Hundred profile opened
    without ?match_format= was worked out in six-ball overs."""
    profile = TeamProfile.query.get(league["bowler"].profile_id)
    profile.format_type = "Hundred"
    match = add_match(league, date=datetime(2026, 11, 1), fmt="Hundred")
    bowl(match, league["bowler"], 2, 30, balls=20)
    db.session.commit()

    bowling = StatsService().get_player_profile(league["bowler"].id, league["user"].id)["bowling"]
    assert bowling["economy_unit"] == "runs/100 balls"
    assert bowling["economy"] == 150.0
    assert bowling["overs"] == 20


def test_profile_match_log_orders_same_day_matches_and_totals_rows(league):
    """The log was sorted on a YYYY-MM-DD string, and a second row for the
    same match overwrote the first."""
    morning = add_match(league, date=datetime(2026, 11, 1, 9, 0))
    evening = add_match(league, date=datetime(2026, 11, 1, 19, 0))
    earlier = add_match(league, date=datetime(2026, 10, 1))
    bat(evening, league["keeper"], 30, stumpings=2)
    bat(morning, league["keeper"], 10)
    bat(earlier, league["keeper"], 40, balls=30, innings=1)
    bat(earlier, league["keeper"], 5, balls=4, innings=2)
    db.session.commit()

    log = StatsService().get_player_profile(league["keeper"].id, league["user"].id)["match_log"]
    assert [row["match_id"] for row in log] == [evening.id, morning.id, earlier.id]
    assert log[0]["stumpings"] == 2
    assert (log[2]["bat_runs"], log[2]["bat_balls"]) == (45, 34)
    assert all(not key.startswith("_") for row in log for key in row)


def test_profile_template_layout():
    source = (ROOT / "templates" / "player_profile.html").read_text(encoding="utf-8")
    batting_card = source.split("{% if profile.batting %}", 1)[1].split("{% if profile.bowling %}", 1)[0]
    # Bowling figures used to be printed inside the batting card.
    assert "profile.bowling." not in batting_card
    assert "profile.bowling.four_wicket_hauls" in source
    # Stumpings were computed for the match log but never shown.
    assert "m.stumpings" in source and ">St</th>" in source


# --------------------------------------------------------------------------
# 6. Milestone counting
# --------------------------------------------------------------------------

def test_century_buckets_are_cumulative():
    """A 250 was a 100 and a 200, but a 310 was a 100 and a 300 with no 200."""
    innings = [{"runs": r, "balls": 300, "is_out": True, "fours": 0, "sixes": 0}
               for r in (120, 250, 310)]
    metrics = StatsService()._calculate_batting_metrics(innings)
    assert (metrics["hundreds"], metrics["double_centuries"], metrics["triple_centuries"]) == (3, 2, 1)


def _write_csvs(tmp_path, match, batting_rows, bowling_rows):
    paths = []
    bat_cols = ["Player Name", "Team Name", "Runs", "Balls", "1s", "2s", "3s", "Fours",
                "Sixes", "Dots", "Status", "Bowler Out", "Fielder Out", "Match Format"]
    bowl_cols = ["Bowler Name", "Team Name", "Overs", "Maidens", "Runs", "Wickets",
                 "Wides", "No Balls", "Byes", "Leg Byes", "Match Format"]
    for kind, cols, rows in (("batting", bat_cols, batting_rows), ("bowling", bowl_cols, bowling_rows)):
        frame = pd.DataFrame([{**dict.fromkeys(cols, 0), "Match Format": "FC", **row} for row in rows], columns=cols)
        path = tmp_path / f"{match}_user_Team_innings1_{kind}.csv"
        frame.to_csv(path, index=False)
        paths.append(str(path))
    return paths


def test_aggregator_milestones_match_the_service(tmp_path, monkeypatch):
    """The CSV aggregator counted a five-for as a four-for as well, and had
    the same 200s/300s gap."""
    monkeypatch.chdir(tmp_path)
    paths = []
    for index, (runs, wickets) in enumerate([(250, 4), (310, 5), (120, 6)]):
        paths += _write_csvs(
            tmp_path, f"match{index}",
            [{"Player Name": "Big Bat", "Team Name": "A", "Runs": runs, "Balls": 300, "Status": "Caught"}],
            [{"Bowler Name": "Quick", "Team Name": "B", "Overs": 20.0, "Runs": 60, "Wickets": wickets}],
        )
    aggregator = StatsAggregator(paths, "review")
    batting = aggregator._calculate_batting_stats().iloc[0]
    bowling = aggregator._calculate_bowling_stats().iloc[0]
    assert (batting["Matches"], batting["100s"], batting["200s"], batting["300s"]) == (3, 3, 2, 1)
    assert (bowling["Matches"], bowling["4w"], bowling["5w"]) == (3, 1, 2)


def test_aggregator_credits_stumpings_and_ignores_did_not_bat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = _write_csvs(
        tmp_path, "match0",
        [
            {"Player Name": "Opener", "Team Name": "A", "Runs": 12, "Balls": 9,
             "Status": "Stumped", "Bowler Out": "Spinner", "Fielder Out": "Gloves"},
            # The archiver writes the rest of the XI with empty cells.
            {"Player Name": "Number 11", "Team Name": "A", "Runs": None, "Balls": None, "Status": None},
        ],
        [{"Bowler Name": "Spinner", "Team Name": "B", "Overs": 4.0, "Runs": 20, "Wickets": 1}],
    )
    paths += _write_csvs(
        tmp_path.joinpath("b").mkdir() or tmp_path / "b", "match0",
        [{"Player Name": "Gloves", "Team Name": "B", "Runs": 3, "Balls": 5, "Status": "not out"}],
        [{"Bowler Name": "Opener", "Team Name": "A", "Overs": 1.0, "Runs": 4, "Wickets": 0}],
    )
    batting = StatsAggregator(paths, "review")._calculate_batting_stats().set_index("Player")
    assert batting.loc["Gloves", "Stumpings"] == 1
    assert batting.loc["Number 11", "Innings"] == 0


# --------------------------------------------------------------------------
# 7. Wicketless best figures
# --------------------------------------------------------------------------

def test_wicketless_best_figures_are_shown_everywhere(league):
    """The main table printed '-' for a best of 0/15 while the profile and the
    comparison printed 0/15."""
    match = add_match(league, date=datetime(2026, 11, 1))
    bowl(match, league["bowler"], 0, 15, balls=12)
    db.session.commit()

    service = StatsService()
    row = service.get_overall_stats(league["user"].id, "T20")["bowling"][0]
    profile = service.get_player_profile(league["bowler"].id, league["user"].id)["bowling"]
    assert row["best"] == profile["best_figures"] == "0/15"
    assert row["best_match"] == "0/15"


# --------------------------------------------------------------------------
# 8. Team stats and Conditions Lens
# --------------------------------------------------------------------------

def test_team_stats_ignore_abandoned_matches_and_unknown_toss(league):
    home, away = league["home"], league["away"]
    add_match(league, date=datetime(2026, 11, 1), winner=home, home=(180, 4), away=(150, 9),
              toss=home, decision="Bat")
    add_match(league, date=datetime(2026, 11, 2), winner=away, home=(120, 10), away=(121, 3),
              toss=away, decision="Bowl")
    # Washed out: counts as played / NR, nothing else.
    add_match(league, date=datetime(2026, 11, 3), status="no_result", home=(0, 0), away=(0, 0),
              toss=home, decision="Bat")
    # Legacy row with no toss: a result, but not attributable to either split.
    add_match(league, date=datetime(2026, 11, 4), winner=home, home=(200, 5), away=(190, 7))
    db.session.commit()

    stats = StatsService().get_team_stats(league["user"].id, home.id, "T20")
    summary = stats["summary"]
    assert (summary["played"], summary["won"], summary["lost"], summary["no_result"]) == (4, 2, 1, 1)
    assert summary["win_pct"] == round(2 * 100 / 3, 1)
    assert summary["avg_scored"] == round((180 + 120 + 200) / 3, 1)
    assert summary["avg_conceded"] == round((150 + 121 + 190) / 3, 1)
    assert (stats["batting_first"]["played"], stats["batting_first"]["won"]) == (2, 1)
    assert stats["chasing"]["played"] == 0
    assert [row["result"] for row in stats["recent"]] == ["W", "NR", "L", "W"]


def test_team_stats_average_fc_scores_per_innings(league):
    add_match(league, date=datetime(2026, 11, 1), fmt="FC", status="drawn",
              home=(400, 10), away=(300, 10),
              home_team_score_innings2=200, home_team_wickets_innings2=4)
    db.session.commit()
    summary = StatsService().get_team_stats(league["user"].id, league["home"].id, "FC")["summary"]
    # Two innings of 400 and 200, not one "600".
    assert summary["avg_scored"] == 300.0
    assert summary["avg_conceded"] == 300.0


def test_conditions_lens_is_per_innings_and_skips_abandoned(league):
    add_match(league, date=datetime(2026, 11, 1), home=(180, 4), away=(160, 10))
    add_match(league, date=datetime(2026, 11, 2), status="aborted", home=(0, 0), away=(0, 0))
    db.session.commit()

    venue = StatsService().get_insights(league["user"].id, match_format="T20")["conditions"]["venues"][0]
    assert venue == {"label": "Review Oval", "avg_runs": 170.0, "avg_wkts": 7.0, "matches": 1, "innings": 2}


# --------------------------------------------------------------------------
# 9. Statistics page: leaderboard subtitle, exports, table behaviour
# --------------------------------------------------------------------------

def test_strike_rate_threshold_is_reported_to_the_page(league):
    """The card always said 'Min 50 balls faced' whatever cut-off was used."""
    match = add_match(league, date=datetime(2026, 11, 1))
    bat(match, league["batter"], 30, balls=24)
    bat(match, league["keeper"], 20, balls=16)
    db.session.commit()

    boards = StatsService().get_overall_stats(league["user"].id, "T20")["leaderboards"]
    assert boards["sr_min_balls"] == 12  # half the median of 24, floor of 10
    assert all(row["sr"] for row in boards["highest_sr"])

    source = (ROOT / "templates" / "statistics.html").read_text(encoding="utf-8")
    assert "Min 50 balls faced" not in source
    assert "leaderboards.sr_min_balls" in source


def test_bowling_exports_lead_with_player_like_the_table():
    service = StatsService()
    row = {
        "team": "Home XI", "player": "S. Seamer", "matches": 1, "innings": 1, "overs": 4.0,
        "runs": 20, "wickets": 2, "best": "2/20", "average": 10.0, "economy": 5.0,
        "dots": 10, "bowled": 1, "lbw": 0, "byes": 0, "leg_byes": 0, "wides": 1, "no_balls": 0,
    }
    header, first = service.export_to_csv([row], "bowling", "T20").splitlines()[:2]
    assert header.startswith("player,team,") and first.startswith("S. Seamer,Home XI,")
    text = service.export_to_txt([row], "bowling", "T20")
    assert text.index("Player") < text.index("Team")
    assert text.index("S. Seamer") < text.index("Home XI")


def test_statistics_table_script_contracts():
    source = (ROOT / "templates" / "statistics.html").read_text(encoding="utf-8")
    # Hundred-only columns were not sortable.
    for call in ("handleSort('batting-table',14)", "handleSort('batting-table',15)",
                 "handleSort('bowling-table',13)", "handleSort('bowling-table',16)"):
        assert call in source
    assert "<th>HS</th>" not in source and "<th>Dot %</th>" not in source
    sort_body = source.split("function handleSort(", 1)[1].split("// ===== PAGINATION", 1)[0]
    # '-' cells sort last in both directions, ahead of any numeric compare.
    assert sort_body.index("aMissing") < sort_body.index("parseFloat(at)")
    kpi_body = source.split("function computeKPIs()", 1)[1].split("// ===== PARTNERSHIPS", 1)[0]
    assert ".split(' ')[0]" not in kpi_body
    assert "batting innings" in kpi_body


def test_head_to_head_offers_the_hundred():
    source = (ROOT / "templates" / "head_to_head.html").read_text(encoding="utf-8")
    assert '<option value="Hundred">' in source
