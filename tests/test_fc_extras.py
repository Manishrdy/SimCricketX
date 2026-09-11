import copy

import pytest

import engine.match as match_module
from engine.fc_delivery import resolve_delivery, extra_profile, apply_keeper_mass
from tests.test_fc_format import _fc_match


def event(runs=0, kind=None, wicket=None, **kw):
    return dict(
        type="extra" if kind else ("wicket" if wicket else "run"),
        runs=runs,
        is_extra=bool(kind),
        extra_type=kind,
        batter_out=bool(wicket),
        wicket_type=wicket,
        description="test",
        **kw
    )


@pytest.mark.parametrize(
    "primary,secondary,total,bat,charged,running,legal",
    [
        (event(1, "No Ball"), event(4), 5, 4, 5, 0, False),
        (event(1, "No Ball"), event(1), 2, 1, 2, 1, False),
        (event(1, "No Ball"), event(1, non_bat_type="Byes"), 2, 0, 1, 1, False),
        (
            event(1, "No Ball"),
            event(1, wicket="Run Out", dismissed_end="non_striker"),
            2,
            1,
            2,
            1,
            False,
        ),
        (event(1, "No Ball"), event(0, wicket="Bowled"), 1, 0, 1, 0, False),
        (event(3, "Wide"), None, 3, 0, 3, 2, False),
        (event(4, "Leg Bye"), None, 4, 0, 0, 0, True),
    ],
)
def test_delivery_components(primary, secondary, total, bat, charged, running, legal):
    result = resolve_delivery(primary, secondary)
    d = result["delivery"]
    assert d["total_runs"] == total == bat + sum(d["extras"].values())
    assert d["bat_runs"] == bat
    assert d["bowler_runs"] == charged
    assert d["completed_runs"] == running
    assert d["legal"] == legal
    if secondary and secondary.get("wicket_type") == "Run Out":
        assert result["batter_out"]
        assert result["dismissed_end"] == "non_striker"


def test_penalty_wins_before_contact_or_runout():
    result = resolve_delivery(event(1, "No Ball"), event(1, wicket="Run Out"), 1)
    assert result["runs"] == 1 and result["bat_runs"] == 0
    assert not result["batter_out"]
    result = resolve_delivery(event(1, wicket="Run Out"), runs_needed=1)
    assert result["runs"] == 1 and not result["batter_out"]


def test_keeper_improves_only_byes_in_absolute_probability():
    weights = dict(Dot=0.6, Single=0.3, Wicket=0.05, Extras=0.05)
    prior = None
    for rating in (10, 30, 50, 68, 90, 100):
        profile = extra_profile(
            {"bowling_type": "Fast"},
            [{"is_wicketkeeper": True, "fielding_rating": rating}],
        )
        adjusted = apply_keeper_mass(weights, profile)
        absolute = {
            k: adjusted["Extras"] * v / sum(profile.values())
            for k, v in profile.items()
        }
        assert sum(adjusted.values()) == pytest.approx(sum(weights.values()))
        if prior:
            assert absolute["Byes"] < prior["Byes"]
            for kind in ("Wide", "No Ball", "Leg Bye"):
                assert absolute[kind] == pytest.approx(prior[kind])
        prior = absolute


def bowl(m):
    before = m.match_balls_bowled
    for _ in range(20):
        response = m.next_ball()
        assert "error" not in response
        if m.match_balls_bowled > before:
            return response
    pytest.fail("No delivery")


