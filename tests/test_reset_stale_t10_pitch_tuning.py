"""Tests for the T10 aggression pass's stale-tuning strip logic.

Same job as the T20 version: tell an involuntary snapshot of the old defaults
(written by the mode picker, which saved the whole effective config) apart from
a value a user actually chose. The first must go so the user inherits the
retuned YAML; the second must survive untouched.
"""

import copy

from engine.ground_config import _deep_merge, get_defaults
from migrations.reset_stale_t10_pitch_tuning import (
    OLD_DRY_WICKET_FACTORS,
    OLD_SCORING_MATRIX,
    _strip_stale_t10,
)
from migrations.reset_stale_t20_pitch_tuning import _strip_stale


def _full_old_snapshot():
    """What the T10 mode picker used to persist: every old default, verbatim."""
    return {
        "version": 2,
        "active_game_mode": "aggressive",
        "pitch_profiles": {
            pitch: {"scoring_matrix": copy.deepcopy(matrix)}
            for pitch, matrix in OLD_SCORING_MATRIX.items()
        },
        "phase_boosts": {
            "powerplay": {"boundary_multiplier": 1.25, "overs_start": 0, "overs_end": 2},
            "death_overs": {
                "overs_start": 7, "overs_end": 9,
                "boundary_boost_batting_pitch": 1.9,   # unchanged by this pass
                "boundary_boost_bowling_pitch": 1.8,
                "wicket_boost": 1.6,
            },
            "second_innings_death": {"scoring_boost": 1.15, "wicket_boost": 1.1},
        },
    }


def test_involuntary_snapshot_is_fully_stripped():
    cfg = _full_old_snapshot()
    cfg["pitch_profiles"]["Dry"]["wicket_factors"] = copy.deepcopy(
        OLD_DRY_WICKET_FACTORS)

    stripped = _strip_stale_t10(cfg)

    assert "pitch_profiles" not in cfg, (
        f"every stored pitch was an old-default copy, so the branch should be "
        f"gone; got {cfg.get('pitch_profiles')!r}"
    )
    # 4 scoring matrices + Dry.wicket_factors + 5 phase-boost scalars.
    assert len(stripped) == 10
    assert "second_innings_death" not in cfg["phase_boosts"]
    # Values this pass did not move are none of its business.
    assert cfg["phase_boosts"]["death_overs"]["boundary_boost_batting_pitch"] == 1.9


def test_deliberate_tuning_survives():
    cfg = _full_old_snapshot()
    cfg["pitch_profiles"]["Dead"]["scoring_matrix"]["Six"] = 0.2   # chosen
    cfg["phase_boosts"]["powerplay"]["boundary_multiplier"] = 1.6  # chosen

    _strip_stale_t10(cfg)

    assert cfg["pitch_profiles"]["Dead"]["scoring_matrix"]["Six"] == 0.2
    assert cfg["phase_boosts"]["powerplay"]["boundary_multiplier"] == 1.6
    assert "Green" not in cfg["pitch_profiles"]


def test_stripping_is_idempotent():
    cfg = _full_old_snapshot()
    assert _strip_stale_t10(cfg)
    assert _strip_stale_t10(cfg) == [], "a second run must find nothing left"


def test_formats_do_not_strip_each_others_keys():
    """A T10 row must not be judged against the T20/List A legacy tables."""
    t10 = _full_old_snapshot()
    assert _strip_stale(t10, "T20") == []
    assert _strip_stale(t10, "ListA") == []


def test_stripped_snapshot_inherits_the_retuned_defaults():
    """The point of the whole exercise."""
    cfg = _full_old_snapshot()
    cfg["pitch_profiles"]["Dry"]["wicket_factors"] = copy.deepcopy(
        OLD_DRY_WICKET_FACTORS)
    _strip_stale_t10(cfg)

    defaults = get_defaults("T10", mutable=True)
    merged = _deep_merge(copy.deepcopy(defaults),
                         {k: v for k, v in cfg.items() if k != "version"})
    for pitch, old in OLD_SCORING_MATRIX.items():
        assert merged["pitch_profiles"][pitch]["scoring_matrix"] != old
    assert merged["phase_boosts"]["second_innings_death"]["wicket_boost"] == 1.0
    assert merged["phase_boosts"]["death_overs"]["wicket_boost"] > 1.6
