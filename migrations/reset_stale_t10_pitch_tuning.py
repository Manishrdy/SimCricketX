"""
Reset Stale T10 Pitch Tuning Migration
======================================

The T10 aggression pass retuned the Green, Dry, Flat and Dead scoring
matrices, Dry's wicket_factors and every phase_boosts value in the T10 block
of config/ground_conditions_defaults.yaml. Stored user configs deep-merge OVER
the factory defaults, so any user carrying a snapshot of the old numbers would
keep the old — and much quieter, much more collapse-prone — T10 forever, and
never see the recalibration at all.

Almost nobody chose those numbers. `/ground-conditions/mode` builds its payload
from get_effective_config(), which is the defaults already merged in, then
saves the whole thing — so merely picking a game mode on the T10 page froze a
complete snapshot of every pitch matrix into that user's row. Same for any UI
save that posts the full config back.

So this strips a retuned key ONLY when its stored value deep-equals the value
we used to ship. That is the precise test for "this was written by a snapshot,
not by a person": a user who deliberately tuned their Dead matrix has something
different from the old default and keeps it, while a user carrying an
involuntary copy drops back to inheriting from the YAML.

Steps (idempotent — re-running finds nothing left to strip):
  1. For each stored T10 row, strip every path in LEGACY_T10_VALUES whose
     stored value still equals the pre-retune default.
  2. Prune dict branches left empty by step 1.
  3. Delete rows that no longer add anything over the factory defaults — an
     absent row already means "use defaults".

Rows for other formats are not touched: a T20 or List A blob that happens to
share a number with the old T10 block is not evidence of anything, and those
formats have their own migrations.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from utils.exception_tracker import log_exception
from migrations.reset_stale_t20_pitch_tuning import (
    TABLE, _lookup, _matches_legacy, _prune_empty, _table_exists, _MISSING,
)

# Frozen snapshot of what shipped BEFORE the T10 aggression pass. Never update
# these to track the YAML — recognising the old values after the YAML has moved
# on is the entire job.
OLD_SCORING_MATRIX = {
    "Green": {"Dot": 0.29513092, "Single": 0.37376343, "Double": 0.09140954,
              "Three": 0.00507831, "Four": 0.0987223, "Six": 0.03717321,
              "Wicket": 0.05809584, "Extras": 0.04062646},
    "Dry":   {"Dot": 0.28670946, "Single": 0.37315325, "Double": 0.09328831,
              "Three": 0.00811203, "Four": 0.10221154, "Six": 0.03958669,
              "Wicket": 0.05130857, "Extras": 0.04563015},
    "Flat":  {"Dot": 0.22998489, "Single": 0.33789156, "Double": 0.13913182,
              "Three": 0.00795039, "Four": 0.12879631, "Six": 0.08002067,
              "Wicket": 0.05137939, "Extras": 0.02484497},
    "Dead":  {"Dot": 0.21009314, "Single": 0.32526736, "Double": 0.14292051,
              "Three": 0.00492829, "Four": 0.15849391, "Six": 0.09018777,
              "Wicket": 0.03361096, "Extras": 0.03449805},
}

OLD_DRY_WICKET_FACTORS = {
    "Leg spin": 1.62, "Wrist spin": 1.56, "Off spin": 1.5,
    "Finger spin": 1.38, "default": 0.34,
}

# Hard's matrix, every run_factor, the game_modes block and blending are
# unchanged by this pass and are deliberately absent from the table below.
LEGACY_T10_VALUES = (
    [(["pitch_profiles", pitch, "scoring_matrix"], matrix)
     for pitch, matrix in OLD_SCORING_MATRIX.items()]
    + [
        (["pitch_profiles", "Dry", "wicket_factors"], OLD_DRY_WICKET_FACTORS),
        (["phase_boosts", "powerplay", "boundary_multiplier"], 1.25),
        (["phase_boosts", "death_overs", "boundary_boost_bowling_pitch"], 1.8),
        (["phase_boosts", "death_overs", "wicket_boost"], 1.6),
        (["phase_boosts", "second_innings_death", "scoring_boost"], 1.15),
        (["phase_boosts", "second_innings_death", "wicket_boost"], 1.1),
    ]
)


def _strip_stale_t10(cfg):
    """Remove involuntarily-snapshotted T10 values. Returns descriptions."""
    stripped = []
    for path, old_value in LEGACY_T10_VALUES:
        stored = _lookup(cfg, path)
        if stored is _MISSING or not _matches_legacy(stored, old_value):
            continue
        parent = _lookup(cfg, path[:-1])
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
            stripped.append(".".join(path[1:]) if len(path) > 1 else path[0])

    for path, _ in LEGACY_T10_VALUES:
        for depth in range(len(path) - 1, 0, -1):
            _prune_empty(cfg, path[:depth])
    return stripped


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        try:
            conn.rollback()
        except Exception:
            pass

        try:
            if not _table_exists(conn, TABLE):
                conn.commit()
                print(f"[Migration] reset_stale_t10_pitch_tuning: {TABLE} absent — "
                      "nothing to do.")
                return

            rows = conn.execute(text(
                f"SELECT id, user_id, match_format, config_json FROM {TABLE}"
            )).fetchall()

            from engine.ground_config import _deep_merge, get_defaults, normalise_format

            try:
                defaults = get_defaults("T10", mutable=True)
            except Exception as e:
                log_exception(e)
                print("[Migration] reset_stale_t10_pitch_tuning: T10 defaults "
                      f"unavailable ({e}) — skipping.")
                return

            cleaned = deleted = inspected = 0
            for row_id, user_id, fmt, blob in rows:
                # A blank stamp predates per-format storage and is T20 by
                # definition — see add_ground_config_formats. Never T10.
                if normalise_format(fmt or "T20") != "T10":
                    continue
                inspected += 1

                try:
                    cfg = json.loads(blob) if isinstance(blob, str) else blob
                except (TypeError, ValueError):
                    print(f"[Migration]   row {row_id} ({user_id}): unparseable "
                          "config_json — left as-is.")
                    continue
                if not isinstance(cfg, dict):
                    continue

                stripped = _strip_stale_t10(cfg)
                if not stripped:
                    continue

                # `version` is a document marker, not part of a format block, so
                # it can never compare equal and must be excluded from the probe.
                probe = {k: v for k, v in cfg.items() if k != "version"}
                if _deep_merge(defaults, probe) == defaults:
                    conn.execute(text(f"DELETE FROM {TABLE} WHERE id=:i"), {"i": row_id})
                    deleted += 1
                    print(f"[Migration]   row {row_id} ({user_id}, T10): stripped "
                          f"{', '.join(stripped)} — row now a no-op, removed.")
                    continue

                conn.execute(
                    text(f"UPDATE {TABLE} SET config_json=:c WHERE id=:i"),
                    {"c": json.dumps(cfg), "i": row_id},
                )
                cleaned += 1
                print(f"[Migration]   row {row_id} ({user_id}, T10): stripped "
                      f"{', '.join(stripped)}.")

            conn.commit()
            print(f"[Migration] reset_stale_t10_pitch_tuning: {cleaned} blob(s) "
                  f"cleaned, {deleted} no-op row(s) removed, {inspected} T10 "
                  f"row(s) inspected of {len(rows)} total.")

        except Exception as e:
            log_exception(e)
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] reset_stale_t10_pitch_tuning FAILED: {e}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    os.environ.setdefault("SIMCRICKETX_PRECHECK_RUNNING", "1")
    from app import create_app, db as _db
    run_migration(_db, create_app())
