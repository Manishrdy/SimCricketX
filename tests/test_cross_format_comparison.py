"""Cross-format identity isolation, cricket arithmetic and evidence safeguards."""
from datetime import datetime, timedelta

import pytest
from database import db
from database.models import Player, Team, MasterPlayer, UserPlayer, Match, MatchScorecard, Tournament
from engine.stats_service import StatsService
from engine.player_comparison import add_insights


@pytest.fixture
def comparison_data(app, regular_user, admin_user):
    team = Team(name='Home', short_code='HOM', user_id=regular_user.id)
    other_team = Team(name='Away', short_code='AWY', user_id=regular_user.id)
    foreign_team = Team(name='Private', short_code='PRI', user_id=admin_user.id)
    master = MasterPlayer(name='Linked')
    db.session.add_all([team, other_team, foreign_team, master]); db.session.flush()
    override = UserPlayer(user_id=regular_user.id, name='Linked override', master_player_id=master.id)
    custom = UserPlayer(user_id=regular_user.id, name='Custom')
    db.session.add_all([override, custom]); db.session.flush()
    players = [Player(name='Linked T20', team_id=team.id, master_player_id=master.id),
               Player(name='Linked FC', team_id=other_team.id, user_player_id=override.id),
               Player(name='Custom', team_id=team.id, user_player_id=custom.id),
               Player(name='Namesake', team_id=team.id), Player(name='Namesake', team_id=other_team.id),
               Player(name='Private', team_id=foreign_team.id, master_player_id=master.id)]
    db.session.add_all(players); db.session.flush()
    tournament = Tournament(name='First class', user_id=regular_user.id, format_type='FC')
    db.session.add(tournament); db.session.flush()
    for n in range(12):
        match = Match(id=f'match-{n}', user_id=regular_user.id, match_format='T20', date=datetime(2026,1,1)+timedelta(days=n))
        db.session.add(match); db.session.flush()
        db.session.add(MatchScorecard(match_id=match.id, player_id=players[0].id, team_id=team.id,
                                     runs=n+1, balls=20, fours=0, sixes=0, is_out=n%2==0))
        db.session.add(MatchScorecard(match_id=match.id, player_id=players[2].id, team_id=team.id,
                                     record_type='bowling', balls_bowled=7, runs_conceded=7, wickets=0))
    fc = Match(id='fc', user_id=regular_user.id, match_format='FC', tournament_id=tournament.id, stats_incomplete=True)
    foreign = Match(id='private', user_id=admin_user.id, match_format='T20')
    db.session.add_all([fc, foreign]); db.session.flush()
    for innings in (1,3):
        db.session.add(MatchScorecard(match_id='fc', player_id=players[1].id, team_id=other_team.id,
                                     innings_number=innings, runs=200, balls=250, fours=20, sixes=5, is_out=True))
        db.session.add(MatchScorecard(match_id='fc', player_id=players[1].id, team_id=other_team.id,
                                     innings_number=innings, record_type='bowling', balls_bowled=61, runs_conceded=20, wickets=5))
    db.session.add_all([
        MatchScorecard(match_id='match-0', player_id=players[0].id, team_id=team.id, runs=999, is_super_over=True),
        MatchScorecard(match_id='private', player_id=players[0].id, team_id=team.id, runs=999),
        MatchScorecard(match_id='match-0', player_id=players[5].id, team_id=foreign_team.id, runs=999),
    ])
    db.session.commit()
    return {'ids':[f'master:{master.id}', f'user:{custom.id}'], 'players':players, 'tournament':tournament}


def test_identity_links_and_namesakes(comparison_data, regular_user):
    groups = StatsService().comparison_identities(regular_user.id)
    assert len(groups) == 4
    linked = next(g for g in groups if g['id'] == comparison_data['ids'][0])
    assert linked['player_ids'] == [p.id for p in comparison_data['players'][:2]]
    assert linked['teams'] == ['Home','Away']
    namesakes = [g for g in groups if g['name']=='Namesake']
    assert len(namesakes)==2 and all(not g['linked'] for g in namesakes)


