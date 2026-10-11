"""Completed-over maiden tracking and pre-commit commentary regressions."""
import pytest

from tests.test_fc_format import _fc_match
import engine.match as match_module


def _outcome(runs=0, extra=None):
    return dict(runs=runs, is_extra=extra is not None, extra_type=extra,
                batter_out=False, wicket_type=None, description="Test delivery")


@pytest.mark.parametrize("extra", [None, "Wide", "No Ball", "Byes", "Leg Bye"])
def test_fc_completed_over_maiden_streak(app, monkeypatch, extra):
    match = _fc_match()
    current = _outcome()
    monkeypatch.setattr(match_module, "calculate_outcome", lambda **kwargs: dict(current))
    states = []
    monkeypatch.setattr(match.commentary_engine, "get_commentary",
                        lambda outcome, state: states.append(dict(state)) or "")

    def play_over():
        target = match.current_over + 1
        for _ in range(10):
            match.next_ball()
            if match.current_over == target:
                return
        pytest.fail("Over did not complete")

    for expected in (1, 2, 3):
        play_over()
        assert match.fc_consecutive_maidens == expected
        assert states[-1]['is_maiden_over']
        assert states[-1]['fc_consecutive_maidens'] == expected

    if extra is None:
        current = _outcome(4)
        before = match.score
        play_over()
        assert match.score - before == 24
    else:
        # Include extras at both the start and (for legal extras) the end.
        current = _outcome(1, extra)
        match.next_ball()
        current = _outcome(1, extra) if extra in ('Byes', 'Leg Bye') else _outcome()
        play_over()

    maiden = extra in ('Byes', 'Leg Bye')
    assert match.fc_consecutive_maidens == (4 if maiden else 0)
    assert states[-1]['is_maiden_over'] == maiden
    assert sum(s['maidens'] for s in match.bowler_stats.values()) == (4 if maiden else 3)
    current = _outcome()
    play_over()
    assert match.fc_consecutive_maidens == (5 if maiden else 1)
