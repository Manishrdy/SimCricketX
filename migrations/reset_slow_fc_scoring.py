"""
Reset Slow FC Scoring Migration
===============================

The 2026-08-30 First-Class scoring acceleration moved every FC pitch's
scoring_matrix: about 12% more probability mass on the scoring outcomes
(Single/Double/Three/Four/Six), about 5% more on Wicket, and Dot absorbing
the difference. The run rate goes from ~3.10 to ~3.40 an over without the
totals or the batting averages running away with it.

Stored user configs are deep-merged OVER the factory defaults, so a user
whose blob happens to contain the pre-acceleration matrices would keep the
old — and slower — FC scoring forever and never see the change at all. That
is the same trap reset_stale_t20_pitch_tuning and reset_stale_fc_pitch_tuning
were written for; this reuses their helpers rather than duplicating them.

The test is unchanged from those: strip a key ONLY when its stored value
deep-equals the value we used to ship. A user who deliberately tuned their FC
Flat matrix has something different from the old default and keeps it; a user
carrying an involuntary snapshot drops back to inheriting from the YAML.

Only scoring_matrix moved in this pass — run_factor, the wicket-factor tables
and the blending weights are all untouched, so nothing else is stripped here.

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

# Frozen snapshot of what shipped BETWEEN the 2026-08-30 FC recalibration and
# the acceleration that followed it. Never update these to track the YAML —
# recognising the OLD values after the YAML has moved on is the entire job.
SLOW_FC_SCORING_MATRIX = {
    "Green": {"Dot": 0.70617, "Single": 0.18074, "Double": 0.04346,
              "Three": 0.00494, "Four": 0.03556, "Six": 0.00198,
              "Wicket": 0.01383, "Extras": 0.01333},
    "Dry":   {"Dot": 0.70054, "Single": 0.18352, "Double": 0.04440,
              "Three": 0.00493, "Four": 0.03749, "Six": 0.00247,
              "Wicket": 0.01381, "Extras": 0.01283},
    "Hard":  {"Dot": 0.68932, "Single": 0.19005, "Double": 0.04727,
              "Three": 0.00591, "Four": 0.04136, "Six": 0.00295,
              "Wicket": 0.01379, "Extras": 0.00935},
    "Flat":  {"Dot": 0.67813, "Single": 0.19459, "Double": 0.05012,
              "Three": 0.00590, "Four": 0.04521, "Six": 0.00344,
              "Wicket": 0.01376, "Extras": 0.00885},
    "Dead":  {"Dot": 0.66928, "Single": 0.19725, "Double": 0.05299,
              "Three": 0.00589, "Four": 0.04907, "Six": 0.00393,
              "Wicket": 0.01374, "Extras": 0.00785},
}

SLOW_FC_VALUES = [(["pitch_profiles", p, "scoring_matrix"], m)
                  for p, m in SLOW_FC_SCORING_MATRIX.items()]


def _strip_slow_fc(cfg):
    """Remove involuntarily-snapshotted pre-acceleration matrices.
    Returns descriptions of what was stripped."""
    stripped = []
    for path, old_value in SLOW_FC_VALUES:
        stored = _lookup(cfg, path)
        if stored is _MISSING or not _matches_legacy(stored, old_value):
            continue
        parent = _lookup(cfg, path[:-1])
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
            stripped.append(".".join(path[1:]))
    for path, _ in SLOW_FC_VALUES:
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
                print(f"[Migration] reset_slow_fc_scoring: {TABLE} absent — "
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

                stripped = _strip_slow_fc(cfg)
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
            print(f"[Migration] reset_slow_fc_scoring: {cleaned} blob(s) "
                  f"cleaned, {deleted} no-op row(s) removed, {len(rows)} inspected.")

        except Exception as e:
            log_exception(e)
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] reset_slow_fc_scoring FAILED: {e}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    os.environ.setdefault("SIMCRICKETX_PRECHECK_RUNNING", "1")
    from app import app as flask_app
    from database import db as _db
    run_migration(_db, flask_app)
