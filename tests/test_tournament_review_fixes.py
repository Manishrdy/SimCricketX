"""Regression tests for the Nov 2026 tournaments review.

1. Points-table runs left out extras (and disagreed with what re-simulate
   later subtracted).
2. Hundred innings lengths reached the points table as 6-ball overs read
   back as 5-ball sets.
3. Re-simulate refused any fixture whose next-round slot the bracket had
   auto-advanced as a bye.
4. A knockout draw could pair two byes, creating a team-less phantom match.
"""

from tests.match_store_helpers import instances as MATCH_INSTANCES, lock as MATCH_INSTANCES_LOCK
import random

import pytest

from database import db
from database.models import (
    Team, TeamProfile, Player, Match, MatchScorecard, TournamentTeam,
    TournamentFixture,
)
from engine.tournament_engine import TournamentEngine
from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS
from utils.fixture_replay import impact, execute, preview_token


def _teams(owner, count):
    teams = []
    for k in range(count):
        team = Team(name=f'[QA] Review {k}', short_code=f'RV{k}', user_id=owner,
                    is_draft=False, is_placeholder=False, venue_mode='legacy',
                    home_ground='QA ground', pitch_preference='Hard')
        db.session.add(team)
        db.session.flush()
        profile = TeamProfile(team_id=team.id, format_type='T20')
        db.session.add(profile)
        db.session.flush()
        for i in range(11):
            db.session.add(Player(
                team_id=team.id, profile_id=profile.id, name=f'RV{k} Player {i}',
                role='Wicketkeeper' if i == 0 else 'All-rounder',
                is_captain=i == 0, is_wicketkeeper=i == 0,
                batting_rating=70, bowling_rating=70, fielding_rating=70,
                bowling_type='Fast', batting_hand='Right', bowling_hand='Right'))
        teams.append(team)
    db.session.commit()
    return teams


@pytest.fixture
def qa(app):
    seed(app)
    return logged_in_client(app, 'user1'), ACCOUNTS['user1']['email']


def _record(engine, fixture, owner, tag):
    """Complete a Scheduled fixture the way the live completion path does."""
    match = Match(
        id=f'review-{tag}-{fixture.id}', user_id=owner, tournament_id=fixture.tournament_id,
        home_team_id=fixture.home_team_id, away_team_id=fixture.away_team_id,
        match_format='T20', match_status='completed', winner_team_id=fixture.home_team_id,
        result_description='QA result', home_team_score=150, away_team_score=120,
        home_team_overs='20.0', away_team_overs='20.0',
        home_team_wickets=5, away_team_wickets=10)
    db.session.add(match)
    db.session.flush()
    fixture.match_id = match.id
    db.session.commit()
    engine.update_standings(match, commit=True)
    player = Player.query.filter_by(team_id=fixture.home_team_id).first()
    db.session.add(MatchScorecard(match_id=match.id, player_id=player.id, team_id=player.team_id,
                                  innings_number=1, record_type='batting', runs=10, balls=5, is_out=True))
    db.session.commit()
    return match


