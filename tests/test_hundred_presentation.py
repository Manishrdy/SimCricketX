"""Regression coverage for Hundred scoring, display, tactics and save contracts."""
import json
import re

import pytest
import engine.match as module
from engine.hundred_snapshot import restore, serialize
from tests.test_hundred import make, dot


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(module, 'print', lambda *a, **kw: None)
    monkeypatch.setattr(module.Match, '_create_match_archive', lambda self: True)


def delivery(runs=0, kind=None, wicket=None):
    return dict(runs=runs, batter_out=bool(wicket), is_extra=bool(kind),
                extra_type=kind, wicket_type=wicket, description=kind or wicket or 'Dot')


def scripted(monkeypatch, outcomes):
    stream = iter(outcomes)
    monkeypatch.setattr(module, 'calculate_outcome', lambda **kw: next(stream))


def test_starting_wides_preserve_runs_and_single_announcement(monkeypatch):
    m = make()
    scripted(monkeypatch, [delivery(1, 'Wide'), delivery(1, 'Wide')] + [dot()] * 5)
    responses = [m.next_ball() for _ in range(7)]
    assert [r['ball_data']['display_label'] for r in responses] == ['1 WD', '1 WD', '1', '2', '3', '4', '5']
    assert len({r['ball_data']['delivery_id'] for r in responses}) == 7
    assert sum('opens the bowling' in r['commentary'] for r in responses) == 1
    assert '2 runs, 0 wickets' in responses[-1]['commentary']
    assert 'CRR: 0.40 runs/ball' in responses[-1]['commentary']
    assert responses[-1]['ball_data']['legal_balls_after'] == 5
    assert not re.search(r'\b\d+\.\d+ .* to ', responses[-1]['commentary'])


@pytest.mark.parametrize('kind', ['Byes', 'Leg Bye'])
def test_extras_not_charged_to_bowler_or_batter(monkeypatch, kind):
    m = make()
    scripted(monkeypatch, [delivery(2, kind)])
    e = m.next_ball()['ball_data']
    assert e['is_legal'] and e['batter_balls'] == 1
    assert e['runs'] == 2 and e['bowler_runs'] == e['batting_runs'] == 0
    assert e['extras'] == {kind: 2}
    assert e['bowler_totals'][e['bowler']]['runs'] == 0


def test_runout_is_not_bowler_wicket(monkeypatch):
    m = make()
    scripted(monkeypatch, [delivery(1, wicket='Run Out')])
    e = m.next_ball()['ball_data']
    assert e['batter_out'] and not e['bowler_wicket']
    assert e['batting_runs'] == e['bowler_runs'] == e['runs'] == 1
    assert e['batter_balls'] == 1 and e['legal_balls_after'] == 1


def test_no_ball_batting_components_and_repeat_label(monkeypatch):
    m = make()
    # No-ball resolution performs a second batting-outcome call.
    scripted(monkeypatch, [delivery(1, 'No Ball'), delivery(4), dot()])
    a, b = m.next_ball(), m.next_ball()
    e = a['ball_data']
    assert e['runs'] == 5 and e['batting_runs'] == 4
    assert e['extras'] == {'No Ball': 1} and e['bowler_runs'] == 5
    assert e['batter_balls'] == 1 and e['batter_fours'] == 1
    assert e['legal_balls_after'] == 0 and b['ball_data']['ball_number'] == 1
    assert b['ball_data']['free_hit']


@pytest.mark.parametrize('terminal', ['wide', 'legal'])
def test_winning_delivery_finalized_before_archive(monkeypatch, terminal):
    m = make()
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    for _ in range(101):
        m.next_ball()
    assert m.innings == 2
    captured = []
    monkeypatch.setattr(m, '_create_match_archive', lambda: captured.append(m.hundred_archive_metadata()))
    scripted(monkeypatch, [delivery(1, 'Wide' if terminal == 'wide' else None)])
    r = m.next_ball()
    assert r['match_over']
    e = r['ball_data']
    assert e['innings'] == 2 and e['score'] == 1
    assert e['legal_balls_after'] == (0 if terminal == 'wide' else 1)
    assert captured[0]['deliveries'][-1] == e
    assert 'opens the bowling' in r['commentary']
    assert 'Change of ends' not in r['commentary']
    bowler = r['scorecard_data']['bowlers'][0]
    assert bowler['balls'] == e['legal_balls_after']
    assert bowler['rpb'] == ('—' if terminal == 'wide' else '1.00')


