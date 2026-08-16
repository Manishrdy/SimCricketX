"""
Per-format ground configuration tests.

Covers the three defects the per-format rework was built to fix:
  1. List A ignored the ground config entirely (only blending weights applied).
  2. A saved config froze forever — later additions to the factory defaults
     never reached the users who had customised anything.
  3. The game-mode picker was inert; the dynamic selector always won.

Plus the storage invariant that makes it all work: one row per
(user, match_format), with neither format able to clobber the other.
"""

import json

import pytest

from database.models import UserGroundConfig
from engine import ball_outcome as bo
from engine.ground_config import (
    AUTO_GAME_MODE,
    VALID_FORMATS,
    _deep_merge,
    get_defaults,
    get_effective_config,
    get_lista_run_factor,
    get_scoring_matrix,
    get_user_config,
    normalise_format,
    reset_user_config,
    save_user_config,
    _validate_config,
)


# ─────────────────────────── Defaults / merging ────────────────────────────

class TestDefaults:
    def test_every_supported_format_has_defaults(self):
        for fmt in VALID_FORMATS:
            block = get_defaults(fmt)
            assert block, f"{fmt} has no defaults block"

    def test_unknown_format_falls_back_to_t20(self):
        assert normalise_format("Hundred") == "T20"
        assert normalise_format(None) == "T20"
        assert normalise_format("ListA") == "ListA"

    def test_get_defaults_mutable_does_not_poison_cache(self):
        """A caller mutating the returned dict must not corrupt the cache."""
        block = get_defaults("T20", mutable=True)
        block["pitch_profiles"]["Hard"]["run_factor"] = 99.0
        assert get_defaults("T20")["pitch_profiles"]["Hard"]["run_factor"] != 99.0

    def test_shipped_matrices_sum_to_one(self):
        """Both formats' shipped matrices must validate."""
        for fmt in VALID_FORMATS:
            ok, err = _validate_config(get_defaults(fmt, mutable=True), fmt)
            assert ok, f"{fmt}: {err}"


class TestDeepMerge:
    def test_stored_values_win_and_missing_keys_inherit(self):
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        override = {"nested": {"y": 99}}
        merged = _deep_merge(base, override)
        assert merged == {"a": 1, "nested": {"x": 1, "y": 99}}

    def test_merge_does_not_mutate_base(self):
        base = {"nested": {"x": 1}}
        _deep_merge(base, {"nested": {"x": 2}})
        assert base["nested"]["x"] == 1

    def test_backfills_key_added_after_user_saved(self, app, regular_user):
        """The real-world bug: a stored config predating the `Medium` wicket
        factor left medium-pacers falling through to `default` forever."""
        with app.app_context():
            stored = get_defaults("T20", mutable=True)
            del stored["pitch_profiles"]["Green"]["wicket_factors"]["Medium"]
            stored["pitch_profiles"]["Green"]["wicket_factors"]["Fast"] = 1.3
            ok, err = save_user_config(regular_user.id, stored, "T20")
            assert ok, err

            effective = get_effective_config(regular_user.id, "T20")
            wf = effective["pitch_profiles"]["Green"]["wicket_factors"]
            # Backfilled from the current defaults...
            assert wf["Medium"] == get_defaults("T20")["pitch_profiles"]["Green"]["wicket_factors"]["Medium"]
            # ...without discarding the user's own edit.
            assert wf["Fast"] == 1.3


# ──────────────────────────── Per-format storage ───────────────────────────

class TestFormatIsolation:
    def test_saving_one_format_leaves_the_other_untouched(self, app, regular_user):
        with app.app_context():
            t20 = get_defaults("T20", mutable=True)
            t20["blending"]["pitch_weight"] = 0.75
            t20["blending"]["skill_weight"] = 0.25
            assert save_user_config(regular_user.id, t20, "T20")[0]

            lista = get_defaults("ListA", mutable=True)
            lista["pitch_profiles"]["Hard"]["run_factor"] = 1.44
            assert save_user_config(regular_user.id, lista, "ListA")[0]

            assert get_effective_config(regular_user.id, "T20")["blending"]["pitch_weight"] == 0.75
            assert get_effective_config(regular_user.id, "ListA")["pitch_profiles"]["Hard"]["run_factor"] == 1.44
            # The T20 block has no ListA-only keys leaking into it.
            assert "scoring_matrices" not in get_effective_config(regular_user.id, "T20")

    def test_one_row_per_user_and_format(self, app, regular_user):
        with app.app_context():
            for fmt in VALID_FORMATS:
                assert save_user_config(regular_user.id, get_defaults(fmt, mutable=True), fmt)[0]
            rows = UserGroundConfig.query.filter_by(user_id=regular_user.id).all()
            assert sorted(r.match_format for r in rows) == sorted(VALID_FORMATS)

    def test_reset_clears_only_the_named_format(self, app, regular_user):
        with app.app_context():
            for fmt in VALID_FORMATS:
                save_user_config(regular_user.id, get_defaults(fmt, mutable=True), fmt)
            assert reset_user_config(regular_user.id, "ListA")[0]
            assert get_user_config(regular_user.id, "ListA") is None
            assert get_user_config(regular_user.id, "T20") is not None

    def test_reset_all_formats(self, app, regular_user):
        with app.app_context():
            for fmt in VALID_FORMATS:
                save_user_config(regular_user.id, get_defaults(fmt, mutable=True), fmt)
            assert reset_user_config(regular_user.id, None)[0]
            assert UserGroundConfig.query.filter_by(user_id=regular_user.id).count() == 0

    def test_no_stored_config_returns_defaults(self, app, regular_user):
        with app.app_context():
            assert get_effective_config(regular_user.id, "T20") == get_defaults("T20")


