"""Manual bowling must not inherit the automatic unused-bowler policy."""
import pytest

from engine.match import Match
from tests.test_bowler_consecutive_guards import _build_match_data


FORMATS = [('T20', 20), ('ListA', 40), ('ListA', 50), ('T10', 10), ('Hundred', 20)]


def death_match(fmt, length, fresh, captaincy):
    data = _build_match_data(fmt, 5 + fresh, 'manual')
    data['scheduled_overs'] = length
    if captaincy:
        data['captaincy_model_version'] = 1
    match = Match(data)
    cap = match.fmt.max_bowler_overs
    counts = [cap, cap, cap, cap if fresh == 1 else cap - 1, cap - 1]
    for bowler, count in zip(match.bowling_team, counts):
        match.bowler_history[bowler['name']] = count
    match.current_over = sum(counts)
    match.current_ball = 0
    match.current_bowler = match.bowling_team[3]
    match.bowler_manager._last_bowler = match.current_bowler['name']
    match.bowler_selected_for_over = -1
    return match


@pytest.mark.parametrize('fmt,length', FORMATS)
@pytest.mark.parametrize('fresh', [1, 2])
@pytest.mark.parametrize('captaincy', [False, True])
@pytest.mark.parametrize('innings', [1, 2])
def test_manual_death_choice_accepts_used_bowler(fmt, length, fresh, captaincy, innings, monkeypatch):
    match = death_match(fmt, length, fresh, captaincy)
    match.innings = innings
    match.target = 200 if innings == 2 else None
    preferred = match.bowling_team[4]
    match.next_ball()
    decision = match.pending_decision
    options = {o['name'] for o in decision['options']}
    assert preferred['name'] in options
    assert match.bowling_team[0]['name'] not in options  # exhausted quota
    if fmt != 'Hundred':
        assert match.current_bowler['name'] not in options
    assert match.bowling_team[5]['name'] in options  # unused remains optional
    if captaincy:
        assert decision['recommendation'] in options
    response, status = match.submit_pending_decision(4)
    assert status == 200, response
    assert match.current_bowler == preferred
    assert match.bowler_selected_for_over == match.current_over
    deliveries = []

    def dot_ball(**kwargs):
        deliveries.append(kwargs['bowler']['name'])
        return {'runs': 0, 'batter_out': False, 'is_extra': False,
                'description': 'Defended.'}

    monkeypatch.setattr('engine.match.calculate_outcome', dot_ball)
    match.next_ball()
    assert deliveries == [preferred['name']]


@pytest.mark.parametrize('fmt,length', FORMATS[:3])
def test_automatic_unused_bowler_policy_is_preserved(fmt, length):
    match = death_match(fmt, length, 2, True)
    expected = {p['name'] for p in match.bowling_team[5:7]}
    assert {p['name'] for p in match._captain_eligible()} == expected


def test_manual_choice_still_requires_a_completable_allocation():
    match = death_match('T20', 20, 2, True)
    # Three overs left; only A has two overs left and B has one. B cannot
    # start: A would then have to bowl both remaining overs consecutively.
    for bowler, count in zip(match.bowling_team, [2, 3, 4, 4, 4, 0, 0]):
        match.bowler_history[bowler['name']] = count
    for bowler in match.bowling_team[5:7]:
        bowler['will_bowl'] = False
    match.current_over = 17
    match.current_bowler = match.bowling_team[4]
    match.bowler_manager._last_bowler = match.current_bowler['name']
    decision = match._create_next_bowler_decision()
    assert [o['name'] for o in decision['options']] == [match.bowling_team[0]['name']]
    response, status = match.submit_pending_decision(1)
    assert status == 400
