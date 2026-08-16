"""
Reset Stale T20 Pitch Tuning Migration
=====================================

The 2026-08-16 T20 recalibration retuned every pitch's scoring_matrix, Dry's
wicket_factors, the death-over boundary boost and the flat_track_bully game
mode (see config/ground_conditions_defaults.yaml). Stored user configs are
deep-merged OVER the factory defaults, so any user whose blob happens to
contain the old numbers would keep the old — and much higher — scoring
forever, and never see the recalibration at all.

Almost nobody chose those numbers. `/ground-conditions/mode` builds its payload
from get_effective_config(), which is the defaults already merged in, then
saves the whole thing — so merely picking a game mode froze a complete snapshot
of every pitch matrix into that user's row. Same for any UI save that posts the
full config back.

So this migration strips a recalibrated key ONLY when its stored value deep-
equals the value we used to ship. That is the precise test for "this was
written by a snapshot, not by a person": a user who deliberately tuned their
Flat matrix has something different from the old default and keeps it, while a
user carrying an involuntary copy drops back to inheriting from the YAML.

Steps (idempotent — re-running finds nothing left to strip):
  1. For each stored T20 row, drop pitch_profiles.<pitch>.scoring_matrix,
     pitch_profiles.Dry.wicket_factors, phase_boosts.death_overs
     .boundary_boost_batting_pitch and game_modes.flat_track_bully.modifiers
     where the stored value matches the pre-recalibration default.
  2. Prune dict branches left empty by step 1, so the blob does not keep an
     empty `pitch_profiles: {}` that means nothing.
  3. Delete rows that no longer add anything over the factory defaults — an
     absent row already means "use defaults".

ListA rows are untouched: the recalibration was T20-only.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from utils.exception_tracker import log_exception

TABLE = "user_ground_configs"

# Frozen snapshot of what shipped BEFORE the 2026-08-16 recalibration. Never
# update these to track the YAML — the whole point is to recognise the old
# values after the YAML has moved on.
OLD_SCORING_MATRIX = {
    "Green": {"Dot": 0.35, "Single": 0.365, "Double": 0.085, "Three": 0.005,
              "Four": 0.07, "Six": 0.025, "Wicket": 0.065, "Extras": 0.04},
    "Dry":   {"Dot": 0.33, "Single": 0.367, "Double": 0.088, "Three": 0.008,
              "Four": 0.07, "Six": 0.03, "Wicket": 0.06, "Extras": 0.04},
    "Hard":  {"Dot": 0.28, "Single": 0.345, "Double": 0.12, "Three": 0.008,
              "Four": 0.10, "Six": 0.06, "Wicket": 0.04, "Extras": 0.04},
    "Flat":  {"Dot": 0.24, "Single": 0.335, "Double": 0.14, "Three": 0.008,
              "Four": 0.13, "Six": 0.08, "Wicket": 0.055, "Extras": 0.025},
    "Dead":  {"Dot": 0.20, "Single": 0.325, "Double": 0.145, "Three": 0.005,
              "Four": 0.17, "Six": 0.09, "Wicket": 0.035, "Extras": 0.025},
}

OLD_DRY_WICKET_FACTORS = {
    "Leg spin": 1.40, "Wrist spin": 1.35, "Off spin": 1.30,
    "Finger spin": 1.20, "default": 0.60,
}

OLD_DEATH_BOUNDARY_BOOST = 2.2

OLD_BULLY_MODIFIERS = {
    "dot_mult": 0.80, "single_mult": 0.86, "double_mult": 0.99,
    "three_mult": 0.90, "four_mult": 1.22, "six_mult": 1.35,
    "wicket_mult": 0.69, "extras_mult": 0.81,
}

_EPSILON = 1e-9


def _table_exists(conn, table):
    return conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
    ), {"t": table}).fetchone() is not None


def _num_eq(a, b):
    """Compare two numbers tolerantly — JSON round-trips float noise."""
    try:
        return abs(float(a) - float(b)) < _EPSILON
    except (TypeError, ValueError):
        return False


def _mapping_matches(stored, expected):
    """True when *stored* is exactly *expected*, comparing numbers tolerantly."""
    if not isinstance(stored, dict) or set(stored) != set(expected):
        return False
    return all(_num_eq(stored[k], expected[k]) for k in expected)


def _prune_empty(cfg, path):
    """Drop empty dicts left behind along *path*, deepest first."""
    for depth in range(len(path), 0, -1):
        node, parent = cfg, None
        for key in path[:depth]:
            parent, node = node, node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and not node and isinstance(parent, dict):
            parent.pop(path[depth - 1], None)


def _strip_stale(cfg):
    """Remove involuntarily-snapshotted values. Returns a list of descriptions."""
    stripped = []

    profiles = cfg.get("pitch_profiles")
    if isinstance(profiles, dict):
        for pitch, old_matrix in OLD_SCORING_MATRIX.items():
            profile = profiles.get(pitch)
            if not isinstance(profile, dict):
                continue
            if _mapping_matches(profile.get("scoring_matrix"), old_matrix):
                profile.pop("scoring_matrix", None)
                stripped.append(f"{pitch}.scoring_matrix")
        dry = profiles.get("Dry")
        if isinstance(dry, dict) and _mapping_matches(
                dry.get("wicket_factors"), OLD_DRY_WICKET_FACTORS):
            dry.pop("wicket_factors", None)
            stripped.append("Dry.wicket_factors")
        for pitch in list(profiles):
            if isinstance(profiles[pitch], dict) and not profiles[pitch]:
                profiles.pop(pitch)
        _prune_empty(cfg, ["pitch_profiles"])

    death = (cfg.get("phase_boosts") or {}).get("death_overs")
    if isinstance(death, dict) and _num_eq(
            death.get("boundary_boost_batting_pitch"), OLD_DEATH_BOUNDARY_BOOST):
        death.pop("boundary_boost_batting_pitch", None)
        stripped.append("death_overs.boundary_boost_batting_pitch")
    _prune_empty(cfg, ["phase_boosts", "death_overs"])

    bully = (cfg.get("game_modes") or {}).get("flat_track_bully")
    if isinstance(bully, dict) and _mapping_matches(
            bully.get("modifiers"), OLD_BULLY_MODIFIERS):
        bully.pop("modifiers", None)
        stripped.append("flat_track_bully.modifiers")
    _prune_empty(cfg, ["game_modes", "flat_track_bully"])

    return stripped


def _defaults_for(match_format):
    try:
        from engine.ground_config import get_defaults
        return get_defaults(match_format, mutable=True)
    except Exception as e:
        log_exception(e)
        print(f"[Migration] reset_stale_t20_pitch_tuning: defaults unavailable ({e}) "
              "— skipping.")
        return None


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
                print(f"[Migration] reset_stale_t20_pitch_tuning: {TABLE} absent — "
                      "nothing to do.")
                return

            rows = conn.execute(text(
                f"SELECT id, user_id, match_format, config_json FROM {TABLE} "
                "WHERE match_format='T20' OR match_format IS NULL OR match_format=''"
            )).fetchall()

            from engine.ground_config import _deep_merge

            cleaned = deleted = 0
            for row_id, user_id, fmt, blob in rows:
                defaults = _defaults_for("T20")
                if defaults is None:
                    break

                try:
                    cfg = json.loads(blob) if isinstance(blob, str) else blob
                except (TypeError, ValueError):
                    print(f"[Migration]   row {row_id} ({user_id}): unparseable "
                          "config_json — left as-is.")
                    continue
                if not isinstance(cfg, dict):
                    continue

                stripped = _strip_stale(cfg)
                if not stripped:
                    continue

                # `version` is a document marker, not part of a format block, so
                # it can never compare equal and must be excluded from the probe.
                probe = {k: v for k, v in cfg.items() if k != "version"}
                if _deep_merge(defaults, probe) == defaults:
                    conn.execute(text(f"DELETE FROM {TABLE} WHERE id=:i"), {"i": row_id})
                    deleted += 1
                    print(f"[Migration]   row {row_id} ({user_id}): stripped "
                          f"{', '.join(stripped)} — row now a no-op, removed.")
                    continue

                conn.execute(
                    text(f"UPDATE {TABLE} SET config_json=:c WHERE id=:i"),
                    {"c": json.dumps(cfg), "i": row_id},
                )
                cleaned += 1
                print(f"[Migration]   row {row_id} ({user_id}): stripped "
                      f"{', '.join(stripped)}.")

            conn.commit()
            print(f"[Migration] reset_stale_t20_pitch_tuning: {cleaned} blob(s) "
                  f"cleaned, {deleted} no-op row(s) removed, {len(rows)} inspected.")

        except Exception as e:
            log_exception(e)
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] reset_stale_t20_pitch_tuning FAILED: {e}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    os.environ.setdefault("SIMCRICKETX_PRECHECK_RUNNING", "1")
    from app import app as flask_app
    from database import db as _db
    run_migration(_db, flask_app)
