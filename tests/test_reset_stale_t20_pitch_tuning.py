"""Tests for the 2026-08-16 stale-T20-pitch-tuning strip logic.

The migration's job is to tell two blobs apart:
  • an involuntary snapshot of the old defaults (written by the mode picker,
    which saved the whole effective config) — must be stripped so the user
    inherits the recalibrated YAML, and
  • a value a user actually chose — must survive untouched.

Everything here exercises _strip_stale() directly; the SQL wrapper around it is
the same shape as every other migration in migrations/.
"""

import copy

from engine.ground_config import get_defaults
from migrations.reset_stale_t20_pitch_tuning import (
    OLD_BULLY_MODIFIERS,
    OLD_DEATH_BOUNDARY_BOOST,
    OLD_DRY_WICKET_FACTORS,
    OLD_SCORING_MATRIX,
    _strip_stale,
)


def _full_old_snapshot():
    """What the mode picker used to persist: every old default, verbatim."""
    return {
        "version": 2,
        "active_game_mode": "aggressive",
        "pitch_profiles": {
            pitch: {"scoring_matrix": copy.deepcopy(matrix)}
            for pitch, matrix in OLD_SCORING_MATRIX.items()
        },
        "phase_boosts": {
            "death_overs": {
                "overs_start": 16,
                "overs_end": 19,
                "boundary_boost_batting_pitch": OLD_DEATH_BOUNDARY_BOOST,
            }
        },
        "game_modes": {
            "flat_track_bully": {"modifiers": copy.deepcopy(OLD_BULLY_MODIFIERS)}
        },
    }


def test_involuntary_snapshot_is_fully_stripped():
    cfg = _full_old_snapshot()
    cfg["pitch_profiles"]["Dry"]["wicket_factors"] = copy.deepcopy(
        OLD_DRY_WICKET_FACTORS)

    stripped = _strip_stale(cfg)

    assert "pitch_profiles" not in cfg, (
        f"every pitch was an old-default copy, so the branch should be gone; "
        f"got {cfg.get('pitch_profiles')!r}"
    )
    assert "boundary_boost_batting_pitch" not in cfg["phase_boosts"]["death_overs"]
    assert "game_modes" not in cfg
    # 5 scoring matrices + Dry.wicket_factors + death boost + bully modifiers.
    assert len(stripped) == 8, stripped

    # A deliberate choice unrelated to the recalibration must survive.
    assert cfg["active_game_mode"] == "aggressive"
    # Untouched siblings inside a partially-stripped dict stay put.
    assert cfg["phase_boosts"]["death_overs"]["overs_start"] == 16


def test_user_customised_matrix_survives():
    cfg = _full_old_snapshot()
    cfg["pitch_profiles"]["Flat"]["scoring_matrix"]["Six"] = 0.11  # deliberate

    _strip_stale(cfg)

    assert cfg["pitch_profiles"]["Flat"]["scoring_matrix"]["Six"] == 0.11, (
        "a hand-tuned Flat matrix differs from the old default and must be kept"
    )
    # ...while its involuntary neighbours still go.
    assert "Dead" not in cfg.get("pitch_profiles", {})


def test_customised_bully_and_death_boost_survive():
    cfg = _full_old_snapshot()
    cfg["phase_boosts"]["death_overs"]["boundary_boost_batting_pitch"] = 2.6
    cfg["game_modes"]["flat_track_bully"]["modifiers"]["six_mult"] = 1.5

    _strip_stale(cfg)

    assert cfg["phase_boosts"]["death_overs"]["boundary_boost_batting_pitch"] == 2.6
    assert cfg["game_modes"]["flat_track_bully"]["modifiers"]["six_mult"] == 1.5


def test_float_noise_from_json_round_trip_still_matches():
    """JSON round-trips can perturb the last bits; the strip must not be brittle."""
    cfg = _full_old_snapshot()
    cfg["pitch_profiles"]["Green"]["scoring_matrix"]["Dot"] = 0.35 + 1e-12

    _strip_stale(cfg)

    assert "Green" not in cfg.get("pitch_profiles", {})


def test_is_idempotent():
    cfg = _full_old_snapshot()
    _strip_stale(cfg)
    after_first = copy.deepcopy(cfg)

    assert _strip_stale(cfg) == [], "second pass should find nothing left to strip"
    assert cfg == after_first


def test_blob_with_no_stale_values_is_untouched():
    cfg = {"version": 2, "active_game_mode": "auto", "blending": {
        "pitch_weight": 0.7, "skill_weight": 0.3}}
    before = copy.deepcopy(cfg)

    assert _strip_stale(cfg) == []
    assert cfg == before


def test_stripped_snapshot_inherits_the_recalibrated_defaults():
    """The point of the whole exercise: after stripping, the user gets the new YAML."""
    from engine.ground_config import _deep_merge

    cfg = _full_old_snapshot()
    _strip_stale(cfg)

    defaults = get_defaults("T20", mutable=True)
    effective = _deep_merge(defaults, {k: v for k, v in cfg.items() if k != "version"})

    for pitch in OLD_SCORING_MATRIX:
        assert (effective["pitch_profiles"][pitch]["scoring_matrix"]
                == defaults["pitch_profiles"][pitch]["scoring_matrix"]), (
            f"{pitch} should now resolve to the recalibrated default"
        )
    assert (effective["phase_boosts"]["death_overs"]["boundary_boost_batting_pitch"]
            == defaults["phase_boosts"]["death_overs"]["boundary_boost_batting_pitch"])
