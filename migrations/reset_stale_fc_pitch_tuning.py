"""
Reset Stale FC Pitch Tuning Migration
=====================================

The 2026-08-30 First-Class recalibration rebuilt the whole FC block in
config/ground_conditions_defaults.yaml: every pitch's scoring_matrix (the
run economy — dot rate up from ~0.55 to ~0.70, singles down, boundaries
rarer), every run_factor (now a flat 1.0, since the per-pitch wicket rate is
carried by the wear/style factors instead), every wicket_factors_start /
wicket_factors_end table, and the blending weights (FC's skill contest was
being damped to near-irrelevance by a constant pitch term).

Stored user configs are deep-merged OVER the factory defaults, so a user
whose blob happens to contain the old numbers would keep the old — and much
higher — FC scoring forever and never see the recalibration at all. This is
the same trap reset_stale_t20_pitch_tuning was written for; see that module
for the full reasoning, whose helpers this one reuses rather than
duplicating.

The test is unchanged: strip a recalibrated key ONLY when its stored value
deep-equals the value we used to ship. A user who deliberately tuned their
FC Flat matrix has something different from the old default and keeps it; a
user carrying an involuntary snapshot drops back to inheriting from the YAML.

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

# Frozen snapshot of what shipped BEFORE the 2026-08-30 FC recalibration.
# Never update these to track the YAML — recognising the OLD values after the
# YAML has moved on is the entire job.
OLD_FC_SCORING_MATRIX = {
    "Green": {"Dot": 0.570, "Single": 0.300, "Double": 0.033, "Three": 0.006,
              "Four": 0.050, "Six": 0.004, "Wicket": 0.022, "Extras": 0.015},
    "Dry":   {"Dot": 0.565, "Single": 0.300, "Double": 0.033, "Three": 0.006,
              "Four": 0.050, "Six": 0.004, "Wicket": 0.022, "Extras": 0.020},
    "Hard":  {"Dot": 0.560, "Single": 0.310, "Double": 0.035, "Three": 0.006,
              "Four": 0.055, "Six": 0.005, "Wicket": 0.019, "Extras": 0.010},
    "Flat":  {"Dot": 0.545, "Single": 0.315, "Double": 0.038, "Three": 0.006,
              "Four": 0.065, "Six": 0.007, "Wicket": 0.015, "Extras": 0.009},
    "Dead":  {"Dot": 0.530, "Single": 0.320, "Double": 0.042, "Three": 0.006,
              "Four": 0.075, "Six": 0.009, "Wicket": 0.010, "Extras": 0.008},
}

OLD_FC_RUN_FACTORS = {
    "Green": 0.90, "Dry": 0.92, "Hard": 1.00, "Flat": 1.08, "Dead": 1.15,
}

OLD_FC_WICKET_FACTORS_START = {
    "Green": {"Fast": 1.55, "Fast-medium": 1.30, "Medium-fast": 1.15,
              "Medium": 1.00, "default": 0.55},
    "Dry":   {"Leg spin": 1.20, "Off spin": 1.15, "Fast": 1.05,
              "Medium": 0.95, "default": 0.90},
    "Hard":  {"Fast": 1.10, "Medium": 0.95, "default": 0.95},
    "Flat":  {"default": 0.85},
    "Dead":  {"default": 0.55},
}

OLD_FC_WICKET_FACTORS_END = {
    "Green": {"Fast": 1.15, "Fast-medium": 1.05, "Medium-fast": 1.00,
              "Medium": 0.95, "Off spin": 1.15, "Leg spin": 1.15, "default": 0.80},
    "Dry":   {"Leg spin": 2.10, "Off spin": 1.95, "Finger spin": 1.80,
              "Wrist spin": 1.85, "Fast": 0.70, "Medium": 0.65, "default": 0.55},
    "Hard":  {"Fast": 1.05, "Medium": 0.95, "Off spin": 1.10,
              "Leg spin": 1.10, "default": 0.95},
    "Flat":  {"Off spin": 1.05, "Leg spin": 1.05, "default": 0.85},
    "Dead":  {"Off spin": 1.15, "Leg spin": 1.15, "default": 0.60},
}

OLD_FC_BLENDING = {"pitch_weight": 0.6, "skill_weight": 0.4}

FC_LEGACY_VALUES = (
    [(["pitch_profiles", p, "scoring_matrix"], m)
     for p, m in OLD_FC_SCORING_MATRIX.items()]
    + [(["pitch_profiles", p, "run_factor"], v)
       for p, v in OLD_FC_RUN_FACTORS.items()]
    + [(["pitch_profiles", p, "wicket_factors_start"], t)
       for p, t in OLD_FC_WICKET_FACTORS_START.items()]
    + [(["pitch_profiles", p, "wicket_factors_end"], t)
       for p, t in OLD_FC_WICKET_FACTORS_END.items()]
    + [(["blending"], OLD_FC_BLENDING)]
)


def _strip_stale_fc(cfg):
    """Remove involuntarily-snapshotted FC values. Returns descriptions."""
    stripped = []
    for path, old_value in FC_LEGACY_VALUES:
        stored = _lookup(cfg, path)
        if stored is _MISSING or not _matches_legacy(stored, old_value):
            continue
        parent = _lookup(cfg, path[:-1]) if len(path) > 1 else cfg
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
            stripped.append(".".join(path[1:]) if len(path) > 1 else path[0])
    for path, _ in FC_LEGACY_VALUES:
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
                print(f"[Migration] reset_stale_fc_pitch_tuning: {TABLE} absent — "
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

                stripped = _strip_stale_fc(cfg)
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
            print(f"[Migration] reset_stale_fc_pitch_tuning: {cleaned} blob(s) "
                  f"cleaned, {deleted} no-op row(s) removed, {len(rows)} inspected.")

        except Exception as e:
            log_exception(e)
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] reset_stale_fc_pitch_tuning FAILED: {e}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    os.environ.setdefault("SIMCRICKETX_PRECHECK_RUNNING", "1")
    from app import app as flask_app
    from database import db as _db
    run_migration(_db, flask_app)
