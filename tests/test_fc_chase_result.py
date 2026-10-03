"""A result achieved by the last delivery takes precedence over stumps."""
import copy

import pytest

import engine.match as match_module
from tests.test_fc_extras import event
from tests.test_fc_format import _fc_match


@pytest.mark.parametrize("weather_v2", [False, True])
@pytest.mark.parametrize("day", [3, 4])
@pytest.mark.parametrize("runs,kind", [(1, None), (4, None), (1, "Leg Bye"), (0, None)])
def test_final_ball_chase_result(monkeypatch, weather_v2, day, runs, kind):
    m = _fc_match()
    monkeypatch.setattr(m, "_create_match_archive", lambda: True)
    m._fc_start_next_innings(4, m.home_xi, m.away_xi)
    m.target = 100
    m.score, m.wickets = 99, 7
    m.fc_day = day
    m.fc_weather_v2 = weather_v2
    m.fc_day_start_emitted = True
    m.fc_sessions_taken_today = 2
    m.current_over, m.current_ball = 12, 5
    m.fc_day_balls_bowled_today = m._fc_effective_overs_today() * 6 - 1
    if weather_v2:
        m.fc_day_balls_bowled_today = m._fc_minimum_overs_today() * 6 - 1
        m.fc_clock_minute = m._fc_latest_close_minute() - 0.01
    m.current_bowler = m.bowling_team[5]
    m.bowler_selected_for_over = m.current_over
    monkeypatch.setattr(match_module, "calculate_outcome",
                        lambda **kw: copy.deepcopy(event(runs, kind)))

    m.next_ball()
    assert m.score == 99 + runs
    assert m.current_ball == 0
    balls = m.match_balls_bowled
    response = m.next_ball()

    if runs:
        assert response["match_over"] is True
        assert response["result"] == "HOM won by 3 wicket(s)."
        assert m.match_status == "completed"
        assert m.winner_is_home is True
        assert (m.margin_type, m.margin_value) == ("wickets", 3)
        assert response["scorecard_data"]["total_score"] == 99 + runs
        assert m.next_ball()["result"] == response["result"]
    elif day == 4:
        assert response["match_over"] is True
        assert response["result"] == "Match drawn."
    else:
        assert response["day_break"] is True
    assert m.match_balls_bowled == balls


@pytest.mark.parametrize("kind", ["Wide", "No Ball"])
def test_winning_extra_precedes_forced_close(monkeypatch, kind):
    m = _fc_match()
    monkeypatch.setattr(m, "_create_match_archive", lambda: True)
    m._fc_start_next_innings(4, m.home_xi, m.away_xi)
    m.target, m.score, m.wickets = 100, 99, 7
    m.fc_day = 4
    m.current_ball = 5
    m.current_bowler = m.bowling_team[5]
    monkeypatch.setattr(match_module, "calculate_outcome",
                        lambda **kw: copy.deepcopy(event(1, kind)))
    m.next_ball()
    assert m.score == 100
    assert m.current_ball == 5
    m.fc_force_day_end = True

    def no_weather_after_win():
        pytest.fail("Weather was processed after the chase was already won")

    monkeypatch.setattr(m, "_fc_weather_pre_delivery", no_weather_after_win)
    response = m.next_ball()
    assert response["match_over"] is True
    assert response["result"] == "HOM won by 3 wicket(s)."
