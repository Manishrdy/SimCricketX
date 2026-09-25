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
