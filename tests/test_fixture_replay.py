"""Replay safety using the shared QA accounts, isolated in pytest's local DB."""
import json
import uuid
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest

from database import db
from database.models import (Team, TeamProfile, Player, Tournament, TournamentTeam,
    TournamentFixture, Match, MatchScorecard, FixtureReplay, VoidedFixtureMatch)
from engine.tournament_engine import TournamentEngine
from scripts.dev_test_accounts import seed, logged_in_client
from utils.fixture_replay import impact, execute, preview_token, cleanup, ReplayError


@pytest.fixture
def qa(app):
    seed(app)
    client = logged_in_client(app, 'user1')
    with client.session_transaction() as session:
        user_id = session['_user_id']
    teams = []
    for n in range(4):
        team = Team(user_id=user_id, name=f'QA Replay {n}', short_code=f'RP{n}', is_draft=False)
        db.session.add(team); db.session.flush()
        for fmt in ('T20', 'T10', 'ListA', 'FC'):
            profile = TeamProfile(team_id=team.id, format_type=fmt)
            db.session.add(profile); db.session.flush()
            for i in range(11):
                db.session.add(Player(team_id=team.id, profile_id=profile.id, name=f'QA {n} {fmt} {i}',
                    role='Wicketkeeper' if i == 0 else 'All-rounder', is_captain=i == 0, is_wicketkeeper=i == 0))
        teams.append(team)
    db.session.commit()
    return SimpleNamespace(client=client, user=user_id, teams=teams, engine=TournamentEngine(), app=app)


def tournament(qa, mode='round_robin', fmt='T20'):
    t = Tournament(name='QA Replay Cup', user_id=qa.user, mode=mode, format_type=fmt)
    db.session.add(t); db.session.flush()
    for team in qa.teams:
        db.session.add(TournamentTeam(tournament_id=t.id, team_id=team.id))
    db.session.commit()
    return t


def fixture(qa, t, stage='league', pos=None, active=None):
    f = TournamentFixture(tournament_id=t.id, home_team_id=qa.teams[0].id,
        away_team_id=qa.teams[1].id, stage=stage, bracket_position=pos, status='Scheduled', active_match_id=active)
    db.session.add(f); db.session.commit()
    return f


def record(qa, f, runs=70, outcome='completed'):
    fmt = f.tournament.format_type
    m = Match(id=str(uuid.uuid4()), user_id=qa.user, tournament_id=f.tournament_id,
        home_team_id=f.home_team_id, away_team_id=f.away_team_id, match_format=fmt,
        match_status=outcome, winner_team_id=f.home_team_id if outcome == 'completed' else None,
        result_description='QA result', home_team_score=runs, away_team_score=50,
        home_team_overs='20.0', away_team_overs='20.0')
    db.session.add(m); db.session.flush()
    p = Player.query.join(TeamProfile).filter(Player.team_id==f.home_team_id,
        TeamProfile.format_type==('T20' if fmt=='Hundred' else fmt)).first()
    db.session.add(MatchScorecard(match_id=m.id, player_id=p.id, team_id=p.team_id,
        innings_number=1, record_type='batting', runs=runs, balls=40, is_out=True))
    if fmt != 'Hundred':
        p.total_runs += runs; p.matches_played += 1; p.highest_score=max(p.highest_score, runs)
        if 50 <= runs < 100: p.total_fifties += 1
    f.match_id=m.id; f.status='Completed'; f.winner_team_id=m.winner_team_id
    if f.stage == 'league':
        qa.engine.update_standings(m, commit=False)
    db.session.commit()
    return m,p


def token_for(qa, f):
    response=qa.client.get(f'/fixture/{f.id}/replay-preview')
    assert response.status_code==200, response.get_data(as_text=True)
    return response.get_json()['token']


def reset(qa, f, token=None):
    return qa.client.post(f'/fixture/{f.id}/resimulate', json={'token':token or token_for(qa,f)})


