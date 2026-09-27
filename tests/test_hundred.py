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


@pytest.mark.parametrize("stage,positions", [("eliminator", (2,3)), ("final", (2,1)), ("semi_final", (4,1))])
def test_two_tied_super_fives_use_league_position(monkeypatch, stage, positions):
    monkeypatch.setattr(module,'calculate_outcome',dot)
    monkeypatch.setattr(module,'calculate_super_over_outcome',dot)
    m=make(is_knockout=True, hundred_knockout=dict(stage=stage, home_position=positions[0], away_position=positions[1]))
    for _ in range(210):
        r=m.next_ball()
        if r.get('super_over_required'):break
    assert r.get('super_over_required')
    for round_number in range(1,3):
        r=m.start_super_over(m._super_over_next_first_batting)
        assert not r.get('error'),r
        for _ in range(6):r=m.next_super_over_ball()
        assert m.super_over_phase=='awaiting_innings2_selection'
        r=m.start_super_over_innings2()
        assert not r.get('error'),r
        for _ in range(6):r=m.next_super_over_ball()
        assert m.super_over_round==round_number
        if round_number == 1:
            assert m.super_over_phase=='awaiting_innings1_selection',r
            assert not r.get('match_over')
            # Restart with the frozen league positions before the deciding round.
            restored = make()
            restore(restored, json.loads(json.dumps(serialize(m))))
            m = restored
            # Boundary count must not override league position.
            lower_side = "away" if positions[0] < positions[1] else "home"
            m.super_over_team_boundaries[lower_side] = 99
        else:
            assert m.super_over_phase == 'complete'
            assert r['match_over'] and r['super_over_complete']
            assert m.winner_is_home is (positions[0] < positions[1])
            assert m.margin_type == 'league_pos' and m.margin_value is None
            assert 'league position' in r['result']
            assert m.start_super_over('home').get('error')
            assert m.super_over_round == 2


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


@pytest.mark.parametrize('decisive_round', [1, 2])
def test_super_five_score_winner_precedes_league_position(monkeypatch, decisive_round):
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    m = make(is_knockout=True, hundred_knockout=dict(stage='final', home_position=1, away_position=2))
    for _ in range(210):
        if m.next_ball().get('super_over_required'):
            break
    def outcome(**kw):
        runs = int(m.super_over_round == decisive_round and m.super_over_batting_team is m.away_xi)
        return dict(runs=runs, batter_out=False, is_extra=False, description='Run' if runs else 'Dot')
    monkeypatch.setattr(module, 'calculate_super_over_outcome', outcome)
    for _ in range(decisive_round):
        assert not m.start_super_over(m._super_over_next_first_batting).get('error')
        for _ in range(6):
            r = m.next_super_over_ball()
            if r.get('super_over_innings_end'):
                break
        assert not m.start_super_over_innings2().get('error')
        for _ in range(6):
            r = m.next_super_over_ball()
            if r.get('match_over'):
                break
    assert r['match_over'] and m.winner_is_home is False
    assert m.margin_type == 'runs'


def test_missing_league_positions_cannot_guess_winner_or_double_count(monkeypatch):
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    monkeypatch.setattr(module, 'calculate_super_over_outcome', dot)
    m = make(is_knockout=True)
    for _ in range(210):
        if m.next_ball().get('super_over_required'):
            break
    for _ in range(2):
        m.start_super_over(m._super_over_next_first_batting)
        for _ in range(6):
            m.next_super_over_ball()
        m.start_super_over_innings2()
        for _ in range(6):
            r = m.next_super_over_ball()
    assert 'League positions' in r['error']
    before = copy.deepcopy(m.super_over_career_batting)
    assert m.next_super_over_ball()['error'] == r['error']
    assert m.super_over_career_batting == before
    m.data['hundred_knockout'] = dict(stage='final', home_position=2, away_position=1)
    assert m.next_super_over_ball()['match_over']
    assert m.winner_is_home is False


def test_pure_knockout_super_fives_can_repeat(monkeypatch):
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    monkeypatch.setattr(module, 'calculate_super_over_outcome', dot)
    m = make(is_knockout=True, hundred_repeat_super_fives=True)
    for _ in range(210):
        if m.next_ball().get('super_over_required'):
            break
    for round_number in range(1, 4):
        assert not m.start_super_over(m._super_over_next_first_batting).get('error')
        for _ in range(6):
            m.next_super_over_ball()
        assert not m.start_super_over_innings2().get('error')
        for _ in range(6):
            r = m.next_super_over_ball()
        assert r['super_over_tied_again'] and not r['match_over']
        assert m.super_over_round == round_number


@pytest.mark.parametrize('stage,positions', [('eliminator', (2, 3)), ('final', (2, 1))])
@pytest.mark.parametrize('at', [0, 22])
def test_abandoned_league_knockout_uses_position(monkeypatch, stage, positions, at):
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    m = make(is_knockout=True,
             hundred_knockout=dict(stage=stage, home_position=positions[0], away_position=positions[1]),
             weather_script={'forecast': 'rain_around', 'events': [{'at_global_over': at, 'overs_lost': 19}]})
    for _ in range(220):
        r = m.next_ball()
        if r.get('match_over'):
            break
    assert r['match_over'] and m.match_status == 'no_result'
    assert m.winner_is_home is (positions[0] < positions[1])
    assert m.margin_type == 'league_pos' and m.margin_value is None
    assert 'after abandonment' in r['result']
    assert r['scorecard_data']['target_info'] == r['result']
    assert m.hundred_archive_metadata()['knockout'] == m.data['hundred_knockout']


@pytest.mark.parametrize('knockout,repeat', [(False, False), (True, True)])
def test_abandonment_league_and_pure_knockout_keep_no_winner(monkeypatch, knockout, repeat):
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    m = make(is_knockout=knockout, hundred_repeat_super_fives=repeat,
             weather_script={'forecast': 'rain_around', 'events': [{'at_global_over': 0, 'overs_lost': 19}]})
    r = m.next_ball()
    assert r['match_over'] and m.match_status == 'no_result'
    assert m.winner_is_home is None and m.margin_type is None


@pytest.mark.parametrize('score,expected_margin', [(0, 'runs'), (1, 'league_pos'), (2, 'runs')])
def test_terminal_chase_only_uses_position_when_par_is_tied(monkeypatch, score, expected_margin):
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    m = make(is_knockout=True, hundred_knockout=dict(stage='final', home_position=2, away_position=1))
    for _ in range(110):
        m.next_ball()
        if m.innings == 2:
            break
    m.score, m.target = score, 2
    r = m._finalize_chase_terminated([])
    assert r['match_over'] and m.margin_type == expected_margin
    if score == 1:
        assert m.winner_is_home is False
        assert m.match_status == 'tied'
    else:
        expected_team = m.batting_team if score > 1 else m.bowling_team
        assert m.winner_is_home is (expected_team is m.home_xi)
        assert m.match_status == 'completed'
