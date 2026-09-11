import pytest
from tests.test_fc_format import _fc_match


@pytest.mark.parametrize(
    "innings,follow,score,target,lead",
    [
        (1, False, 50, None, None),
        (2, False, 250, None, -150),
        (3, False, 50, None, 200),
        (3, True, 50, None, -100),
        (4, False, 200, 201, 0),
        (4, False, 201, 201, 1),
    ],
)
def test_position_shared_by_tactics_and_scoreboard(
    innings, follow, score, target, lead
):
    m = _fc_match()
    m.fc_innings = innings
    m.follow_on_enforced = follow
    m.fc_innings_totals = {1: {"score": 400}, 2: {"score": 250}}
    m.score = score
    m.target = target
    assert m._fc_aggregate_lead() == lead
    assert m._fc_lead_before_ball() == lead
    assert m._fc_build_match_state()["lead"] == lead
    if lead is None:
        assert m._fc_batting_team_lead_or_trail() is None
    elif lead:
        assert str(abs(lead)) in m._fc_batting_team_lead_or_trail()
    else:
        assert "level" in m._fc_batting_team_lead_or_trail()


def test_third_innings_lead_cannot_trigger_deficit_survival():
    m = _fc_match()
    m.fc_innings = 3
    m.fc_day = 4
    m.fc_innings_totals = {1: {"score": 400}, 2: {"score": 250}}
    m.score = 50
    assert m._fc_build_match_state()["intent"]["survival"] == 0