@pytest.mark.parametrize('fmt,outcome', [('T20','completed'),('T10','tied'),('ListA','no_result'),('FC','drawn'),('Hundred','completed')])
def test_single_result_reversed_and_retry_harmless(qa,fmt,outcome):
    t=tournament(qa,fmt=fmt); f=fixture(qa,t); m,p=record(qa,f,outcome=outcome)
    original=m.id; pid=p.id
    token=token_for(qa,f)
    assert db.session.get(Match,original)  # preview changes nothing
    r=reset(qa,f,token)
    assert r.status_code==200, r.get_json()
    assert f.status=='Scheduled' and f.match_id is None
    assert db.session.get(Match,original) is None
    assert db.session.get(VoidedFixtureMatch,original)
    assert db.session.get(Player,pid).total_runs==0
    assert db.session.get(Player,pid).highest_score==0
    assert db.session.get(Player,pid).matches_played==0
    points=TournamentTeam.query.filter_by(tournament_id=t.id,team_id=f.home_team_id).one()
    assert (points.played,points.points)==(0,0)
    new,_=record(qa,f,runs=90)
    r=reset(qa,f,token)
    assert r.status_code==200 and r.get_json()['already_reset']
    assert f.match_id==new.id and db.session.get(Match,new.id)
    assert FixtureReplay.query.count()==1


@pytest.mark.parametrize('stage,affected', [('qualifier_1',{1,3,4}),('eliminator',{2,3,4}),('qualifier_2',{3,4}),('final',{4})])
def test_ipl_exact_cascade_and_independent_result(qa,stage,affected):
    t=tournament(qa,mode='ipl_style'); fixtures=[]
    for pos,name in enumerate(['qualifier_1','eliminator','qualifier_2','final'],1):
        f=fixture(qa,t,name,pos); record(qa,f,runs=pos*20); fixtures.append(f)
    selected=next(f for f in fixtures if f.stage==stage)
    plan=impact(qa.engine,selected,qa.user)
    assert {row['id'] for row in plan['fixtures']}=={fixtures[i-1].id for i in affected}
    assert reset(qa,selected).status_code==200
    for i,f in enumerate(fixtures,1):
        assert (f.match_id is None)==(i in affected)
        if i in affected and f!=selected: assert f.status=='Locked'
    p=Player.query.filter_by(team_id=qa.teams[0].id,name='QA 0 T20 0').one()
    assert p.total_runs==sum(i*20 for i in range(1,5) if i not in affected)
    assert p.highest_score==max([i*20 for i in range(1,5) if i not in affected] or [0])


@pytest.mark.parametrize('mode',['round_robin_knockout','double_round_robin_knockout','ipl_style'])
def test_league_invalidates_playoffs_including_active_and_unplayed(qa,mode):
    t=tournament(qa,mode=mode); selected=fixture(qa,t);record(qa,selected)
    independent=fixture(qa,t); old,_=record(qa,independent,30)
    running=fixture(qa,t,'semifinal',1,active=str(uuid.uuid4()))
    active=running.active_match_id
    unplayed=fixture(qa,t,'final',3)
    plan=impact(qa.engine,selected,qa.user)
    assert [row['kind'] for row in plan['fixtures']]==['completed','in_progress','unplayed']
    assert reset(qa,selected).status_code==200
    assert running.status==unplayed.status=='Locked'
    assert running.active_match_id is None and db.session.get(VoidedFixtureMatch,active)
    assert independent.match_id==old.id


def test_stale_preview_and_missing_token_do_not_mutate(qa):
    t=tournament(qa); f=fixture(qa,t); m,_=record(qa,f)
    token=token_for(qa,f)
    f.active_match_id=str(uuid.uuid4());db.session.commit()
    assert reset(qa,f,token).status_code==409
    assert qa.client.post(f'/fixture/{f.id}/resimulate',json={}).status_code==409
    assert db.session.get(Match,m.id) and FixtureReplay.query.count()==0