# ───────────────────────── List A reads the config ─────────────────────────

class TestListAHonoursConfig:
    """Before this rework these assertions were impossible: List A read
    hardcoded module constants, so a user's config changed nothing."""

    def test_run_factor_comes_from_the_supplied_config(self):
        cfg = get_defaults("ListA", mutable=True)
        cfg["pitch_profiles"]["Hard"]["run_factor"] = 1.9
        assert get_lista_run_factor("Hard", config=cfg) == 1.9
        # The default is untouched by that override.
        assert get_lista_run_factor("Hard") != 1.9

    def test_custom_run_factor_reaches_the_simulation(self):
        """A user's List A run factor must change simulated outcomes.

        NOTE on what run_factor actually does here: it scales every *run*
        outcome — Dot included — and the matrix is then renormalised. That
        leaves the dot/single/four/six mix untouched and only shifts weight
        between the run outcomes as a group and Wicket/Extras. So raising it
        suppresses wickets rather than lifting the run rate. This assertion
        pins the real behaviour; see the T20 run_factor for the other meaning.
        """
        import random

        from engine.format_config import get_format

        fmt = get_format("ListA")
        batter = {"name": "A", "batting_rating": 75, "batting_hand": "Right"}
        bowler = {"name": "P", "bowling_rating": 70, "fielding_rating": 65,
                  "bowling_hand": "Right", "bowling_type": "Fast"}

        def wickets_for(run_factor):
            cfg = get_defaults("ListA", mutable=True)
            cfg["pitch_profiles"]["Hard"]["run_factor"] = run_factor
            wickets = 0
            random.seed(20260815)
            for _ in range(3000):
                res = bo.calculate_outcome(
                    batter=batter, bowler=bowler, pitch="Hard",
                    streak={"boundaries": 0}, over_number=20, batter_runs=10,
                    innings=1, balls_faced=12, format_config=fmt,
                    ground_config_override=cfg,
                )
                wickets += 1 if res.get("batter_out") else 0
            return wickets

        assert wickets_for(1.6) < wickets_for(0.6)

    def test_lista_dew_window_is_configurable(self):
        weights = {"Dot": 0.3, "Single": 0.3, "Four": 0.2, "Wicket": 0.1, "Extras": 0.1}
        from engine.format_config import get_format
        fmt = get_format("ListA")

        cfg = get_defaults("ListA", mutable=True)
        cfg["dew"]["start_over"] = 5
        cfg["dew"]["peak_over"] = 6

        # Over 10 is before the shipped default window (24) but inside the
        # custom one, so only the custom config should shift the weights.
        shifted = bo._apply_dew_factor(weights, 2, 10, True, fmt, config=cfg)
        unshifted = bo._apply_dew_factor(weights, 2, 10, True, fmt)
        assert shifted != weights
        assert unshifted == weights

    def test_lista_phase_boosts_come_from_config(self):
        from engine.format_config import get_format
        fmt = get_format("ListA")
        weights = {"Dot": 0.4, "Single": 0.3, "Four": 0.2, "Six": 0.05, "Wicket": 0.05}

        cfg = get_defaults("ListA", mutable=True)
        cfg["phase_boosts"]["middle"]["all"]["Dot"] = 2.0

        boosted = bo._apply_lista_phase_boosts(weights, 20, "Hard", 1, fmt, config=cfg)
        assert boosted["Dot"] == pytest.approx(0.8)


# ─────────────────────────── Game mode resolution ──────────────────────────