@pytest.mark.parametrize('match_format', ['Hundred', 'T20'])
def test_points_table_matches_the_recorded_scorecard(app, qa, match_format):
    """Whatever the points table adds must be what the saved match holds:
    full innings totals (extras included) and, for the Hundred, the NRR
    balls from its own metadata."""
    import app as app_module
    client, owner = qa
    teams = _teams(owner, 2)
    tournament = TournamentEngine().create_tournament(
        '[QA] Review league', owner, [t.id for t in teams], format_type=match_format)
    fixture = tournament.fixtures[0]
    response = client.post('/match/setup', json=dict(
        team_home=fixture.home_team_id, team_away=fixture.away_team_id,
        match_format=match_format, fixture_id=fixture.id, tournament_id=tournament.id,
        pitch='Hard', stadium='QA Ground', toss='Heads',
        toss_winner=teams[0].short_code, toss_decision='Bat'))
    assert response.status_code == 200, response.json
    match_id = response.json['match_id']
    for _ in range(800):
        app_module._rate_limit_store.clear()
        response = client.post(f'/match/{match_id}/next-ball')
        assert response.status_code == 200, response.json
        if response.json.get('match_over'):
            break
    else:
        pytest.fail('match did not finish')

    db.session.expire_all()
    match = db.session.get(Match, match_id)
    if match.match_status == 'no_result':
        pytest.skip('rained off; nothing reaches the NRR columns')
    rows = {r.team_id: r for r in TournamentTeam.query.filter_by(tournament_id=tournament.id)}
    home, away = rows[match.home_team_id], rows[match.away_team_id]

    if match_format == 'Hundred':
        nrr = match.format_metadata['nrr']
        engine = TournamentEngine()
        assert (home.runs_scored, away.runs_scored) == (nrr['home']['runs'], nrr['away']['runs'])
        assert engine.overs_to_balls(home.overs_faced, 5) == nrr['home']['balls']
        assert engine.overs_to_balls(away.overs_faced, 5) == nrr['away']['balls']
    else:
        assert (home.runs_scored, away.runs_scored) == (match.home_team_score, match.away_team_score)
    assert (home.runs_conceded, away.runs_conceded) == (away.runs_scored, home.runs_scored)

    # Re-simulate subtracts from the saved match, so it must land on zero
    # without leaning on the max(0, ...) floor: add a second identical
    # contribution first and check exactly one comes back off.
    before = (home.runs_scored, home.overs_faced, home.runs_conceded, home.overs_bowled)
    engine = TournamentEngine()
    fixture = db.session.get(TournamentFixture, fixture.id)
    fixture.standings_applied = False
    engine.update_standings(match, commit=True)
    engine.reverse_standings(match, commit=True)
    db.session.refresh(home)
    assert (home.runs_scored, home.overs_faced, home.runs_conceded, home.overs_bowled) == before


@pytest.mark.parametrize('team_count', [3, 5, 6, 7, 9, 11, 13])
def test_knockout_draw_never_pairs_two_byes(app, qa, team_count):
    _, owner = qa
    teams = _teams(owner, team_count)
    engine = TournamentEngine()
    for draw in range(25):
        random.seed(draw)
        tournament = engine.create_tournament(
            f'[QA] Draw {draw}', owner, [t.id for t in teams], mode='knockout')
        slots = engine._next_power_of_two(team_count)
        round1 = [f for f in tournament.fixtures if f.round_number == 1]
        assert len(round1) == slots // 2
        assert not [f for f in round1 if f.home_team_id is None or f.away_team_id is None]
        # Every bye is one real team given a free pass; the rest are real matches.
        assert sum(f.status == 'Completed' for f in round1) == slots - team_count
        assert sum(f.status == 'Scheduled' for f in round1) == team_count - slots // 2
        drawn = [t for f in round1 for t in (f.home_team_id, f.away_team_id)]
        assert {t.id for t in teams} <= set(drawn)
        # No later round is decided before a ball is bowled.
        assert all(f.winner_team_id is None for f in tournament.fixtures if f.round_number > 1)


def test_replay_reaches_past_an_auto_advanced_bye(app, qa, monkeypatch):
    """Brackets drawn before the bye fix can hold a phantom next to a real
    match; the slot they feed is then auto-completed as a bye with no match.
    Re-simulating the real match must reset that slot, not be refused."""
    _, owner = qa
    teams = _teams(owner, 5)
    ids = [t.id for t in teams]
    engine = TournamentEngine()
    # (a v b) real, phantom, (c v d) real, (e v bye)
    monkeypatch.setattr(engine, '_seed_knockout_slots', lambda team_ids, next_power: [
        ids[0], ids[1], None, None, ids[2], ids[3], ids[4], None])
    tournament = engine.create_tournament('[QA] Legacy bracket', owner, ids, mode='knockout')
    by_pos = {f.bracket_position: f for f in tournament.fixtures}
    target, other, bye_slot, final = by_pos[0], by_pos[2], by_pos[4], by_pos[6]

    _record(engine, target, owner, 'first')
    _record(engine, other, owner, 'first')
    db.session.expire_all()
    assert (bye_slot.status, bye_slot.match_id, bye_slot.away_team_id) == ('Completed', None, None)
    assert bye_slot.winner_team_id == ids[0]

    plan = impact(engine, target, owner)
    assert [row['id'] for row in plan['fixtures']] == [target.id, bye_slot.id, final.id]
    _, changed = execute(engine, target.id, owner, preview_token(plan, owner))
    assert changed
    db.session.expire_all()
    assert target.status == 'Scheduled' and target.match_id is None
    assert bye_slot.status == 'Locked' and bye_slot.winner_team_id is None

    # Replayed with the other side winning: the bye re-resolves for them.
    replay = Match(
        id='review-second', user_id=owner, tournament_id=tournament.id,
        home_team_id=target.home_team_id, away_team_id=target.away_team_id,
        match_format='T20', match_status='completed', winner_team_id=ids[1],
        result_description='QA replay')
    db.session.add(replay)
    db.session.flush()
    target.match_id = replay.id
    db.session.commit()
    engine.update_standings(replay, commit=True)
    db.session.expire_all()
    assert (bye_slot.status, bye_slot.winner_team_id) == ('Completed', ids[1])


