"""
Per-format User Ground Configs Migration
========================================

Moves `user_ground_configs` from one row per user to one row per
(user, match_format), and upgrades stored config blobs to the v2 vocabulary.

Background: ground conditions were a single JSON blob per user with T20
semantics — ListA matches ignored almost all of it. The config file is now
keyed by format, so the storage has to be too.

Steps (each independently idempotent):
  1. ADD COLUMN match_format, backfilling existing rows to 'T20'. Existing
     blobs are T20-shaped by definition, so this is the correct stamp.
  2. Replace the UNIQUE index on user_id alone with a UNIQUE index on
     (user_id, match_format). Uniqueness here is enforced by an index rather
     than a table constraint, so no table rebuild is needed.
  3. Stamp stored blobs with `version: 2` and translate `active_game_mode`:
     'natural_game' was the *unchosen* default and the engine ignored the
     setting entirely, so it becomes 'auto' (dynamic selection, i.e. today's
     actual behaviour). Any other mode name was a deliberate choice and
     becomes a real pin.
  4. Delete rows that add nothing over the factory defaults. An absent row
     means "use defaults", so a row that deep-equals them is pure noise.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from utils.exception_tracker import log_exception

TABLE = "user_ground_configs"
OLD_INDEX = "ix_user_ground_configs_user_id"
NEW_INDEX = "uq_ground_config_user_format"


def _table_exists(conn, table):
    return conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
    ), {"t": table}).fetchone() is not None


def _column_exists(conn, table, column):
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def _index_info(conn, table):
    """Return {index_name: is_unique} for *table*."""
    return {row[1]: bool(row[2])
            for row in conn.execute(text(f"PRAGMA index_list({table})")).fetchall()}


def _defaults_for(match_format):
    """Factory defaults block, or None if the config layer can't be loaded."""
    try:
        from engine.ground_config import get_defaults
        return get_defaults(match_format, mutable=True)
    except Exception as e:
        log_exception(e)
        print(f"[Migration] add_ground_config_formats: defaults unavailable ({e}) — "
              "skipping blob normalisation.")
        return None


def _deep_merge(base, override):
    from engine.ground_config import _deep_merge as merge
    return merge(base, override)


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
                print(f"[Migration] add_ground_config_formats: {TABLE} absent — nothing to do.")
                return

            # ── Step 1: match_format column ──────────────────────────────
            if not _column_exists(conn, TABLE, "match_format"):
                conn.execute(text(
                    f"ALTER TABLE {TABLE} ADD COLUMN match_format VARCHAR(10) DEFAULT 'T20'"
                ))
                print("[Migration] add_ground_config_formats: added match_format column.")
            conn.execute(text(
                f"UPDATE {TABLE} SET match_format='T20' "
                "WHERE match_format IS NULL OR match_format=''"
            ))

            # ── Step 2: swap the uniqueness index ────────────────────────
            indexes = _index_info(conn, TABLE)
            if indexes.get(OLD_INDEX):
                # Unique on user_id alone — blocks a second row for the same
                # user, which is exactly what per-format storage needs.
                conn.execute(text(f"DROP INDEX {OLD_INDEX}"))
                print(f"[Migration] add_ground_config_formats: dropped unique {OLD_INDEX}.")
            if NEW_INDEX not in _index_info(conn, TABLE):
                conn.execute(text(
                    f"CREATE UNIQUE INDEX {NEW_INDEX} ON {TABLE} (user_id, match_format)"
                ))
                print(f"[Migration] add_ground_config_formats: created {NEW_INDEX}.")
            if OLD_INDEX not in _index_info(conn, TABLE):
                # Recreate as a plain lookup index to match the model's index=True.
                conn.execute(text(f"CREATE INDEX {OLD_INDEX} ON {TABLE} (user_id)"))

            conn.commit()

            # ── Steps 3 & 4: normalise stored blobs ──────────────────────
            rows = conn.execute(text(
                f"SELECT id, user_id, match_format, config_json FROM {TABLE}"
            )).fetchall()

            upgraded = deleted = 0
            for row_id, user_id, fmt, blob in rows:
                fmt = fmt or "T20"
                defaults = _defaults_for(fmt)
                if defaults is None:
                    break

                try:
                    cfg = json.loads(blob) if isinstance(blob, str) else blob
                except (TypeError, ValueError):
                    print(f"[Migration]   row {row_id} ({user_id}): unparseable config_json — left as-is.")
                    continue
                if not isinstance(cfg, dict):
                    continue

                original = json.dumps(cfg, sort_keys=True)

                # 'natural_game' was the default nobody chose, and the engine
                # ignored the field regardless — map it to the dynamic selector
                # so behaviour is unchanged. A different mode was deliberate.
                mode = cfg.get("active_game_mode")
                if mode in (None, "natural_game"):
                    cfg["active_game_mode"] = "auto"
                cfg["version"] = 2

                # Drop rows that contribute nothing over the defaults. `version`
                # is a document-level marker, not part of a format block, so it
                # is excluded — otherwise no row could ever compare equal.
                probe = {k: v for k, v in cfg.items() if k != "version"}
                if _deep_merge(defaults, probe) == defaults:
                    conn.execute(text(f"DELETE FROM {TABLE} WHERE id=:i"), {"i": row_id})
                    deleted += 1
                    print(f"[Migration]   row {row_id} ({user_id}, {fmt}): "
                          "identical to defaults — removed.")
                    continue

                if json.dumps(cfg, sort_keys=True) != original:
                    conn.execute(
                        text(f"UPDATE {TABLE} SET config_json=:c WHERE id=:i"),
                        {"c": json.dumps(cfg), "i": row_id},
                    )
                    upgraded += 1

            conn.commit()
            print(f"[Migration] add_ground_config_formats: {upgraded} blob(s) upgraded, "
                  f"{deleted} no-op row(s) removed, {len(rows)} inspected.")

        except Exception as e:
            log_exception(e)
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_ground_config_formats FAILED: {e}")
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    os.environ.setdefault("SIMCRICKETX_PRECHECK_RUNNING", "1")
    from app import app as flask_app
    from database import db as _db
    run_migration(_db, flask_app)
