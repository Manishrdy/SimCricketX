"""
FC Flat wicket-factor retune (2026-09)
======================================

Raises the FC wicket factors for Flat in stored user ground
configs, so the retune in `config/ground_conditions_defaults.yaml` actually
reaches existing users.

Why a migration is needed at all: a stored `user_ground_configs` blob is
deep-merged OVER the factory defaults, so any key a user's blob already
carries permanently shadows the YAML. A user who has opened the Ground
Conditions page even once has a blob, and without this they would keep the
old numbers forever — the surface that produced a 600+ innings once every
five, with a 973 as its worst case.

What it changes: only Flat's `wicket_factors_start` / `_end`, and only where
the stored value still equals the OLD default. Dead was deliberately left
alone — see the retune note in config/ground_conditions_defaults.yaml.
A value the user deliberately changed is left alone — this is a defaults
correction, not a reset of anyone's tuning. Scoring matrices and run factors
are untouched: those two surfaces keep their identity through run rate.

Idempotent: re-running finds no old values left and does nothing.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from utils.exception_tracker import log_exception

TABLE = "user_ground_configs"

# (pitch, block, key): (old default, new default)
RETUNE = {
    ("Flat", "wicket_factors_start", "Fast"): (0.784, 0.845),
    ("Flat", "wicket_factors_start", "default"): (0.744, 0.800),
    ("Flat", "wicket_factors_end", "Off spin"): (0.922, 0.995),
    ("Flat", "wicket_factors_end", "Leg spin"): (0.922, 0.995),
    ("Flat", "wicket_factors_end", "default"): (0.834, 0.900),
}

TOLERANCE = 1e-6


def _table_exists(conn, table):
    return conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
    ), {"t": table}).fetchone() is not None


def _column_exists(conn, table, column):
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def _retune_blob(blob):
    """Update stale FC wicket factors in place. Returns True if changed."""
    # config_json holds the FLAT format block (the row is already keyed by
    # match_format), so pitch_profiles sits at the top level — not under
    # formats.FC as it does in the YAML document.
    profiles = blob.get("pitch_profiles")
    if not isinstance(profiles, dict):
        return False

    changed = False
    for (pitch, block, key), (old, new) in RETUNE.items():
        factors = profiles.get(pitch, {}).get(block)
        if not isinstance(factors, dict) or key not in factors:
            continue
        try:
            current = float(factors[key])
        except (TypeError, ValueError):
            continue
        # Only correct values still sitting on the old default. Anything the
        # user actually tuned is theirs.
        if abs(current - old) <= TOLERANCE:
            factors[key] = new
            changed = True
    return changed


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
                print(f"[Migration] fc_flat_dead_wicket_retune: {TABLE} absent — nothing to do.")
                return
            if not _column_exists(conn, TABLE, "match_format"):
                conn.commit()
                print("[Migration] fc_flat_dead_wicket_retune: pre-format schema — "
                      "run add_ground_config_formats first.")
                return

            rows = conn.execute(text(
                f"SELECT id, config_json FROM {TABLE} WHERE match_format='FC'"
            )).fetchall()

            updated = 0
            for row_id, raw in rows:
                if not raw:
                    continue
                try:
                    blob = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(blob, dict) or not _retune_blob(blob):
                    continue
                conn.execute(
                    text(f"UPDATE {TABLE} SET config_json=:c WHERE id=:i"),
                    {"c": json.dumps(blob), "i": row_id},
                )
                updated += 1

            conn.commit()
            print(f"[Migration] fc_flat_dead_wicket_retune: {updated} of "
                  f"{len(rows)} FC config(s) updated.")
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log_exception(e)
            print(f"[Migration] fc_flat_dead_wicket_retune: FAILED ({e})")
        finally:
            conn.close()