def test_all_out_ball_99_keeps_first_innings_identity(monkeypatch):
    m = make()
    scripted(monkeypatch, [delivery(wicket='Bowled')] * 9 + [dot()] * 89 + [delivery(wicket='Bowled')])
    for _ in range(99):
        r = m.next_ball()
    assert r['innings_end'] and m.innings == 2
    assert r['ball_data']['innings'] == 1
    assert r['ball_data']['legal_balls_after'] == 99
    assert r['ball_data']['wickets'] == 10
    assert sum(b['balls'] for b in r['scorecard_data']['bowlers']) == 99


def test_end_change_and_continuation_are_independent(monkeypatch):
    m = make()
    bowlers = m.bowler_manager._eligible_xi
    sequence = iter([bowlers[0], bowlers[1], bowlers[1], bowlers[2]])
    monkeypatch.setattr(m, 'pick_bowler', lambda: next(sequence))
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    responses = [m.next_ball() for _ in range(16)]
    assert 'Change of ends.' in responses[10]['commentary']
    assert 'continues for balls 11–15' in responses[10]['commentary']
    assert responses[9]['ball_data']['bowling_end'] == 0
    assert responses[9]['ball_data']['next_bowling_end'] == 1
    assert responses[10]['ball_data']['bowling_end'] == 1
    assert 'continues' not in responses[15]['commentary']


@pytest.mark.parametrize('stop', [1, 5, 10, 25, 80, 100])
def test_ledger_and_announcements_survive_checkpoint(monkeypatch, stop):
    m = make()
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    for _ in range(stop): m.next_ball()
    n = make()
    restore(n, json.loads(json.dumps(serialize(m))))
    assert n.hundred_deliveries == m.hundred_deliveries
    assert n.next_ball() == m.next_ball()


def test_legacy_checkpoint_keeps_old_selection_policy(monkeypatch):
    m = make()
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    m.next_ball()
    del m.hundred_policy_version
    del m._hundred_announced_set
    n = make()
    restore(n, json.loads(json.dumps(serialize(m))))
    assert n.hundred_policy_version == 1
    assert 'opens the bowling' not in n.next_ball()['commentary']


def test_failed_selector_recovers_using_safe_incumbent(monkeypatch):
    m = make()
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    for _ in range(5): m.next_ball()
    previous = m.current_bowler
    monkeypatch.setattr(m, 'pick_bowler', lambda: (_ for _ in ()).throw(ValueError('selection failed')))
    monkeypatch.setattr(m.bowler_manager, 'get_eligible_bowlers', lambda *a, **kw: [previous])
    r = m.next_ball()
    assert not r.get('error') and r['ball_data']['bowler'] == previous['name']
    assert 'continues' in r['commentary']


def test_productive_continuation_and_stronger_alternative(monkeypatch):
    m = make()
    # Isolate tactical selection from existing delivery-rating modifiers.
    monkeypatch.setattr(m, '_get_effective_bowler_dict', lambda p, **kw: p)
    monkeypatch.setattr(m, '_get_preferred_bowler_type', lambda _: 'mixed')
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    for p in m.bowler_manager._eligible_xi: p['bowling_rating'] = 70
    for _ in range(5): m.next_ball()
    incumbent = m.current_bowler
    assert m.pick_bowler()['name'] == incumbent['name']
    other = next(p for p in m.bowler_manager._eligible_xi if p['name'] != incumbent['name'])
    other['bowling_rating'] = 90
    # Remove reservation only to isolate ranking from a separate policy.
    m.current_over = m.fmt.death_phase.start
    assert m.pick_bowler()['name'] == other['name']


def test_death_reserve_and_rain_fallback(monkeypatch):
    m = make()
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    monkeypatch.setattr(m, '_get_effective_bowler_dict', lambda p, **kw: p)
    monkeypatch.setattr(m, '_get_preferred_bowler_type', lambda _: 'mixed')
    manager = m.bowler_manager
    for i, p in enumerate(manager._eligible_xi):
        p['bowling_rating'] = 90 - i * 5
    specialists = manager._eligible_xi[:2]
    for _ in range(75): m.next_ball()
    original = manager.get_eligible_bowlers
    assert m.pick_bowler() in original(15, 5)
    for _ in range(5): m.next_ball()
    assert all(manager.overs_remaining(p['name']) == 2 for p in specialists)
    assert manager.get_eligible_bowlers(16, 4)

    # Rain can make an otherwise preferred reserve infeasible. The selector
    # must use the ordinary safe pool, not abort or relax a hard quota.
    n = make()
    n.fmt.revise_short_innings(10)
    n.overs = 10
    legal = n.bowler_manager.get_eligible_bowlers
    monkeypatch.setattr(n.bowler_manager, 'get_eligible_bowlers',
                        lambda *args, **kwargs: [] if kwargs.get('death_reserve') else legal(*args))
    assert n.pick_bowler() in legal(0, 10)