# ── Second batch: findings fixed from reading the code ────────────────────

def _setup(client, fixture, teams, match_format='T20'):
    response = client.post('/match/setup', json=dict(
        team_home=fixture.home_team_id, team_away=fixture.away_team_id,
        match_format=match_format, fixture_id=fixture.id, tournament_id=fixture.tournament_id,
        pitch='Hard', stadium='QA Ground', toss='Heads',
        toss_winner=teams[0].short_code, toss_decision='Bat'))
    assert response.status_code == 200, response.json
    return response.json['match_id']


def _play_out(client, match_id):
    import app as app_module
    for _ in range(800):
        app_module._rate_limit_store.clear()
        response = client.post(f'/match/{match_id}/next-ball')
        assert response.status_code == 200, response.json
        if response.json.get('match_over'):
            return
    pytest.fail('match did not finish')


def _washout(monkeypatch):
    import engine.weather as weather
    monkeypatch.setattr(weather, 'generate_weather_script', lambda *a, **kw: {
        'forecast': 'rain_around', 'events': [{'at_global_over': 0, 'overs_lost': 19}]})


def test_rain_cut_all_out_is_charged_the_allotted_overs(app, qa):
    """A side bowled out in a chase rain had cut to 12 overs faced 12, not 20;
    the side batting first is credited with the DLS par score off those 12."""
    _, owner = qa
    teams = _teams(owner, 2)
    engine = TournamentEngine()
    tournament = engine.create_tournament('[QA] Rain league', owner, [t.id for t in teams])
    fixture = tournament.fixtures[0]
    match = Match(
        id='review-rain', user_id=owner, tournament_id=tournament.id,
        home_team_id=fixture.home_team_id, away_team_id=fixture.away_team_id,
        match_format='T20', overs_per_side=20, match_status='completed',
        winner_team_id=fixture.home_team_id, result_description='Home won (DLS method)',
        home_team_score=160, home_team_wickets=6, home_team_overs='20.0',
        away_team_score=80, away_team_wickets=10, away_team_overs='10.2',
        format_metadata={'nrr': {'home': {'runs': 101, 'balls': 72},
                                 'away': {'runs': 80, 'balls': 72}}})
    db.session.add(match)
    db.session.flush()
    fixture.match_id = match.id
    db.session.commit()
    assert engine.update_standings(match)
    rows = {r.team_id: r for r in TournamentTeam.query.filter_by(tournament_id=tournament.id)}
    home, away = rows[fixture.home_team_id], rows[fixture.away_team_id]
    assert (away.runs_scored, away.overs_faced) == (80, '12.0')
    assert (home.runs_scored, home.overs_faced) == (101, '12.0')
    assert home.net_run_rate == pytest.approx((101 - 80) / 12, abs=1e-6)
    assert engine.reverse_standings(match, commit=True)
    assert (home.runs_scored, home.overs_faced, away.overs_bowled) == (0, '0.0', '0.0')


