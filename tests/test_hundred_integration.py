"""Public Hundred workflows use shared QA identities and existing T20 profiles."""
import json
from pathlib import Path
from datetime import datetime
import pytest
from database import db
from database.models import Team, TeamProfile, Player, Match, MatchScorecard, TournamentTeam
from engine.stats_service import StatsService
from engine.tour_engine import create_tour
from engine.tournament_engine import TournamentEngine
from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS


@pytest.fixture
def hundred_qa(app):
    seed(app)
    owner=ACCOUNTS['user1']['email']
    teams=[]
    for side in ('HQA','HQB'):
        team=Team(name='[QA] '+side,short_code=side,user_id=owner,is_draft=False,is_placeholder=False)
        db.session.add(team);db.session.flush()
        profile=TeamProfile(team_id=team.id,format_type='T20');db.session.add(profile);db.session.flush()
        for i in range(11):
            db.session.add(Player(team_id=team.id,profile_id=profile.id,name=f'{side} Player {i}',
                role='Wicketkeeper' if i==0 else 'All-rounder',is_captain=i==0,is_wicketkeeper=i==0,
                batting_rating=70,bowling_rating=70,fielding_rating=70,bowling_type='Fast',batting_hand='Right',bowling_hand='Right'))
        teams.append(team)
    db.session.commit()
    return logged_in_client(app,'user1'),owner,teams


def test_setup_live_restore_and_no_squad_creation(app,hundred_qa):
    from app import MATCH_INSTANCES
    client,owner,teams=hundred_qa
    response=client.post('/match/setup',json=dict(team_home=teams[0].id,team_away=teams[1].id,
        match_format='Hundred',pitch='Hard',stadium='QA Ground',toss='Heads',
        toss_winner=teams[0].short_code,toss_decision='Bat'))
    assert response.status_code==200,response.json
    mid=response.json['match_id']
    for _ in range(7):
        response=client.post(f'/match/{mid}/next-ball')
        assert response.status_code==200,response.json
    assert response.json['balls_per_over']==5
    old=MATCH_INSTANCES.pop(mid)
    live=client.get(f'/match/{mid}/live-state')
    assert live.status_code==200,live.json
    assert live.json['legal_balls']==old.current_over*5+old.current_ball
    assert TeamProfile.query.filter_by(format_type='Hundred').count()==0
    assert client.get('/statistics?match_format=Hundred').status_code==200
    assert client.get('/match/setup').status_code==200


def test_stats_isolation_exports_and_tour(hundred_qa):
    client,owner,teams=hundred_qa
    player=teams[0].profiles[0].players[0]
    for fmt,runs in [('T20',99),('Hundred',20)]:
        match=Match(id='qa-'+fmt,user_id=owner,home_team_id=teams[0].id,away_team_id=teams[1].id,
                    match_format=fmt,date=datetime.utcnow())
        db.session.add(match);db.session.flush()
        db.session.add(MatchScorecard(match_id=match.id,player_id=player.id,team_id=teams[0].id,
            record_type='bowling',balls_bowled=20,runs_conceded=runs,wickets=4,dot_balls_bowled=10))
    db.session.commit()
    service=StatsService()
    h=service.get_overall_stats(owner,'Hundred')['bowling'][0]
    assert h['runs']==20 and h['economy']==100 and h['balls']==20
    assert h['four_wicket_hauls']==1 and h['dot_percentage']==50
    assert service.get_overall_stats(owner,'T20')['bowling'][0]['runs']==99
    csv=service.export_to_csv([h],'bowling','Hundred')
    assert 'economy_unit' in csv and 'runs/100 balls' in csv
    tour=create_tour('[QA] Hundred tour',owner,teams[0].id,teams[1].id,{'Hundred':1},['Hundred'])
    assert tour.series[0].format_type=='Hundred'


