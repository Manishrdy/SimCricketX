"""Hundred rules and state contracts, with no live DB/archive writes."""
import copy
import json
from collections import Counter
import pytest
import engine.match as module
from engine.format_config import get_format
from engine.hundred_bowler_manager import HundredBowlerManager
from engine.hundred_snapshot import serialize, restore
from engine.dls import ResourceLedger, resources_remaining
from tests.test_bowler_consecutive_guards import _build_match_data, _build_team


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(module, 'print', lambda *a, **kw: None)
    monkeypatch.setattr(module.Match, '_create_match_archive', lambda self: True)


def make(**kwargs):
    data = _build_match_data('Hundred', 5)
    data.update(kwargs)
    return module.Match(data)


def dot(**kw):
    return dict(runs=0, batter_out=False, is_extra=False, description='Dot')


@pytest.mark.parametrize('sets,pp', zip(range(5,21), [6,8,9,10,11,13,14,15,16,18,19,20,21,23,24,25]))
def test_reduced_rules_and_adversarial_allocation(sets, pp):
    fmt = get_format('Hundred'); fmt.revise_short_innings(sets)
    assert fmt.powerplay_balls == pp
    assert fmt.is_powerplay((pp-1)/5)
    assert not fmt.is_powerplay(pp/5)
    manager = HundredBowlerManager(_build_team('A'), fmt)
    manager.validate_attack()
    sequence=[]
    for i in range(sets):
        pool=manager.get_eligible_bowlers(i, sets-i)
        assert pool
        p=max(pool,key=lambda p:manager.overs_bowled(p['name']))
        sequence.append(p['name']);manager.record_over_completion(p['name'],0)
    assert all(not(a==b==c) for a,b,c in zip(sequence,sequence[1:],sequence[2:]))
    assert max(Counter(sequence).values()) <= fmt.max_bowler_overs


def test_end_changes_not_every_set(monkeypatch):
    m=make();monkeypatch.setattr(module,'calculate_outcome',dot)
    striker=m.current_striker['name']
    for _ in range(5):m.next_ball()
    assert m.current_striker['name']==striker
    for _ in range(5):m.next_ball()
    assert m.current_striker['name']!=striker
    assert m.hundred_state()['legal_balls']==10


def test_manual_continue_across_end(monkeypatch):
    m=make(simulation_mode='manual');m.current_over=2
    a,b=m.bowling_team[:2]
    a,b=[p for p in m.bowling_team if p['will_bowl']][:2]
    m.bowler_manager.record_over_completion(a['name'],0)
    m.bowler_manager.record_over_completion(b['name'],0)
    m.current_bowler=b
    options=m._create_next_bowler_decision()['options']
    option=next(p for p in options if p['name']==b['name'])
    assert option['continue_bowler']
    assert m.submit_pending_decision(option['index'])[1]==200
    m.bowler_manager.record_over_completion(b['name'],0);m.current_over=3
    assert b['name'] not in [p['name'] for _,p in m._get_manual_bowler_candidates()]


def test_full_match_extras_tie_and_stats(monkeypatch):
    m=make(); calls=0
    def outcome(**kw):
        nonlocal calls
        calls+=1
        if calls in (1,107):return dict(runs=1,batter_out=False,is_extra=True,extra_type='Wide',description='Wide')
        return dot()
    monkeypatch.setattr(module,'calculate_outcome',outcome)
    for _ in range(220):
        r=m.next_ball(); assert not r.get('error'),r
        if r.get('match_over'):break
    else:pytest.fail('did not finish')
    assert r['match_tied'] and not r.get('super_over_required')
    for stats in (m.first_innings_bowling_stats,m.bowler_stats):
        assert sum(s['balls_bowled'] for s in stats.values())==100
        assert max(s['balls_bowled'] for s in stats.values())<=20
    assert r['scorecard_data']['overs']=='100/100 balls'


@pytest.mark.parametrize('sets',range(5,21))
def test_rain_and_checkpoint(sets,monkeypatch):
    m=make(weather_script={'forecast':'rain_around','events':[{'at_global_over':0,'overs_lost':20-sets}]})
    monkeypatch.setattr(module,'calculate_outcome',dot)
    for _ in range(7):m.next_ball()
    n=make();restore(n,json.loads(json.dumps(serialize(m))))
    assert n.batting_team is n.home_xi or n.batting_team is n.away_xi
    assert n.bowler_history is n.bowler_manager._quota
    for _ in range(210):
        a,b=m.next_ball(),n.next_ball()
        assert a==b
        if a.get('match_over'):break
    else:pytest.fail('rain match did not finish')
    assert n.fmt.overs==sets