class TestGameModeResolution:
    def test_auto_applies_no_modifiers(self):
        """"auto" must leave the base matrix's proportions alone — the per-ball
        dynamic selector supplies a concrete mode instead.

        get_scoring_matrix always renormalises to 1.0, and the shipped Hard
        matrix sums to 0.993, so compare proportions rather than raw values.
        """
        cfg = get_defaults("T20", mutable=True)
        cfg["active_game_mode"] = AUTO_GAME_MODE
        base = cfg["pitch_profiles"]["Hard"]["scoring_matrix"]
        total = sum(base.values())
        got = get_scoring_matrix("Hard", config=cfg)
        assert sum(got.values()) == pytest.approx(1.0)
        for outcome, value in base.items():
            assert got[outcome] == pytest.approx(value / total, abs=1e-9)

    def test_auto_matches_the_all_ones_natural_game_mode(self):
        """"auto" is behaviourally identical to the old default, whose
        modifiers were all 1.0 — this is why switching the shipped default
        from natural_game to auto changed no existing match."""
        auto_cfg = get_defaults("T20", mutable=True)
        auto_cfg["active_game_mode"] = AUTO_GAME_MODE
        natural = get_scoring_matrix("Hard", mode_override="natural_game", config=auto_cfg)
        auto = get_scoring_matrix("Hard", config=auto_cfg)
        for outcome, value in natural.items():
            assert auto[outcome] == pytest.approx(value, abs=1e-12)

    def test_pinned_mode_applies_its_modifiers(self):
        cfg = get_defaults("T20", mutable=True)
        cfg["active_game_mode"] = "bowlers_day"
        base = cfg["pitch_profiles"]["Hard"]["scoring_matrix"]
        got = get_scoring_matrix("Hard", config=cfg)
        # bowlers_day boosts wickets and suppresses sixes.
        assert got["Wicket"] > base["Wicket"]
        assert got["Six"] < base["Six"]

    def test_resolve_game_mode_prefers_the_pin(self):
        """Match._resolve_game_mode returns the pinned mode; "auto" delegates."""
        from engine.match import Match

        stub = object.__new__(Match)
        stub.ground_config = get_defaults("T20", mutable=True)
        stub.ground_config["active_game_mode"] = "defensive"
        assert stub._resolve_game_mode() == "defensive"

        stub.ground_config["active_game_mode"] = AUTO_GAME_MODE
        stub._get_dynamic_game_mode = lambda: "sentinel_dynamic"
        assert stub._resolve_game_mode() == "sentinel_dynamic"

    def test_lista_config_has_no_game_mode_and_falls_back_to_dynamic(self):
        """List A blocks carry no active_game_mode, so the pin must not fire."""
        from engine.match import Match

        stub = object.__new__(Match)
        stub.ground_config = get_defaults("ListA", mutable=True)
        stub._get_dynamic_game_mode = lambda: "sentinel_dynamic"
        assert stub._resolve_game_mode() == "sentinel_dynamic"


# ──────────────────────────────── Validation ───────────────────────────────

class TestValidation:
    def test_rejects_t20_matrix_that_does_not_sum_to_one(self):
        cfg = get_defaults("T20", mutable=True)
        cfg["pitch_profiles"]["Hard"]["scoring_matrix"]["Six"] = 0.9
        ok, err = _validate_config(cfg, "T20")
        assert not ok and "Hard" in err

    def test_rejects_lista_phase_matrix_that_does_not_sum_to_one(self):
        cfg = get_defaults("ListA", mutable=True)
        cfg["scoring_matrices"]["death"]["Six"] = 0.9
        ok, err = _validate_config(cfg, "ListA")
        assert not ok and "death" in err

    def test_save_refuses_an_invalid_config(self, app, regular_user):
        with app.app_context():
            cfg = get_defaults("ListA", mutable=True)
            cfg["scoring_matrices"]["pp1"]["Dot"] = 0.99
            ok, _ = save_user_config(regular_user.id, cfg, "ListA")
            assert not ok
            assert get_user_config(regular_user.id, "ListA") is None


# ──────────────────────────────── Migration ────────────────────────────────