def test_complete_match_archive_and_readonly_scorecard(app,hundred_qa,monkeypatch):
    import engine.match as engine
    from app import MATCH_INSTANCES
    client,owner,teams=hundred_qa
    monkeypatch.setattr(engine,'calculate_outcome',lambda **kw: dict(runs=1,batter_out=False,is_extra=False,description='Single'))
    response=client.post('/match/setup',json=dict(team_home=teams[0].id,team_away=teams[1].id,
        match_format='Hundred',pitch='Hard',stadium='QA Ground',toss='Heads',
        toss_winner=teams[0].short_code,toss_decision='Bat'))
    mid=response.json['match_id']
    instance=MATCH_INSTANCES.get(mid)
    if instance is None:
        client.post(f'/match/{mid}/next-ball')
        instance=MATCH_INSTANCES[mid]
    for _ in range(198):
        instance.next_ball()
    for _ in range(15):
        r=client.post(f'/match/{mid}/next-ball')
        assert r.status_code==200,r.json
        if r.json.get('match_over'):break
    else:pytest.fail('match did not complete')
    saved=db.session.get(Match,mid)
    assert saved and saved.match_format=='Hundred'
    assert saved.format_metadata['legal_balls']==[100,100]
    assert MatchScorecard.query.filter_by(match_id=mid,record_type='bowling').count()==10
    assert client.get(f'/match/{mid}/scoreboard').status_code==200
    assert client.get('/my-matches?match_format=Hundred').status_code==200


def test_hundred_points_nrr_reversal(hundred_qa):
    client,owner,teams=hundred_qa
    engine=TournamentEngine()
    tournament=engine.create_tournament('[QA] Hundred league',owner,[t.id for t in teams],format_type='Hundred')
    fixture=tournament.fixtures[0]
    match=Match(id='qa-standings',user_id=owner,tournament_id=tournament.id,match_format='Hundred',
        home_team_id=fixture.home_team_id,away_team_id=fixture.away_team_id,
        winner_team_id=fixture.home_team_id,home_team_score=140,away_team_score=130,
        home_team_wickets=10,away_team_wickets=9,home_team_overs='18.0',away_team_overs='20.0',
        result_description='Home won by 10 runs',format_metadata={'nrr':{'home':{'runs':140,'balls':100},'away':{'runs':130,'balls':100}}})
    db.session.add(match);fixture.match_id=match.id;db.session.commit()
    assert engine.update_standings(match)
    home=TournamentTeam.query.filter_by(tournament_id=tournament.id,team_id=fixture.home_team_id).one()
    assert home.points==4 and home.net_run_rate==.5
    assert engine.reverse_standings(match)
    assert home.points==0 and home.runs_scored==0 and home.net_run_rate==0


def test_two_tied_super_fives_archive_league_winner(app, hundred_qa, monkeypatch):
    import engine.match as engine
    from app import MATCH_INSTANCES
    from database.models import TournamentFixture

    client, owner, teams = hundred_qa
    tournament = TournamentEngine().create_tournament(
        '[QA] Hundred tie-break', owner, [t.id for t in teams], format_type='Hundred')
    for fixture in tournament.fixtures:
        fixture.status = 'Completed'
        fixture.winner_team_id = teams[1].id
    rows = TournamentTeam.query.filter_by(tournament_id=tournament.id).all()
    for row in rows:
        row.played = 1
        row.points = 4 if row.team_id == teams[1].id else 0
    final = TournamentFixture(tournament_id=tournament.id, home_team_id=teams[0].id,
        away_team_id=teams[1].id, stage='final', round_number=2, status='Scheduled')
    db.session.add(final)
    db.session.commit()
    monkeypatch.setattr(engine, 'calculate_outcome', lambda **kw: dict(
        runs=0, batter_out=False, is_extra=False, description='Dot'))
    monkeypatch.setattr(engine, 'calculate_super_over_outcome', lambda **kw: dict(
        runs=0, batter_out=False, is_extra=False, description='Dot'))
    response = client.post('/match/setup', json=dict(
        team_home=teams[0].id, team_away=teams[1].id, match_format='Hundred',
        fixture_id=final.id, tournament_id=tournament.id,
        pitch='Hard', stadium='QA Ground', toss='Heads',
        toss_winner=teams[0].short_code, toss_decision='Bat',
        hundred_repeat_super_fives=True,
        hundred_knockout={'stage': 'final', 'home_position': 1, 'away_position': 2}))
    assert response.status_code == 200, response.json
    mid = response.json['match_id']
    client.post(f'/match/{mid}/next-ball')
    m = MATCH_INSTANCES[mid]
    assert m.data['hundred_knockout'] == dict(stage='final', home_position=2, away_position=1)
    assert m.data['hundred_repeat_super_fives'] is False
    # Simulate a legacy snapshot without ranking metadata; route restore backfills it.
    m.data.pop('hundred_knockout')
    # The live accessor also upgrades already-cached legacy instances.
    assert client.get(f'/match/{mid}/live-state').status_code == 200
    # live-state may use a separate accessor; the Super Five routes always restore.
    for _ in range(210):
        result = m.next_ball()
        if result.get('super_over_required'):
            break
    for round_number in (1, 2):
        response = client.post(f'/match/{mid}/start-super-over', json={'first_batting_team': 'home'})
        assert response.status_code == 200, response.json
        m = MATCH_INSTANCES[mid]
        assert m.data['hundred_knockout']['away_position'] == 1
        for _ in range(6):
            response = client.post(f'/match/{mid}/next-super-over-ball')
            assert not response.json.get('error'), response.json
        assert client.post(f'/match/{mid}/start-super-over-innings2', json={}).status_code == 200
        for _ in range(6):
            response = client.post(f'/match/{mid}/next-super-over-ball')
            assert not response.json.get('error'), response.json
        if round_number == 1:
            # Later table edits cannot change this match's saved tie-breaker.
            for row in rows:
                row.points = 8 if row.team_id == teams[0].id else 0
            db.session.commit()
            MATCH_INSTANCES.pop(mid)
    assert response.json['match_over']
    saved = db.session.get(Match, mid)
    assert saved.winner_team_id == teams[1].id
    assert saved.margin_type == 'league_pos' and saved.margin_value is None
    assert 'awarded the trophy' in saved.result_description
    db.session.refresh(final)
    assert final.status == 'Completed' and final.winner_team_id == teams[1].id
    assert client.get(f'/match/{mid}/scoreboard').status_code == 200