def test_reduced_powerplay_inside_set(monkeypatch):
    m = make()
    m.fmt.revise_short_innings(5)
    m.overs = 5
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    for _ in range(6): r = m.next_ball()
    assert 'Powerplay complete after 6 balls' in r['commentary']
    assert r['fielders_outside_circle'] == 5


@pytest.mark.parametrize('boundary', [5, 10])
def test_odd_run_strike_rotation_at_boundaries(monkeypatch, boundary):
    m = make()
    scripted(monkeypatch, [dot()] * (boundary - 1) + [delivery(1)])
    for _ in range(boundary): r = m.next_ball()
    event = r['ball_data']
    # At 5 only the run changes strike; at 10 the end change cancels it.
    assert (r['striker'] == event['striker']) == (boundary == 10)


def test_wicket_on_ball_100_is_archived_once(monkeypatch):
    m = make()
    scripted(monkeypatch, [delivery(wicket='Bowled')] * 9 + [dot()] * 90 + [delivery(wicket='Bowled')])
    for _ in range(100): r = m.next_ball()
    assert r['innings_end']
    assert r['ball_data']['legal_balls_after'] == 100
    assert 'End of set: balls 96–100' in r['commentary']
    assert len(m.hundred_deliveries) == 100
    assert sum(s['overs'] for s in m.first_innings_bowling_stats.values()) == 20


def test_chase_completes_at_ball_84(monkeypatch):
    m = make()
    scripted(monkeypatch, [delivery(1)] * 83 + [dot()] * 17)
    for _ in range(101): m.next_ball()
    assert m.innings == 2 and m.target == 84
    scripted(monkeypatch, [delivery(1)] * 84)
    for _ in range(84): r = m.next_ball()
    assert r['match_over'] and '16 balls remaining' in r['result']
    assert r['ball_data']['innings'] == 2 and r['ball_data']['legal_balls_after'] == 84
    assert r['score'] == 84


def test_extras_totals_reconcile_with_ledger(monkeypatch):
    m = make()
    scripted(monkeypatch, [delivery(3, 'Wide'), delivery(4, 'Byes'), delivery(2, 'Leg Bye')])
    events = [m.next_ball()['ball_data'] for _ in range(3)]
    stats = m.bowler_stats[events[0]['bowler']]
    assert (stats['wides'], stats['byes'], stats['legbyes']) == (3, 4, 2)
    assert stats['runs'] == 3 and m.score == 9
    assert sum(sum(e['extras'].values()) for e in events) == 9


def test_stumped_wide_records_runs_and_wicket_without_ball(monkeypatch):
    m = make()
    scripted(monkeypatch, [delivery(1, 'Wide', 'Stumped')])
    event = m.next_ball()['ball_data']
    assert event['extras'] == {'Wide': 1}
    assert event['bowler_wicket'] and not event['is_legal']
    assert event['bowler_runs'] == 1 and event['batter_balls'] == 0


def test_full_manual_match_and_pending_decision_restore(monkeypatch):
    m = make(simulation_mode='manual')
    monkeypatch.setattr(module, 'calculate_outcome', dot)
    prompt = m.next_ball()
    assert prompt['decision_required']
    n = make()
    restore(n, json.loads(json.dumps(serialize(m))))
    assert n.pending_decision == m.pending_decision
    announcements = []
    for _ in range(260):
        r = n.next_ball()
        if r.get('decision_required'):
            options = n.pending_decision['options']
            selected = next((o for o in options if o.get('continue_bowler')), options[0])
            assert n.submit_pending_decision(selected['index'])[1] == 200
        if r.get('ball_data'):
            announcements.append(r.get('commentary', ''))
        if r.get('match_over'): break
    else:
        pytest.fail('manual match did not complete')
    assert len(n.hundred_deliveries) == 200
    assert sum('opens the bowling' in text for text in announcements) == 2
    assert sum('continues for balls' in text for text in announcements) > 0
    assert all(s['balls_bowled'] <= 20 for s in n.bowler_stats.values())