@pytest.mark.parametrize(
    "primary,secondary,score,bat,charged,extra_key,extra_runs",
    [
        (event(1, "No Ball"), event(4), 5, 4, 5, "noballs", 1),
        (event(1, "No Ball"), event(1, non_bat_type="Byes"), 2, 0, 1, "byes", 1),
        (event(3, "Wide"), None, 3, 0, 3, "wides", 3),
        (event(4, "Leg Bye"), None, 4, 0, 0, "legbyes", 4),
        (
            event(1, "No Ball"),
            event(1, wicket="Run Out", dismissed_end="non_striker"),
            2,
            1,
            2,
            "noballs",
            1,
        ),
    ],
)
def test_match_accounting_and_resume(
    monkeypatch, primary, secondary, score, bat, charged, extra_key, extra_runs
):
    m = _fc_match()
    striker = m.current_striker["name"]
    outcomes = iter([primary] + ([secondary] if secondary is not None else []))
    monkeypatch.setattr(
        match_module, "calculate_outcome", lambda **kw: copy.deepcopy(next(outcomes))
    )
    bowl(m)
    assert m.score == score
    assert m.batsman_stats[striker]["runs"] == bat
    bowler = m.bowler_stats[m.current_bowler["name"]]
    assert bowler["runs"] == charged
    assert bowler[extra_key] == extra_runs
    assert m.current_ball == (1 if primary["extra_type"] == "Leg Bye" else 0)
    assert not m.free_hit_active
    assert m._generate_detailed_scorecard()["extras"] == score - bat
    snapshot = m.serialize_fc_snapshot()
    restored = _fc_match()
    restored.restore_fc_snapshot(snapshot)
    assert restored.bowler_stats == m.bowler_stats
    assert restored.batsman_stats == m.batsman_stats
    if secondary and secondary.get("wicket_type") == "Run Out":
        assert m.wickets == 1
        assert m.fc_innings_partnerships[1][0]["runs"] == score
    else:
        assert m.current_partnership_runs == score


def test_winning_no_ball_does_not_sample_contact(monkeypatch):
    m = _fc_match()
    m.fc_innings = 4
    m.target = 1
    calls = []

    def outcome(**kw):
        calls.append(kw)
        return event(1, "No Ball")

    monkeypatch.setattr(match_module, "calculate_outcome", outcome)
    bowl(m)
    assert len(calls) == 1
    assert m.score == 1


@pytest.mark.parametrize(
    "ball,kind,run,expected_partner",
    [(0, "No Ball", 1, True), (5, "No Ball", 1, True), (5, "Leg Bye", 1, False)],
)
def test_completed_running_and_end_of_over(
    monkeypatch, ball, kind, run, expected_partner
):
    m = _fc_match()
    m.current_ball = ball
    m.current_bowler = next(p for p in m.bowling_team if p["will_bowl"])
    partner = m.current_non_striker["name"]
    outcomes = iter([event(1, kind), event(run)])
    monkeypatch.setattr(
        match_module, "calculate_outcome", lambda **kw: copy.deepcopy(next(outcomes))
    )
    bowl(m)
    assert (m.current_striker["name"] == partner) is expected_partner


def test_final_wicket_no_ball_records_penalty_before_innings_archive(monkeypatch):
    m = _fc_match()
    m.wickets = 9
    m.remaining_batter_indices.clear()
    outcomes = iter(
        [event(1, "No Ball"), event(1, wicket="Run Out", dismissed_end="striker")]
    )
    monkeypatch.setattr(
        match_module, "calculate_outcome", lambda **kw: copy.deepcopy(next(outcomes))
    )
    response = bowl(m)
    assert response["innings_end"]
    assert response["scorecard_data"]["total_score"] == 2
    assert response["scorecard_data"]["extras"] == 1
    assert response["scorecard_data"]["wickets"] == 10
    saved = m.fc_innings_stats[0]
    assert sum(b["noballs"] for b in saved["bowling_stats"].values()) == 1


def test_partner_runout_does_not_reset_surviving_strikers_form():
    m = _fc_match()
    striker = m.current_striker["name"]
    partner = m.current_non_striker["name"]
    m.batsman_stats[striker]["form"] = 1.1
    m.batsman_stats[partner]["form"] = 1.1
    outcome = resolve_delivery(
        event(1, "No Ball"), event(1, wicket="Run Out", dismissed_end="non_striker")
    )
    m._update_batter_form(striker, outcome)
    assert m.batsman_stats[striker]["form"] > 1.0
    assert m.batsman_stats[partner]["form"] == 1.0
    assert outcome["batter_out"]  # observer input is immutable