class TestMigration:
    def _v1_row(self, db, user_id, active_mode="natural_game", tweak=None):
        """Insert a pre-migration (flat, T20-shaped) config row."""
        from sqlalchemy import text
        cfg = get_defaults("T20", mutable=True)
        cfg["active_game_mode"] = active_mode
        # v1 blobs predate the Medium wicket factor.
        cfg["pitch_profiles"]["Green"]["wicket_factors"].pop("Medium", None)
        if tweak:
            tweak(cfg)
        db.session.execute(
            text("INSERT INTO user_ground_configs (user_id, match_format, config_json, updated_at) "
                 "VALUES (:u, :f, :c, CURRENT_TIMESTAMP)"),
            {"u": user_id, "f": "T20", "c": json.dumps(cfg)},
        )
        db.session.commit()

    def test_normalises_modes_and_drops_no_op_rows(self, app, regular_user):
        from app import db
        from migrations.add_ground_config_formats import run_migration

        with app.app_context():
            # A row identical to defaults except for the unchosen mode default.
            self._v1_row(db, regular_user.id)
            run_migration(db, app)
            # Contributes nothing over defaults → removed entirely.
            assert get_user_config(regular_user.id, "T20") is None

    def test_keeps_a_deliberate_mode_as_a_pin(self, app, regular_user):
        from app import db
        from migrations.add_ground_config_formats import run_migration

        with app.app_context():
            self._v1_row(db, regular_user.id, active_mode="defensive")
            run_migration(db, app)
            stored = get_user_config(regular_user.id, "T20")
            assert stored is not None
            assert stored["active_game_mode"] == "defensive"

    def test_preserves_real_customisation(self, app, regular_user):
        from app import db
        from migrations.add_ground_config_formats import run_migration

        with app.app_context():
            self._v1_row(
                db, regular_user.id,
                tweak=lambda c: c["blending"].update(pitch_weight=0.5, skill_weight=0.5),
            )
            run_migration(db, app)
            stored = get_user_config(regular_user.id, "T20")
            assert stored is not None
            assert stored["blending"]["pitch_weight"] == 0.5
            # 'natural_game' was never a real choice — it becomes dynamic.
            assert stored["active_game_mode"] == AUTO_GAME_MODE

    def test_is_idempotent(self, app, regular_user):
        from app import db
        from migrations.add_ground_config_formats import run_migration

        with app.app_context():
            self._v1_row(
                db, regular_user.id,
                tweak=lambda c: c["blending"].update(pitch_weight=0.5, skill_weight=0.5),
            )
            run_migration(db, app)
            first = get_user_config(regular_user.id, "T20")
            run_migration(db, app)
            assert get_user_config(regular_user.id, "T20") == first


# ─────────────────────── GSME baseline coupling (Phase 6) ──────────────────

class TestRRRBaselineCoupling:
    """GSME normalises required run rate against a per-pitch "neutral" RPO.
    That table is fixed, so before this coupling a user who made a pitch more
    run-friendly was still judged against the stock number."""

    def test_stock_config_returns_the_stock_baseline(self):
        from engine.format_config import get_format
        from engine.ground_config import get_rrr_baseline

        fmt = get_format("T20")
        for pitch in ("Green", "Hard", "Flat"):
            assert get_rrr_baseline(pitch, fmt) == fmt.rrr_baseline[pitch]

    def test_baseline_scales_with_a_custom_run_factor(self):
        from engine.format_config import get_format
        from engine.ground_config import get_rrr_baseline

        fmt = get_format("T20")
        cfg = get_defaults("T20", mutable=True)
        stock = cfg["pitch_profiles"]["Flat"]["run_factor"]
        cfg["pitch_profiles"]["Flat"]["run_factor"] = stock * 2

        assert get_rrr_baseline("Flat", fmt, config=cfg) == pytest.approx(
            fmt.rrr_baseline["Flat"] * 2
        )

    def test_lista_uses_the_lista_run_factor(self):
        from engine.format_config import get_format
        from engine.ground_config import get_rrr_baseline

        fmt = get_format("ListA")
        cfg = get_defaults("ListA", mutable=True)
        stock = cfg["pitch_profiles"]["Hard"]["run_factor"]
        cfg["pitch_profiles"]["Hard"]["run_factor"] = stock * 1.5

        assert get_rrr_baseline("Hard", fmt, config=cfg) == pytest.approx(
            fmt.rrr_baseline["Hard"] * 1.5
        )

    def test_unknown_pitch_falls_through(self):
        from engine.format_config import get_format
        from engine.ground_config import get_rrr_baseline

        assert get_rrr_baseline("Moon", get_format("T20")) is None

    def test_gsme_vector_uses_the_adjusted_baseline(self):
        """A doubled run factor must halve the required-aggression index."""
        from engine.format_config import get_format
        from engine.game_state_engine import compute_game_state_vector

        fmt = get_format("T20")
        cfg = get_defaults("T20", mutable=True)
        cfg["pitch_profiles"]["Flat"]["run_factor"] *= 2

        kwargs = dict(ball_history=[], score=60, current_over=10, current_ball=0,
                      wickets=3, innings=2, target=180, pitch="Flat",
                      format_config=fmt)

        stock = compute_game_state_vector(**kwargs)
        tuned = compute_game_state_vector(**kwargs, ground_config=cfg)
        assert tuned["required_aggression"] == pytest.approx(
            stock["required_aggression"] / 2
        )
