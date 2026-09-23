"""
Community Board Tables
======================

Creates the community board (posts, comments, votes, images, notifications,
reports), the FTS5 search index + sync triggers, and users.community_muted_until.

Table DDL is generated from the ORM models rather than hand-written, so the
migration cannot drift from database/models.py. Safe to run multiple times.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from utils.exception_tracker import log_exception

COMMUNITY_MODELS = (
    "CommunityPost",
    "CommunityComment",
    "CommunityVote",
    "CommunityImage",
    "CommunityNotification",
    "CommunityReport",
)


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def _add_missing_model_columns(conn, table) -> None:
    """ALTER TABLE ADD COLUMN for model columns the live table lacks."""
    existing = _columns(conn, table.name)
    for col in table.columns:
        if col.name in existing:
            continue
        ddl = f"{col.name} {col.type.compile(dialect=conn.dialect)}"
        default = getattr(col.default, "arg", None)
        if isinstance(default, bool):
            ddl += f" NOT NULL DEFAULT {int(default)}"
        elif isinstance(default, (int, float)):
            ddl += f" NOT NULL DEFAULT {default}"
        elif isinstance(default, str):
            ddl += " NOT NULL DEFAULT '" + default.replace("'", "''") + "'"
        conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
        print(f"[Migration] add_community: added {table.name}.{col.name}.")


def run_migration(db, app):
    import database.models as models
    from services.community_search import ensure_fts

    with app.app_context():
        conn = db.engine.connect()
        trans = conn.begin()
        try:
            if "community_muted_until" not in _columns(conn, "users"):
                conn.execute(text("ALTER TABLE users ADD COLUMN community_muted_until DATETIME"))
                print("[Migration] add_community: added users.community_muted_until.")

            had_score = (conn.dialect.has_table(conn, "community_posts")
                         and "score" in _columns(conn, "community_posts"))
            for name in COMMUNITY_MODELS:
                table = getattr(models, name).__table__
                if not conn.dialect.has_table(conn, table.name):
                    table.create(bind=conn)
                    print(f"[Migration] add_community: created {table.name}.")
                else:
                    _add_missing_model_columns(conn, table)
                    for index in table.indexes:
                        index.create(bind=conn, checkfirst=True)

            if not had_score:
                # Downvotes arrived after the first release: every existing
                # vote is an upvote (value defaulted to 1); derive the counters.
                conn.execute(text("""
                    UPDATE community_posts SET
                        vote_count = (SELECT COUNT(*) FROM community_votes v WHERE v.post_id = community_posts.id AND v.value > 0),
                        downvote_count = (SELECT COUNT(*) FROM community_votes v WHERE v.post_id = community_posts.id AND v.value < 0)
                """))
                conn.execute(text("UPDATE community_posts SET score = vote_count - downvote_count"))

            if ensure_fts(conn):
                print("[Migration] add_community: created community_posts_fts.")

            trans.commit()
        except Exception as e:
            trans.rollback()
            log_exception(e, source="sqlite")
            print(f"[Migration] add_community failed: {e}")
            raise
        finally:
            conn.close()
