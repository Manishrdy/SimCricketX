"""
Reset Flat/Dead FC Tuning Migration
===================================

The 2026-09-04 First-Class ladder change moved two surfaces so that a
first-innings total on the board reads 400+ on Flat and 500-600 on Dead,
instead of Flat and Dead producing near-identical scores (429 vs 413):

  * Flat  — scoring_matrix +5% on the scoring outcomes (Dot absorbing),
            wicket_factors_start/end eased about 6%.
  * Dead  — scoring_matrix +10% on the scoring outcomes, and its wicket
            factors dropped hard at the START of the match (0.825 -> 0.680).
            Dead had been taking wickets MORE easily than Flat on a fresh
            pitch, which is backwards for the deadest surface in the game;
            its late spin assistance is kept so long innings still end.

Green, Dry and Hard are untouched, as is every other block in the file.
The matching declaration-bar change (Flat's pitch_par_factor 1.15 -> 1.22)
lives in engine/format_config.py, which is code and reaches everyone
without a migration.

Stored user configs deep-merge OVER the factory defaults, so a user whose
blob happens to carry the pre-change Flat/Dead profiles would keep the old
values forever and never see any of this — the trap documented in
reset_stale_t20_pitch_tuning and reset_slow_fc_scoring. Same test as those:
strip a key ONLY when its stored value deep-equals the value we used to
ship, so a hand-tuned Flat matrix survives and an involuntary snapshot
drops back to inheriting from the YAML.

Idempotent — re-running finds nothing left to strip.
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

# Frozen snapshot of what shipped for FC Flat/Dead BETWEEN the 2026-08-30
# scoring acceleration and this ladder change. Never update these to track
# the YAML — recognising the OLD values after the YAML has moved on is the
# entire job.
PREVIOUS_FLAT_DEAD = {
    "Flat": {
        "scoring_matrix": {"Dot": 0.64153, "Single": 0.21794, "Double": 0.05613,
                           "Three": 0.00661, "Four": 0.05064, "Six": 0.00385,
                           "Wicket": 0.01445, "Extras": 0.00885},
        "wicket_factors_start": {"Fast": 0.834, "default": 0.792},
        "wicket_factors_end": {"Off spin": 0.981, "Leg spin": 0.981,
                               "default": 0.887},
    },
    "Dead": {
        "scoring_matrix": {"Dot": 0.63150, "Single": 0.22092, "Double": 0.05935,
                           "Three": 0.00660, "Four": 0.05496, "Six": 0.00440,
                           "Wicket": 0.01443, "Extras": 0.00785},
        "wicket_factors_start": {"default": 0.825},
        "wicket_factors_end": {"Off spin": 1.008, "Leg spin": 1.008,
                               "default": 0.845},
    },
}

PREVIOUS_VALUES = [(["pitch_profiles", pitch, key], value)
                   for pitch, block in PREVIOUS_FLAT_DEAD.items()
                   for key, value in block.items()]


def _strip_previous(cfg):
    """Remove involuntarily-snapshotted pre-ladder Flat/Dead values.
    Returns descriptions of what was stripped."""
    stripped = []
    for path, old_value in PREVIOUS_VALUES:
        stored = _lookup(cfg, path)
        if stored is _MISSING or not _matches_legacy(stored, old_value):
            continue
        parent = _lookup(cfg, path[:-1])
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
            stripped.append(".".join(path[1:]))
    for path, _ in PREVIOUS_VALUES:
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
                print(f"[Migration] reset_flat_dead_fc_tuning: {TABLE} absent — "
                      "nothing to do.")
                return

            rows = conn.execute(text(
                f"SELECT id, user_id, match_format, config_json FROM {TABLE} "
                "WHERE match_format = 'FC'"
            )).fetchall()

            from engine.ground_config import _deep_merge, get_defaults

            defaults = get_defaults("FC", mutable=True)
            cleaned = deleted = 0
            for row_id, user_id, _fmt, blob in rows:
                try:
                    cfg = json.loads(blob) if isinstance(blob, str) else blob
                except (TypeError, ValueError):
                    print(f"[Migration]   row {row_id} ({user_id}): unparseable "
                          "config_json — left as-is.")
                    continue
                if not isinstance(cfg, dict):
                    continue

                stripped = _strip_previous(cfg)
                if not stripped:
                    continue

                probe = {k: v for k, v in cfg.items() if k != "version"}
                if _deep_merge(defaults, probe) == defaults:
                    conn.execute(text(f"DELETE FROM {TABLE} WHERE id=:i"), {"i": row_id})
                    deleted += 1
                    print(f"[Migration]   row {row_id} ({user_id}, FC): stripped "
                          f"{', '.join(stripped)} — row now a no-op, removed.")
                    continue

                conn.execute(
                    text(f"UPDATE {TABLE} SET config_json=:c WHERE id=:i"),
                    {"c": json.dumps(cfg), "i": row_id},
                )
                cleaned += 1
                print(f"[Migration]   row {row_id} ({user_id}, FC): stripped "
                      f"{', '.join(stripped)}.")

            conn.commit()
            print(f"[Migration] reset_flat_dead_fc_tuning: {cleaned} blob(s) "
                  f"cleaned, {deleted} no-op row(s) removed, {len(rows)} inspected.")

        except Exception as e:
            log_exception(e)
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] reset_flat_dead_fc_tuning FAILED: {e}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    os.environ.setdefault("SIMCRICKETX_PRECHECK_RUNNING", "1")
    from app import app as flask_app
    from database import db as _db
    run_migration(_db, flask_app)