def test_pure_knockout_setup_selects_repeat_policy(app, hundred_qa):
    from app import MATCH_INSTANCES
    client, owner, teams = hundred_qa
    tournament = TournamentEngine().create_tournament(
        '[QA] Hundred pure knockout', owner, [t.id for t in teams],
        mode='knockout', format_type='Hundred')
    fixture = tournament.fixtures[0]
    response = client.post('/match/setup', json=dict(
        team_home=fixture.home_team_id, team_away=fixture.away_team_id,
        match_format='Hundred', fixture_id=fixture.id, tournament_id=tournament.id,
        pitch='Hard', stadium='QA Ground', toss='Heads',
        toss_winner=fixture.home_team.short_code, toss_decision='Bat'))
    assert response.status_code == 200, response.json
    mid = response.json['match_id']
    assert client.post(f'/match/{mid}/next-ball').status_code == 200
    assert MATCH_INSTANCES[mid].data['hundred_repeat_super_fives'] is True
    assert MATCH_INSTANCES[mid].data['hundred_knockout'] is None


def _completed_hundred_league(owner, teams, stage):
    from database.models import TournamentFixture
    tournament = TournamentEngine().create_tournament(
        '[QA] Hundred abandonment', owner, [t.id for t in teams], format_type='Hundred')
    for fixture in tournament.fixtures:
        fixture.status = 'Completed'
        fixture.winner_team_id = teams[1].id
    for row in TournamentTeam.query.filter_by(tournament_id=tournament.id):
        row.played = 1
        row.points = 4 if row.team_id == teams[1].id else 0
    fixture = TournamentFixture(tournament_id=tournament.id, home_team_id=teams[0].id,
        away_team_id=teams[1].id, stage=stage, round_number=2, status='Scheduled')
    db.session.add(fixture)
    db.session.commit()
    return tournament, fixture


@pytest.mark.parametrize('stage', ['eliminator', 'final'])
def test_abandoned_hundred_knockout_completes_via_live_route(app, hundred_qa, monkeypatch, stage):
    import engine.weather as weather
    client, owner, teams = hundred_qa
    tournament, fixture = _completed_hundred_league(owner, teams, stage)
    monkeypatch.setattr(weather, 'generate_weather_script', lambda *a, **kw: {
        'forecast': 'rain_around', 'events': [{'at_global_over': 0, 'overs_lost': 19}]})
    response = client.post('/match/setup', json=dict(
        team_home=fixture.home_team_id, team_away=fixture.away_team_id,
        match_format='Hundred', fixture_id=fixture.id, tournament_id=tournament.id,
        pitch='Hard', stadium='QA Ground', toss='Heads',
        toss_winner=teams[0].short_code, toss_decision='Bat'))
    assert response.status_code == 200, response.json
    mid = response.json['match_id']
    response = client.post(f'/match/{mid}/next-ball')
    assert response.status_code == 200 and response.json['match_over'], response.json
    saved = db.session.get(Match, mid)
    assert saved.winner_team_id == teams[1].id and saved.margin_type == 'league_pos'
    assert saved.match_status == 'no_result'
    assert 'after abandonment' in saved.result_description
    db.session.refresh(fixture)
    assert fixture.status == 'Completed' and fixture.winner_team_id == teams[1].id
    assert fixture.standings_applied
    assert client.get(f'/match/{mid}/scoreboard').status_code == 200
    rows = TournamentTeam.query.filter_by(tournament_id=tournament.id).all()
    assert sorted(row.points for row in rows) == [0, 4]
    assert all(row.played == 1 for row in rows)
    assert not TournamentEngine().update_standings(saved)