def test_live_rain_reduced_chase_records_nrr_metadata(app, qa, monkeypatch):
    import engine.weather as weather
    client, owner = qa
    teams = _teams(owner, 2)
    tournament = TournamentEngine().create_tournament('[QA] Rain live', owner, [t.id for t in teams])
    fixture = tournament.fixtures[0]
    monkeypatch.setattr(weather, 'generate_weather_script', lambda *a, **kw: {
        'forecast': 'rain_around', 'events': [{'at_global_over': 23, 'overs_lost': 7}]})
    match_id = _setup(client, fixture, teams)
    _play_out(client, match_id)
    db.session.expire_all()
    match = db.session.get(Match, match_id)
    if match.match_status == 'no_result' or not (match.format_metadata or {}).get('rain_events'):
        pytest.skip('the scripted shower did not reduce this chase')
    nrr = match.format_metadata['nrr']
    chase_balls = match.format_metadata['chase_overs'] * 6
    assert chase_balls < 120
    assert match.scheduled_overs == 20
    assert match.overs_per_side == match.format_metadata['chase_overs']
    assert all(side['balls'] <= chase_balls for side in nrr.values())
    engine = TournamentEngine()
    rows = {r.team_id: r for r in TournamentTeam.query.filter_by(tournament_id=tournament.id)}
    home, away = rows[match.home_team_id], rows[match.away_team_id]
    assert (home.runs_scored, away.runs_scored) == (nrr['home']['runs'], nrr['away']['runs'])
    assert engine.overs_to_balls(home.overs_faced) == nrr['home']['balls']
    assert engine.overs_to_balls(away.overs_faced) == nrr['away']['balls']
    assert engine.reverse_standings(match, commit=True)
    db.session.refresh(home)
    db.session.refresh(away)
    assert (home.runs_scored, home.overs_faced, away.runs_scored, away.overs_faced) == (0, '0.0', 0, '0.0')


def test_replaying_a_winnerless_knockout_match_removes_the_first(app, qa, monkeypatch):
    client, owner = qa
    teams = _teams(owner, 2)
    tournament = TournamentEngine().create_tournament(
        '[QA] Washed-out final', owner, [t.id for t in teams], mode='knockout')
    fixture = tournament.fixtures[0]

    with monkeypatch.context() as patch:
        _washout(patch)
        first_id = _setup(client, fixture, teams)
        _play_out(client, first_id)
    db.session.expire_all()
    first = db.session.get(Match, first_id)
    assert first is not None and first.winner_team_id is None
    assert (fixture.status, fixture.match_id) == ('Scheduled', first_id)

    second_id = _setup(client, fixture, teams)
    _play_out(client, second_id)
    db.session.expire_all()
    second = db.session.get(Match, second_id)
    if second.winner_team_id is None:
        pytest.skip('the replay had no winner either')
    assert db.session.get(Match, first_id) is None
    assert MatchScorecard.query.filter_by(match_id=first_id).count() == 0
    assert Match.query.filter_by(tournament_id=tournament.id).count() == 1
    assert (fixture.status, fixture.match_id) == ('Completed', second_id)


def test_deleting_a_tournament_discards_its_in_flight_match(app, qa):
    import os
    import app as app_module
    client, owner = qa
    teams = _teams(owner, 2)
    tournament = TournamentEngine().create_tournament('[QA] Deleted cup', owner, [t.id for t in teams])
    match_id = _setup(client, tournament.fixtures[0], teams)
    assert client.post(f'/match/{match_id}/next-ball').status_code == 200
    path = os.path.join(app_module.PROJECT_ROOT, 'data', 'matches', f'match_{match_id}.json')
    assert os.path.isfile(path)

    assert client.post(f'/tournaments/{tournament.id}/delete').status_code == 302
    assert db.session.get(type(tournament), tournament.id) is None
    assert not os.path.isfile(path)
    assert match_id not in MATCH_INSTANCES


def test_result_is_not_recorded_on_a_fixture_that_changed_hands(app, qa, monkeypatch):
    """Stands in for id reuse after a delete: the fixture id the match was
    set up with now names a different pairing."""
    client, owner = qa
    teams = _teams(owner, 3)
    tournament = TournamentEngine().create_tournament('[QA] Reused ids', owner, [t.id for t in teams])
    fixture = next(f for f in tournament.fixtures
                   if {f.home_team_id, f.away_team_id} == {teams[0].id, teams[1].id})
    _washout(monkeypatch)
    match_id = _setup(client, fixture, [db.session.get(Team, fixture.home_team_id)])
    fixture.away_team_id = teams[2].id
    fixture.active_match_id = None
    db.session.commit()

    _play_out(client, match_id)
    db.session.expire_all()
    assert db.session.get(Match, match_id) is None
    assert (fixture.status, fixture.match_id, fixture.standings_applied) == ('Scheduled', None, False)
    assert all(r.played == 0 for r in TournamentTeam.query.filter_by(tournament_id=tournament.id))


