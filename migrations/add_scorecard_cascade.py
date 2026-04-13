"""
MatchScorecard ON DELETE CASCADE Migration
==========================================

Recreates `match_scorecards` so that `player_id` has `ON DELETE CASCADE`.

Without this, deleting a Player (e.g. via the Team → TeamProfile → Player
cascade) leaves orphaned scorecard rows or fails the delete entirely under
RESTRICT, breaking stats aggregation.

Idempotent: detects whether the FK already cascades and exits early.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception


def _player_fk_cascades(conn):
    """Return True when match_scorecards.player_id FK already has ON DELETE CASCADE."""
    rows = conn.execute(text("PRAGMA foreign_key_list(match_scorecards)")).fetchall()
    # row: (id, seq, table, from, to, on_update, on_delete, match)
    for row in rows:
        from_col = row[3]
        on_delete = row[6]
        if from_col == "player_id":
            return (on_delete or "").upper() == "CASCADE"
    return False


def run_migration(db, app):
    with app.app_context():
        conn = db.engine.connect()
        # SQLAlchemy 2.x autobegins a transaction on connect(), so we can't
        # call conn.begin() again. Commit the autobegun txn first so the
        # PRAGMA runs outside a transaction (SQLite requires this), then
        # let the next statement autobegin a fresh one for our DDL.
        try:
            conn.rollback()
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            conn.commit()
        except Exception:
            pass

        try:
            exists = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='match_scorecards'"
            )).fetchone()
            if not exists:
                conn.commit()
                print("[Migration] add_scorecard_cascade: match_scorecards table absent — nothing to do.")
                return

            if _player_fk_cascades(conn):
                conn.commit()
                print("[Migration] add_scorecard_cascade: already applied.")
                return

            cols_info = conn.execute(text("PRAGMA table_info(match_scorecards)")).fetchall()
            # (cid, name, type, notnull, dflt_value, pk)

            col_defs = []
            for col in cols_info:
                cid, cname, ctype, notnull, dflt, pk = col
                if pk:
                    col_defs.append(f"    {cname} {ctype} PRIMARY KEY AUTOINCREMENT")
                    continue
                parts = [f"    {cname} {ctype or 'TEXT'}"]
                if notnull:
                    parts.append("NOT NULL")
                if dflt is not None:
                    parts.append(f"DEFAULT {dflt}")
                if cname == "match_id":
                    parts.append("REFERENCES matches(id)")
                elif cname == "player_id":
                    parts.append("REFERENCES players(id) ON DELETE CASCADE")
                elif cname == "team_id":
                    parts.append("REFERENCES teams(id)")
                col_defs.append(" ".join(parts))

            col_defs_sql = ",\n".join(col_defs)

            conn.execute(text("CREATE TABLE match_scorecards_new (\n" + col_defs_sql + "\n)"))

            col_names = [col[1] for col in cols_info]
            cols_csv = ", ".join(col_names)
            conn.execute(text(
                f"INSERT INTO match_scorecards_new ({cols_csv}) SELECT {cols_csv} FROM match_scorecards"
            ))

            conn.execute(text("DROP TABLE match_scorecards"))
            conn.execute(text("ALTER TABLE match_scorecards_new RENAME TO match_scorecards"))

            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_match_scorecards_match_id ON match_scorecards(match_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_match_scorecards_player_id ON match_scorecards(player_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_match_scorecards_team_id ON match_scorecards(team_id)"))

            conn.commit()
            print("[Migration] add_scorecard_cascade: completed successfully.")
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_scorecard_cascade"})
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Migration] add_scorecard_cascade: FAILED — {exc}")
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
    print("MatchScorecard CASCADE - Database Migration")
    print("=" * 60)

    from database import db as _db
    from app import create_app

    _app = create_app()
    run_migration(_db, _app)
    print("Done.")
