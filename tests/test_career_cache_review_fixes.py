"""
Regression tests for the Nov 2026 review of the Player career-aggregate cache.

  1. Career best bowling: a wicketless spell must replace the 0/0 column
     default, and an unused bowler's empty row must never outrank a real spell.
  2. Reversal recomputes high-water marks without Hundred / super-over rows.
  3. Catch milestones ignore Hundred matches, which share the T20 Player rows
     but stay out of their career aggregates.
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db
from database.models import Match as DBMatch, MatchScorecard, Player as DBPlayer
from engine.stats_service import StatsService
from match_archiver import reverse_player_aggregates, _recompute_best_bowling
from tests.test_super_over_career_stats import _make_match, _archive


def _db_match(user_id, team, match_format="T20"):
    m = DBMatch(id=str(uuid.uuid4()), user_id=user_id, home_team_id=team.id,
                match_format=match_format)
    db.session.add(m)
    db.session.flush()
    return m


def _bowl(match, player, team, wickets, runs, balls, super_over=False):
    card = MatchScorecard(
        match_id=match.id, player_id=player.id, team_id=team.id,
        innings_number=3 if super_over else 1, record_type="bowling",
        is_super_over=super_over, wickets=wickets, runs_conceded=runs,
        balls_bowled=balls, maidens=0,
    )
    db.session.add(card)
    return card


def _bat(match, player, team, runs, catches=0, super_over=False):
    card = MatchScorecard(
        match_id=match.id, player_id=player.id, team_id=team.id,
        innings_number=3 if super_over else 1, record_type="batting",
        is_super_over=super_over, runs=runs, balls=runs, fours=0, sixes=0,
        is_out=True, catches=catches,
    )
    db.session.add(card)
    return card


def _player(team):
    return DBPlayer.query.filter_by(team_id=team.id).first()


def test_wicketless_spell_becomes_best_bowling_on_archive(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_match(regular_user.id)
        match.super_over_career_batting = {"home": {}, "away": {}}
        match.super_over_career_bowling = {"home": {}, "away": {}}
        match.first_innings_bowling_stats = {
            "Champion 2": {"balls_bowled": 24, "runs": 25, "wickets": 0, "maidens": 0},
        }
        assert _archive(match) is not False

        bowler = DBPlayer.query.filter_by(name="Champion 2").first()
        assert bowler.total_runs_conceded == 25, "spell was not archived"
        assert (bowler.best_bowling_wickets, bowler.best_bowling_runs) == (0, 25)


def test_unused_bowler_row_does_not_outrank_real_spell(app, regular_user, test_team):
    with app.app_context():
        p = _player(test_team)
        m1 = _db_match(regular_user.id, test_team)
        m2 = _db_match(regular_user.id, test_team)
        _bowl(m1, p, test_team, wickets=0, runs=18, balls=12)
        _bowl(m2, p, test_team, wickets=0, runs=0, balls=0)   # in the XI, never bowled
        db.session.flush()

        _recompute_best_bowling(p)
        assert (p.best_bowling_wickets, p.best_bowling_runs) == (0, 18)


def test_reversal_ignores_hundred_super_over_and_empty_rows(app, regular_user, test_team):
    with app.app_context():
        p = _player(test_team)
        keep = _db_match(regular_user.id, test_team)
        hundred = _db_match(regular_user.id, test_team, match_format="Hundred")
        gone = _db_match(regular_user.id, test_team)

        _bowl(keep, p, test_team, wickets=1, runs=30, balls=24)
        _bat(keep, p, test_team, runs=40)
        _bowl(keep, p, test_team, wickets=2, runs=3, balls=6, super_over=True)
        _bat(keep, p, test_team, runs=60, super_over=True)
        _bowl(hundred, p, test_team, wickets=4, runs=10, balls=20)
        _bat(hundred, p, test_team, runs=90)
        removed = [
            _bowl(gone, p, test_team, wickets=5, runs=20, balls=24),
            _bat(gone, p, test_team, runs=120),
        ]
        p.matches_played = 2
        p.total_runs, p.total_wickets = 220, 8
        p.highest_score, p.best_bowling_wickets, p.best_bowling_runs = 120, 5, 20
        p.total_centuries, p.five_wicket_hauls = 1, 1
        db.session.flush()

        reverse_player_aggregates(removed)

        assert p.highest_score == 40
        assert (p.best_bowling_wickets, p.best_bowling_runs) == (1, 30)


def test_catch_milestone_ignores_hundred_matches(app, regular_user, test_team):
    with app.app_context():
        p = _player(test_team)
        t20 = _db_match(regular_user.id, test_team)
        hundred = _db_match(regular_user.id, test_team, match_format="Hundred")
        _bat(t20, p, test_team, runs=10, catches=8)
        _bat(hundred, p, test_team, runs=10, catches=2)
        db.session.flush()

        # 8 T20 catches + 2 Hundred catches is not 10 career (T20) catches.
        assert StatsService.detect_milestones(p.id, deltas={"runs": 0, "wickets": 0, "catches": 1}) == []

        t20b = _db_match(regular_user.id, test_team)
        _bat(t20b, p, test_team, runs=10, catches=2)
        db.session.flush()
        ms = StatsService.detect_milestones(p.id, deltas={"runs": 0, "wickets": 0, "catches": 2})
        assert ms == [f"{p.name} reached 10 career catches!"]
