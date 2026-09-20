"""Drama follows the active deadline; weather and dew retain scheduled length."""
import random
from types import SimpleNamespace

import pytest

from engine.format_config import get_format
from engine.scenario_engine import ScenarioEngine
from engine.weather import generate_weather_script, min_overs_for_result
from tests.test_lista_format import _build_match_data
from engine.match import Match


@pytest.mark.parametrize('length', [20, 40, 50])
@pytest.mark.parametrize('mode', ['last_ball_six', 'win_by_1_run', 'super_over_thriller'])
def test_drama_tracks_final_overs(length, mode):
    match = SimpleNamespace(fmt=SimpleNamespace(overs=length), innings=2,
                            target=201, score=177, wickets=4, current_ball=0,
                            current_over=length-6)
    engine = ScenarioEngine(mode, match)
    assert engine.get_phase() == 'free_play'
    match.current_over = length-5
    assert engine.get_phase() == 'convergence'
    match.current_over = length-2
    assert engine.get_phase() == 'finale'
    engine._generate_finale_script()
    assert len(engine.finale_script) == 12
    expected = {'last_ball_six': 24, 'win_by_1_run': 22, 'super_over_thriller': 23}
    assert sum(ball['runs'] for ball in engine.finale_script) == expected[mode]
    if mode == 'last_ball_six':
        assert engine.finale_script[-1]['runs'] == 6


def test_rain_discards_old_finale_and_rechecks_target():
    match = SimpleNamespace(fmt=get_format('ListA', 40), innings=2,
                            target=201, score=177, wickets=4, current_ball=0,
                            current_over=38)
    engine = ScenarioEngine('last_ball_six', match)
    assert engine.get_phase() == 'finale'
    engine._generate_finale_script()
    engine.finale_ball_index = 3
    match.fmt.overs = 39
    match.target = 195
    assert engine.get_phase() == 'finale'
    assert engine.finale_script is None
    assert engine.finale_ball_index == 0
    engine._generate_finale_script()
    assert len(engine.finale_script) == 6
    assert sum(ball['runs'] for ball in engine.finale_script) == 18
    assert match.fmt.scheduled_overs == 40


@pytest.mark.parametrize('forecast', ['clear', 'passing_showers', 'rain_around', 'storm_warning'])
@pytest.mark.parametrize('night', [False, True])
def test_40_over_conditions_can_be_combined(forecast, night):
    data = _build_match_data('Hard', 4101)
    data.update(scheduled_overs=40, is_day_night=night,
                scenario_mode='last_ball_six', weather_forecast=forecast)
    script = generate_weather_script(forecast, 40, 'ListA', rng=random.Random(1))
    data['weather_script'] = script
    match = Match(data)
    assert match.scenario_engine is not None
    assert match.scenario_engine.get_phase() == 'first_innings'
    assert match.data['is_day_night'] == night
    assert match.original_overs == 40
    assert match.weather_script == script
    for event in script['events']:
        assert 1 <= event['at_global_over'] < 80
        assert 1 <= event['overs_lost'] <= 32
    assert min_overs_for_result('ListA') == 20

    # Exercise delivery, interruptions, DLS and scenario overrides together.
    for _ in range(1000):
        result = match.next_ball()
        assert not result.get('error'), result
        if result.get('match_over') or result.get('super_over_required'):
            break
    else:
        pytest.fail('Combined conditions did not finish')
    assert match.data['scheduled_overs'] == 40
    assert match.fmt.scheduled_overs == 40
