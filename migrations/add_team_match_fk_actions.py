"""
Team / Match FK ON DELETE Actions Migration
============================================

Adds `ON DELETE` actions to the foreign-key constraints linking matches,
fixtures and standings to teams (and matches to tournaments). Without these,
deleting a Team or Tournament fails at the DB layer with an IntegrityError
whenever any dependent row still references it — see [delete_team] in
team_routes.py and [delete_tournament] in tournament_routes.py for the
application-layer cleanup that compensates for the missing actions today.

What this migration changes
---------------------------
matches:
    home_team_id        INTEGER  REFERENCES teams(id)        ON DELETE SET NULL
    away_team_id        INTEGER  REFERENCES teams(id)        ON DELETE SET NULL
    winner_team_id      INTEGER  REFERENCES teams(id)        ON DELETE SET NULL
    toss_winner_team_id INTEGER  REFERENCES teams(id)        ON DELETE SET NULL
    tournament_id       INTEGER  REFERENCES tournaments(id)  ON DELETE SET NULL

tournament_teams:
    team_id             INTEGER  REFERENCES teams(id)        ON DELETE CASCADE

tournament_fixtures:
    home_team_id        INTEGER  REFERENCES teams(id)        ON DELETE CASCADE
    away_team_id        INTEGER  REFERENCES teams(id)        ON DELETE CASCADE
    winner_team_id      INTEGER  REFERENCES teams(id)        ON DELETE SET NULL
    match_id            VARCHAR  REFERENCES matches(id)      ON DELETE SET NULL

Idempotent: each table is checked first. If every targeted column already has
the desired action, the table is skipped. Re-running the migration on a fully
migrated database is a no-op.

SQLite caveat
-------------
SQLite cannot ALTER a foreign-key action on an existing column — it must
rebuild the table. This migration does that by reading the current
`CREATE TABLE` statement out of `sqlite_master`, splicing `ON DELETE <action>`
into the relevant `FOREIGN KEY` clauses, creating a `<table>_new` with the
modified SQL, copying data, dropping the old table and renaming.

Foreign-key enforcement is disabled for the duration of the rebuild so the
data copy does not trigger spurious cascades; it is re-enabled in the
`finally` block.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


# (table, column) → desired ON DELETE action
TARGETS = {
    "matches": {
        "home_team_id":        "SET NULL",
        "away_team_id":        "SET NULL",
        "winner_team_id":      "SET NULL",
        "toss_winner_team_id": "SET NULL",
        "tournament_id":       "SET NULL",
    },
    "tournament_teams": {
        "team_id":             "CASCADE",
    },
    "tournament_fixtures": {
        "home_team_id":        "CASCADE",
        "away_team_id":        "CASCADE",
        "winner_team_id":      "SET NULL",
        "match_id":            "SET NULL",
    },
}


def _table_exists(conn, table):
    row = conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:n"
    ), {"n": table}).fetchone()
    return row is not None


def _current_fk_actions(conn, table):
    """Map column → current ON DELETE action (uppercase, '' if NO ACTION)."""
    rows = conn.execute(text(f"PRAGMA foreign_key_list({table})")).fetchall()
    # row layout: (id, seq, table, from, to, on_update, on_delete, match)
    return {r[3]: (r[6] or "").upper() for r in rows}


def _table_already_correct(conn, table, wanted):
    actions = _current_fk_actions(conn, table)
    for col, want in wanted.items():
        if actions.get(col, "") != want.upper():
            return False
    return True


def _inject_on_delete(create_sql, col, action):
    """Add `ON DELETE <action>` to the FK clause for `col` in `create_sql`.

    Handles both forms emitted by SQLAlchemy / SQLite:
      A. column-inline:  `<col> TYPE REFERENCES <ref> (<refcol>)`
      B. table-clause:   `FOREIGN KEY(<col>) REFERENCES <ref> (<refcol>)`

    If the FK already has any `ON DELETE` clause the SQL is returned unchanged
    — the caller is responsible for short-circuiting when nothing needs doing.
    """
    col_q = re.escape(col)

    # Form B — separate FOREIGN KEY clause.
    pat_b = re.compile(
        rf"(FOREIGN\s+KEY\s*\(\s*{col_q}\s*\)\s+REFERENCES\s+\w+\s*\(\s*\w+\s*\))(?!\s+ON\s+DELETE)",
        re.IGNORECASE,
    )
    new_sql, n = pat_b.subn(rf"\1 ON DELETE {action}", create_sql)
    if n:
        return new_sql

    # Form A — column-inline REFERENCES.
    pat_a = re.compile(
        rf"(\b{col_q}\b\s+\w+(?:\s*\(\s*\d+\s*\))?(?:\s+NOT\s+NULL)?(?:\s+DEFAULT\s+\S+)?\s+REFERENCES\s+\w+\s*\(\s*\w+\s*\))(?!\s+ON\s+DELETE)",
        re.IGNORECASE,
    )
    new_sql, n = pat_a.subn(rf"\1 ON DELETE {action}", create_sql)
    return new_sql


def _list_indexes(conn, table):
    """Return [(name, sql)] for user-defined indexes on `table` (auto-indexes
    have NULL sql and are recreated automatically by SQLite)."""
    rows = conn.execute(text(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name=:t AND sql IS NOT NULL"
    ), {"t": table}).fetchall()
    return [(r[0], r[1]) for r in rows]


def _rebuild_table(conn, table, wanted):
    create_sql_row = conn.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"
    ), {"t": table}).fetchone()
    if not create_sql_row:
        return False
    create_sql = create_sql_row[0]

    new_sql = create_sql
    for col, action in wanted.items():
        # Skip if this column's FK is already correct in the live schema —
        # but still apply the regex so the resulting CREATE TABLE encodes
        # every desired action (the regex no-ops when ON DELETE is present).
        new_sql = _inject_on_delete(new_sql, col, action)

    # Rename the live table reference to <table>_new for the rebuild.
    new_table = f"{table}_new"
    new_sql = re.sub(
        rf"^CREATE\s+TABLE\s+{re.escape(table)}\b",
        f"CREATE TABLE {new_table}",
        new_sql,
        count=1,
        flags=re.IGNORECASE,
    )

    indexes = _list_indexes(conn, table)
    cols_info = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    col_names = [c[1] for c in cols_info]
    cols_csv = ", ".join(col_names)

    conn.execute(text(new_sql))
    conn.execute(text(f"INSERT INTO {new_table} ({cols_csv}) SELECT {cols_csv} FROM {table}"))
    conn.execute(text(f"DROP TABLE {table}"))
    conn.execute(text(f"ALTER TABLE {new_table} RENAME TO {table}"))

    for _idx_name, idx_sql in indexes:
        try:
            conn.execute(text(idx_sql))
        except Exception:
            # Auto-recreated unique indexes etc. may already exist; benign.
            pass

    return True


def run_migration(db, app):
    with app.app_context():
        if db.engine.dialect.name != "sqlite":
            print(f"[Migration] add_team_match_fk_actions: dialect "
                  f"'{db.engine.dialect.name}' — skipping (run native ALTER manually).")
            return

        conn = db.engine.connect()

        # SQLAlchemy 2.x autobegins on connect(). Commit so PRAGMA can run
        # outside a transaction (SQLite quirk), matching add_scorecard_cascade.
        try:
            conn.rollback()
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            conn.commit()
        except Exception:
            pass

        rebuilt = []
        skipped = []
        try:
            for table, wanted in TARGETS.items():
                if not _table_exists(conn, table):
                    skipped.append(f"{table} (absent)")
                    continue

                # Filter to columns that actually exist on this DB — safer
                # than asserting the live schema matches the model.
                col_names = {r[1] for r in conn.execute(
                    text(f"PRAGMA table_info({table})")
                ).fetchall()}
                applicable = {c: a for c, a in wanted.items() if c in col_names}
                if not applicable:
                    skipped.append(f"{table} (no target columns present)")
                    continue

                if _table_already_correct(conn, table, applicable):
                    skipped.append(f"{table} (already correct)")
                    continue

                _rebuild_table(conn, table, applicable)
                rebuilt.append(table)

            conn.commit()
            if rebuilt:
                print(f"[Migration] add_team_match_fk_actions: rebuilt "
                      f"{len(rebuilt)} table(s) — {', '.join(rebuilt)}.")
            for s in skipped:
                print(f"[Migration] add_team_match_fk_actions: skipped {s}.")
        except Exception as exc:
            log_exception(exc, source="sqlite",
                          context={"migration": "add_team_match_fk_actions"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_team_match_fk_actions: FAILED — {exc}")
            raise
        finally:
            try:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.commit()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("Team / Match FK ON DELETE Actions - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