def test_dls_units_and_recovery():
    ledger=ResourceLedger(20,5)
    assert ledger.available()==resources_remaining(100/6,0)
    ledger.record_interruption(15,2,10)
    assert ResourceLedger.from_dict(ledger.to_dict()).available()==ledger.available()


def test_timeout_checkpoint(monkeypatch):
    m=make(simulation_mode='manual');m.current_over=5
    m.set_strategic_timeout(True)
    n=make();restore(n,json.loads(json.dumps(serialize(m))))
    assert n.next_ball()['timeout_active']
    n.set_strategic_timeout(False)
    with pytest.raises(ValueError):n.set_strategic_timeout(True)


def test_random_resume():
    m=make()
    for _ in range(14):m.next_ball()
    n=make();restore(n,json.loads(json.dumps(serialize(m))))
    for _ in range(15):
        a,b=m.next_ball(),n.next_ball()
        assert (a.get('score'),a.get('wickets'),a.get('over'),a.get('ball'))==(b.get('score'),b.get('wickets'),b.get('over'),b.get('ball'))

@pytest.mark.parametrize('pitch',['Green','Dry','Hard','Flat','Dead'])
@pytest.mark.parametrize('night',[False,True])
def test_seeded_full_matches(pitch,night):
    import random
    for seed in range(5):
        random.seed(seed)
        m=make(pitch=pitch,is_day_night=night)
        for _ in range(350):
            response=m.next_ball()
            assert not response.get('error'), response
            if response.get('match_over'):break
        else:pytest.fail('Hundred did not finish')
        for stats in (m.first_innings_bowling_stats,m.bowler_stats):
            assert sum(s['balls_bowled'] for s in stats.values())<=100
            assert max(s['balls_bowled'] for s in stats.values())<=20

@pytest.mark.parametrize('at,lost',[(3,8),(7,8),(11,5),(15,2),(22,7),(27,7),(32,4),(37,2)])
def test_mid_innings_rain_remains_completion_safe(at,lost,monkeypatch):
    monkeypatch.setattr(module,'calculate_outcome',dot)
    m=make(weather_script={'forecast':'rain_around','events':[{'at_global_over':at,'overs_lost':lost}]})
    for _ in range(240):
        r=m.next_ball()
        assert not r.get('error'),r
        if r.get('match_over'):break
    else:pytest.fail('rain allocation deadlock')


def test_repeated_super_fives_no_countback(monkeypatch):
    monkeypatch.setattr(module,'calculate_outcome',dot)
    monkeypatch.setattr(module,'calculate_super_over_outcome',dot)
    m=make(is_knockout=True)
    for _ in range(210):
        r=m.next_ball()
        if r.get('super_over_required'):break
    assert r.get('super_over_required')
    for round_number in range(1,8):
        r=m.start_super_over(m._super_over_next_first_batting)
        assert not r.get('error'),r
        for _ in range(6):r=m.next_super_over_ball()
        assert m.super_over_phase=='awaiting_innings2_selection'
        r=m.start_super_over_innings2()
        assert not r.get('error'),r
        for _ in range(6):r=m.next_super_over_ball()
        assert m.super_over_phase=='awaiting_innings1_selection',r
        assert m.super_over_round==round_number
        assert not r.get('match_over')


def test_hundred_dew_fixed_ball_clock():
    from engine.ball_outcome import _apply_hundred_dew
    weights={'Extras':1.,'Wicket':1.,'Four':1.,'Dot':1.}
    assert _apply_hundred_dew(weights,2,90,False)==weights
    assert _apply_hundred_dew(weights,1,90,True)==weights
    assert _apply_hundred_dew(weights,2,50,True)==weights
    peak=_apply_hundred_dew(weights,2,90,True)
    assert peak['Extras']==pytest.approx(1.2)
    assert peak['Wicket']==pytest.approx(.925)
    assert peak['Four']==pytest.approx(1.05)
    assert _apply_hundred_dew(weights,2,70,True)['Extras']==pytest.approx(1.1)
    assert _apply_hundred_dew(weights,2,99,True)==peak