def test_create_tournament_rolls_back_before_logging(app, qa, monkeypatch):
    import engine.tournament_engine as module
    from database.models import Tournament
    _, owner = qa
    teams = _teams(owner, 2)
    seen = []
    monkeypatch.setattr(module, 'log_exception', lambda *a, **kw: seen.append(
        Tournament.query.filter_by(name='[QA] Bad series').count()))
    with pytest.raises(ValueError):
        TournamentEngine().create_tournament(
            '[QA] Bad series', owner, [t.id for t in teams], mode='custom_series',
            series_config={'matches': [{'home': 5}]})
    assert seen == [0], 'the failed tournament was still in the transaction when it was logged'


def test_tournament_dashboard_leaderboards_exclude_zero_runs_and_wickets(app, qa):
    client, owner = qa
    teams = _teams(owner, 2)
    engine = TournamentEngine()
    tournament = engine.create_tournament('[QA] Zero filter', owner, [t.id for t in teams])
    fixture = tournament.fixtures[0]
    match = Match(
        id='review-zero-leaders', user_id=owner, tournament_id=tournament.id,
        home_team_id=fixture.home_team_id, away_team_id=fixture.away_team_id,
        match_format='T20', match_status='completed', winner_team_id=fixture.home_team_id,
        result_description='QA win', home_team_score=10, away_team_score=0,
        home_team_overs='2.0', away_team_overs='2.0',
        home_team_wickets=0, away_team_wickets=1)
    db.session.add(match)
    db.session.flush()
    fixture.match_id = match.id
    home_players = Player.query.filter_by(team_id=fixture.home_team_id).order_by(Player.id).all()
    away_players = Player.query.filter_by(team_id=fixture.away_team_id).order_by(Player.id).all()
    # One scorer (10 runs) and ten 0-run rows; one wicket-taker (1 wkt) and ten 0-wicket rows.
    for idx, p in enumerate(home_players):
        db.session.add(MatchScorecard(
            match_id=match.id, player_id=p.id, team_id=p.team_id,
            innings_number=1, record_type='batting',
            runs=10 if idx == 0 else 0, balls=6 if idx == 0 else 0, is_out=False))
        db.session.add(MatchScorecard(
            match_id=match.id, player_id=p.id, team_id=p.team_id,
            innings_number=2, record_type='bowling',
            wickets=1 if idx == 0 else 0, balls_bowled=6 if idx == 0 else 0, runs_conceded=0))
    for p in away_players:
        db.session.add(MatchScorecard(
            match_id=match.id, player_id=p.id, team_id=p.team_id,
            innings_number=2, record_type='batting', runs=0, balls=0, is_out=False))
        db.session.add(MatchScorecard(
            match_id=match.id, player_id=p.id, team_id=p.team_id,
            innings_number=1, record_type='bowling', wickets=0, balls_bowled=0, runs_conceded=0))
    db.session.commit()

    page = client.get(f'/tournaments/{tournament.id}').get_data(as_text=True)
    assert home_players[0].name in page
    assert home_players[1].name not in page
    assert away_players[0].name not in page


def test_get_tournament_stats_enforces_tournament_ownership(app, qa):
    from engine.stats_service import StatsService
    _, owner = qa
    other_user = ACCOUNTS['user2']['email']
    teams = _teams(owner, 2)
    engine = TournamentEngine()
    tournament = engine.create_tournament('[QA] Private cup', owner, [t.id for t in teams])
    _record(engine, tournament.fixtures[0], owner, 'owned')

    service = StatsService()
    assert service.get_tournament_stats(owner, tournament.id, 'T20')['batting']
    assert service.get_tournament_stats(other_user, tournament.id, 'T20') == service._empty_stats()
    assert service.get_tournament_stats(owner, 999999, 'T20') == service._empty_stats()