def test_blockers_and_ownership(qa):
    t=tournament(qa);f=fixture(qa,t);m,_=record(qa,f)
    qa.teams[0].is_draft=True;db.session.commit()
    r=qa.client.get(f'/fixture/{f.id}/replay-preview')
    assert r.get_json()['blockers'][0]['url']
    assert reset(qa,f,r.get_json()['token']).status_code==409
    other=logged_in_client(qa.app,'user2')
    assert other.get(f'/fixture/{f.id}/replay-preview').status_code==404
    assert db.session.get(Match,m.id)


def test_database_failure_preserves_results_and_live_cache(qa,monkeypatch):
    t=tournament(qa);f=fixture(qa,t);m,p=record(qa,f);mid=m.id
    token=token_for(qa,f)
    def fail(*args,**kwargs): raise RuntimeError('injected rebuild failure')
    monkeypatch.setattr(TournamentEngine,'rebuild_player_stats_cache',fail)
    assert reset(qa,f,token).status_code==500
    assert db.session.get(Match,mid) and f.match_id==mid
    assert p.total_runs==70 and FixtureReplay.query.count()==0
    assert VoidedFixtureMatch.query.count()==0


def test_artifact_failure_is_retryable_and_legacy_files_removed(qa,tmp_path,monkeypatch):
    t=tournament(qa);f=fixture(qa,t);m,_=record(qa,f);mid=m.id
    token=token_for(qa,f)
    receipt,_=execute(qa.engine,f.id,qa.user,token)
    directory=tmp_path/'data'/'matches';directory.mkdir(parents=True, exist_ok=True)
    canonical=directory/f'match_{mid}.json';canonical.write_text(json.dumps({'match_id':mid}))
    legacy=directory/'legacy.json';legacy.write_text(json.dumps({'match_id':mid}))
    cache={mid:object()}
    unlink=Path.unlink
    def fail(self,*args,**kwargs):
        if self==canonical: raise PermissionError('injected file failure')
        return unlink(self,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',fail)
    assert not cleanup(receipt,tmp_path,cache,RLock())
    assert receipt.cleanup_pending and mid not in cache and canonical.exists()
    monkeypatch.setattr(Path,'unlink',unlink)
    assert cleanup(receipt,tmp_path,cache,RLock())
    assert not canonical.exists() and not legacy.exists() and not receipt.cleanup_pending


def test_invalidated_archiver_cannot_restore_old_result(qa):
    from match_archiver import MatchArchiver
    t=tournament(qa);f=fixture(qa,t);m,_=record(qa,f);mid=m.id
    assert reset(qa,f).status_code==200
    archiver=object.__new__(MatchArchiver)
    archiver.match_data={'tournament_id':t.id}
    archiver.match_id=mid
    archiver.logger=qa.app.logger
    assert archiver._save_to_database() is False
    assert db.session.get(Match,mid) is None


def test_migration_idempotent(qa):
    from migrations.add_fixture_replays import run_migration
    run_migration(db,qa.app);run_migration(db,qa.app)
    assert FixtureReplay.query.count()==0


@pytest.mark.parametrize('mode',['knockout','round_robin_knockout','double_round_robin_knockout'])
def test_binary_bracket_cascade_preserves_other_semifinal(qa,mode):
    t=tournament(qa,mode=mode)
    first=fixture(qa,t,'knockout_sf',0);record(qa,first)
    other=fixture(qa,t,'knockout_sf',1);other_match,_=record(qa,other)
    final=fixture(qa,t,'final',2);record(qa,final)
    assert {row['id'] for row in impact(qa.engine,first,qa.user)['fixtures']}=={first.id,final.id}
    assert reset(qa,first).status_code==200
    assert final.status=='Locked' and other.match_id==other_match.id


def test_preview_rejects_missing_and_incomplete_scorecards(qa):
    t=tournament(qa);f=fixture(qa,t);m,_=record(qa,f)
    m.stats_incomplete=True;db.session.commit()
    assert qa.client.get(f'/fixture/{f.id}/replay-preview').status_code==409
    m.stats_incomplete=False
    MatchScorecard.query.filter_by(match_id=m.id).delete();db.session.commit()
    assert qa.client.get(f'/fixture/{f.id}/replay-preview').status_code==409
    assert db.session.get(Match,m.id)


def test_completed_downstream_while_preview_open_requires_new_preview(qa):
    t=tournament(qa,mode='ipl_style')
    fixtures=[fixture(qa,t,stage,pos) for pos,stage in enumerate(['qualifier_1','eliminator','qualifier_2','final'],1)]
    selected=fixtures[0];m,_=record(qa,selected)
    token=token_for(qa,selected)
    record(qa,fixtures[2])
    response=reset(qa,selected,token)
    assert response.status_code==409 and 'changed' in response.get_json()['error']
    assert selected.match_id==m.id


def test_fc_second_innings_super_over_and_bowling_records_reverse(qa):
    t=tournament(qa,fmt='FC'); f=fixture(qa,t);m,p=record(qa,f)
    # The inverse must count this as one match even with several innings/record types.
    db.session.add_all([
        MatchScorecard(match_id=m.id,player_id=p.id,team_id=p.team_id,record_type='batting',innings_number=3,runs=110,balls=150,is_out=False),
        MatchScorecard(match_id=m.id,player_id=p.id,team_id=p.team_id,record_type='bowling',innings_number=2,wickets=5,balls_bowled=90,runs_conceded=30),
        MatchScorecard(match_id=m.id,player_id=p.id,team_id=p.team_id,record_type='batting',innings_number=5,is_super_over=True,runs=12,balls=6,is_out=False),
    ])
    p.total_runs+=122;p.total_centuries=1;p.not_outs=1;p.total_balls_faced=196
    p.highest_score=110;p.total_wickets=5;p.total_balls_bowled=90;p.total_runs_conceded=30
    p.five_wicket_hauls=1;p.best_bowling_wickets=5;p.best_bowling_runs=30
    db.session.commit()
    assert reset(qa,f).status_code==200
    assert (p.matches_played,p.total_runs,p.total_centuries,p.not_outs,p.highest_score,p.total_wickets,p.five_wicket_hauls,p.best_bowling_wickets)==(0,0,0,0,0,0,0,0)


def test_late_completion_and_resume_rejected(qa):
    import inspect
    from types import FunctionType
    def find(function,name,seen=None):
        seen=seen or set()
        if id(function) in seen: return None
        seen.add(id(function))
        if function.__name__==name:return function
        for value in inspect.getclosurevars(function).nonlocals.values():
            if isinstance(value,FunctionType):
                found=find(value,name,seen)
                if found:return found
    t=tournament(qa);f=fixture(qa,t);m,_=record(qa,f);mid=m.id
    assert reset(qa,f).status_code==200
    handler=find(qa.app.view_functions['next_ball'],'_handle_tournament_match_completion')
    assert handler
    stale=SimpleNamespace(data={'tournament_id':t.id,'fixture_id':f.id,'current_state':'live'})
    handler(stale,mid,{},qa.app.logger)
    assert stale.data['current_state']=='completed'
    assert f.match_id is None and db.session.get(Match,mid) is None
    response=qa.client.post(f'/match/{mid}/next-ball',json={})
    assert response.status_code==410


def test_two_concurrent_confirmations_commit_once(qa):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    t=tournament(qa);f=fixture(qa,t);m,_=record(qa,f)
    token=token_for(qa,f);fid=f.id;user=qa.user
    barrier=Barrier(2)
    def submit():
        with qa.app.app_context():
            barrier.wait(timeout=10)
            try:
                receipt,changed=execute(TournamentEngine(),fid,user,token)
                return changed
            finally:
                db.session.remove()
    db.session.remove()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:submit(),range(2)))
    assert sorted(results)==[False,True]
    assert FixtureReplay.query.count()==1