@pytest.mark.parametrize('saved_context', [False, True])
def test_abandoned_hundred_repair_resolves_without_live_engine(hundred_qa, saved_context):
    client, owner, teams = hundred_qa
    tournament, fixture = _completed_hundred_league(owner, teams, 'final')
    # A frozen position takes precedence over subsequently edited standings.
    metadata = {'knockout': dict(stage='final', home_position=1, away_position=2)} if saved_context else {}
    match = Match(id='qa-abandoned-repair', user_id=owner, tournament_id=tournament.id,
        home_team_id=teams[0].id, away_team_id=teams[1].id, match_format='Hundred',
        match_status='no_result', result_description='Match abandoned due to rain. No result.',
        format_metadata=metadata)
    db.session.add(match)
    db.session.flush()
    fixture.match_id = match.id
    db.session.commit()
    engine = TournamentEngine()
    assert engine.update_standings(match)
    winner = teams[0].id if saved_context else teams[1].id
    assert match.winner_team_id == winner and fixture.winner_team_id == winner
    assert fixture.status == 'Completed' and fixture.standings_applied
    assert match.margin_type == 'league_pos' and match.match_status == 'no_result'
    assert not engine.update_standings(match)


def test_hundred_abandonment_preserves_pure_knockout_replay(hundred_qa):
    client, owner, teams = hundred_qa
    engine = TournamentEngine()
    tournament = engine.create_tournament('[QA] Pure knockout washout', owner,
        [t.id for t in teams], mode='knockout', format_type='Hundred')
    fixture = tournament.fixtures[0]
    match = Match(id='qa-pure-washout', user_id=owner, tournament_id=tournament.id,
        home_team_id=fixture.home_team_id, away_team_id=fixture.away_team_id,
        match_format='Hundred', match_status='no_result')
    db.session.add(match)
    db.session.flush()
    fixture.match_id = match.id
    db.session.commit()
    assert engine.update_standings(match)
    assert match.winner_team_id is None and fixture.winner_team_id is None
    assert fixture.status == 'Scheduled' and not fixture.standings_applied


def test_hundred_abandoned_eliminator_advances_bracket(hundred_qa):
    client, owner, teams = hundred_qa
    for code in ('HQC', 'HQD'):
        team = Team(name='[QA] ' + code, short_code=code, user_id=owner,
                    is_draft=False, is_placeholder=False)
        db.session.add(team)
        teams.append(team)
    db.session.commit()
    engine = TournamentEngine()
    tournament = engine.create_tournament('[QA] Playoff washout', owner,
        [t.id for t in teams], mode='ipl_style', format_type='Hundred')
    for fixture in tournament.fixtures:
        if fixture.stage == 'league':
            fixture.status = 'Completed'
            fixture.winner_team_id = fixture.home_team_id
    for index, team in enumerate(teams):
        row = TournamentTeam.query.filter_by(tournament_id=tournament.id, team_id=team.id).one()
        row.points = 16 - 4 * index
        row.played = 6
    db.session.commit()
    engine.check_and_progress_tournament(tournament.id)
    fixtures = {f.stage: f for f in tournament.fixtures if f.stage != 'league'}
    q1, elim, q2 = fixtures['qualifier_1'], fixtures['eliminator'], fixtures['qualifier_2']
    q1.status = 'Completed'
    q1.winner_team_id = q1.home_team_id
    match = Match(id='qa-eliminator-washout', user_id=owner, tournament_id=tournament.id,
        home_team_id=elim.home_team_id, away_team_id=elim.away_team_id,
        match_format='Hundred', match_status='no_result')
    db.session.add(match)
    db.session.flush()
    elim.match_id = match.id
    db.session.commit()
    assert engine.update_standings(match)
    assert elim.status == 'Completed' and elim.winner_team_id == teams[2].id
    assert q2.status == 'Scheduled'
    assert q2.away_team_id == teams[2].id