def test_weighted_metrics_recent_fc_and_missing(comparison_data, regular_user):
    data = StatsService().compare_players_cross_format(regular_user.id, comparison_data['ids'])
    linked, custom = data['players']
    t20 = linked['formats']['T20']
    assert t20['batting']['runs'] == 78  # Foreign-user and super-over rows excluded.
    assert t20['batting']['average'] == 13
    assert t20['batting']['strike_rate'] == 32.5
    assert [d['runs'] for d in t20['recent']['batting']] == list(range(3,13))
    assert t20['recent']['batting'][-1]['is_out'] is False
    fc = linked['formats']['FC']
    assert fc['matches']==1 and fc['batting']['innings']==2
    assert fc['batting']['double_centuries']==2
    assert fc['bowling']['ten_wicket_matches']==1
    assert fc['bowling']['best_match_figures']=='10/40'
    assert fc['bowling']['overs']=='20.2'
    assert fc['bowling']['economy']==1.97
    assert fc['composition']=={'fours':160,'sixes':60,'other':180,'boundary_percentage':55.0}
    assert fc['incomplete'] and fc['insights']=={'strengths':[],'weaknesses':[]}
    assert linked['formats']['ListA']['has_data'] is False
    assert linked['formats']['ListA']['composition'] is None
    bowling = custom['formats']['T20']['bowling']
    assert bowling['economy']==6 and bowling['average'] is None and bowling['strike_rate'] is None


def test_tournament_and_rejected_selections(comparison_data, regular_user):
    service=StatsService(); ids=comparison_data['ids']
    data=service.compare_players_cross_format(regular_user.id, ids, comparison_data['tournament'].id)
    assert data['formats']==['FC'] and data['players'][1]['formats']['FC']['matches']==0
    for invalid in ([ids[0]], [ids[0],ids[0]], [ids[0],'player:999999'], ids*4):
        with pytest.raises(ValueError): service.compare_players_cross_format(regular_user.id, invalid)
    with pytest.raises(ValueError): service.compare_players_cross_format(regular_user.id,ids,999999)


def insight_players(values, discipline='batting', metric='strike_rate'):
    players=[]
    for i,value in enumerate(values):
        stats={'innings':5,'balls':120,'not_outs':0,'wickets':5,metric:value}
        players.append({'id':str(i),'formats':{'T20':{'batting':{},'bowling':{},discipline:stats,
            'incomplete':False,'insights':{'strengths':[],'weaknesses':[]}}}})
    return players


@pytest.mark.parametrize('discipline,metric,values,strong_index',[
    ('batting','strike_rate',[80,120],1),('batting','average',[20,40],1),
    ('bowling','economy',[6,9],0),('bowling','strike_rate',[15,30],0),('bowling','average',[20,40],0)])
def test_insight_direction_and_evidence(discipline,metric,values,strong_index):
    players=insight_players(values,discipline,metric);add_insights(players,['T20'])
    evidence=players[strong_index]['formats']['T20']['insights']['strengths'][0]
    assert evidence['value']==values[strong_index] and evidence['eligible_players']==2
    assert evidence['median']==sum(values)/2 and evidence['balls']==120
    assert players[1-strong_index]['formats']['T20']['insights']['weaknesses']


@pytest.mark.parametrize('values',[[100,100],[99,101],[0,0],[80,80,120,120]])
def test_ties_small_differences_and_zero_median(values):
    players=insight_players(values);add_insights(players,['T20'])
    assert all(not p['formats']['T20']['insights']['strengths'] and not p['formats']['T20']['insights']['weaknesses'] for p in players)


@pytest.mark.parametrize('change',[{'innings':4},{'balls':99},{'not_outs':3}])
def test_batting_average_eligibility(change):
    players=insight_players([20,40],'batting','average')
    players[0]['formats']['T20']['batting'].update(change);add_insights(players,['T20'])
    assert all(not p['formats']['T20']['insights']['strengths'] for p in players)


def test_incomplete_and_wicket_threshold():
    for change in ('incomplete','wickets','balls'):
        players=insight_players([20,40],'bowling','average')
        data=players[0]['formats']['T20']
        if change=='incomplete':data['incomplete']=True
        else:data['bowling'][change]=4 if change=='wickets' else 119
        add_insights(players,['T20'])
        assert all(not p['formats']['T20']['insights']['strengths'] for p in players)


def test_new_api_and_legacy_contract(authenticated_client, comparison_data):
    client=authenticated_client
    response=client.get('/api/compare-players',query_string={'mode':'cross-format','identity_ids':','.join(comparison_data['ids'])})
    assert response.status_code==200 and response.json['data']['formats']==['T20','T10','ListA','FC','Hundred']
    for query in ({'identity_ids':''},{'identity_ids':','.join(comparison_data['ids']),'tournament_id':'oops'}):
        assert client.get('/api/compare-players',query_string={'mode':'cross-format',**query}).status_code==400
    legacy=client.get('/api/compare-players',query_string={'player_ids':','.join(str(p.id) for p in comparison_data['players'][:2]),'match_format':'T20'})
    assert legacy.status_code==200 and isinstance(legacy.json['data'],list)
    page=client.get('/compare-players')
    assert page.status_code==200 and b'player_comparison.js' in page.data
