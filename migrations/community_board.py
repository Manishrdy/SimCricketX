"""Community board schema (replaces the 1:1 support chat).

Read-only by default; ``--apply`` makes the changes:

    python -m migrations.community_board --db ./cricket_sim.db            # dry run
    python -m migrations.community_board --db ./cricket_sim.db --apply    # apply

What --apply does (idempotent, safe to re-run):
  1. adds users.community_muted_until / community_engaged_at
  2. creates community_posts / _comments / _votes / _images / _notifications /
     _reports, or adds any columns and indexes they are missing (DDL comes
     from the ORM models, so it cannot drift from database/models.py)
  3. creates the FTS5 search index + sync triggers, and builds it once
  4. backfills vote counters (vote_count / downvote_count / score) from
     community_votes the first time the score column appears
  5. DROPS support_conversation_read_state, support_message and
     support_conversation — the old chat history is discarded. Take a DB
     backup first if you want to keep it.

On app boot, migrations/precheck.py calls ``run_on_boot``, which applies
steps 1-4 only (purely additive) and merely reports whether step 5 is still
pending, so the destructive drop only ever happens on an explicit --apply.

The drop also refuses to run while any other process holding the database is
not running the checked-out code (utils/runtime_registry.py). Order it as
deploy -> restart -> drop: dropping first leaves the still-running old worker
serving the support API against missing tables (GitHub #195).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from utils.exception_tracker import log_exception
from utils.runtime_registry import StaleServerError, assert_no_stale_holders, stale_holders, describe_stale

COMMUNITY_MODELS = (
    "CommunityPost",
    "CommunityComment",
    "CommunityVote",
    "CommunityImage",
    "CommunityNotification",
    "CommunityReport",
    "CommunityMention",
)
USER_COLUMNS = {"community_muted_until": "DATETIME", "community_engaged_at": "DATETIME"}
# Child tables first: read_state -> message -> conversation.
SUPPORT_TABLES = ("support_conversation_read_state", "support_message", "support_conversation")


def _tables():
    """Model tables in dependency order; a model not defined in this checkout
    is skipped, so the script works on either side of a feature landing."""
    import database.models as models
    return [getattr(models, n).__table__ for n in COMMUNITY_MODELS if hasattr(models, n)]


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def _has_table(conn, table: str) -> bool:
    return conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table}).fetchone() is not None


def _index_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()}


def _column_ddl(conn, col) -> str:
    ddl = f"{col.name} {col.type.compile(dialect=conn.dialect)}"
    default = getattr(col.default, "arg", None)
    if isinstance(default, bool):
        ddl += f" NOT NULL DEFAULT {int(default)}"
    elif isinstance(default, (int, float)):
        ddl += f" NOT NULL DEFAULT {default}"
    elif isinstance(default, str):
        ddl += " NOT NULL DEFAULT '" + default.replace("'", "''") + "'"
    return ddl


# ── Plan ─────────────────────────────────────────────────────────────────────

def inspect_plan(conn) -> dict:
    """Everything --apply would change, without changing anything."""
    from services.community_search import FTS_TABLE

    indexes = _index_names(conn)
    plan = {
        "user_columns": [c for c in USER_COLUMNS if c not in _columns(conn, "users")],
        "create_tables": [],
        "add_columns": {},
        "add_indexes": [],
        "fts_missing": not _has_table(conn, FTS_TABLE),
        "backfill_scores": False,
        "backfill_engagement": "community_engaged_at" not in _columns(conn, "users"),
        "support_tables": {},
    }
    for table in _tables():
        if not _has_table(conn, table.name):
            plan["create_tables"].append(table.name)
            continue
        existing = _columns(conn, table.name)
        missing = [c.name for c in table.columns if c.name not in existing]
        if missing:
            plan["add_columns"][table.name] = missing
        plan["add_indexes"] += [i.name for i in table.indexes if i.name not in indexes]
    plan["backfill_scores"] = "community_posts" in plan["create_tables"] or \
        "score" in plan["add_columns"].get("community_posts", [])
    for table in SUPPORT_TABLES:
        if _has_table(conn, table):
            plan["support_tables"][table] = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
    return plan


def _pending(plan: dict, *, include_drop: bool) -> bool:
    additive = (plan["user_columns"] or plan["create_tables"] or plan["add_columns"]
                or plan["add_indexes"] or plan["fts_missing"] or plan["backfill_scores"]
                or plan["backfill_engagement"])
    return bool(additive or (include_drop and plan["support_tables"]))


def print_plan(plan: dict) -> None:
    print("=" * 64)
    print("Community board migration")
    print("=" * 64)
    lines = []
    for col in plan["user_columns"]:
        lines.append(f"add column   users.{col}")
    for table in plan["create_tables"]:
        lines.append(f"create table {table}")
    for table, cols in plan["add_columns"].items():
        lines.append(f"add columns  {table}: {', '.join(cols)}")
    for idx in plan["add_indexes"]:
        lines.append(f"add index    {idx}")
    if plan["fts_missing"]:
        lines.append("create       community_posts_fts (FTS5 search index + triggers)")
    if plan["backfill_scores"]:
        lines.append("backfill     community_posts vote_count / downvote_count / score")
    if plan["backfill_engagement"]:
        lines.append("backfill     users.community_engaged_at from existing community activity")
    for table, rows in plan["support_tables"].items():
        lines.append(f"DROP table   {table}  ({rows} row{'s' if rows != 1 else ''} will be deleted)")
    print("\n".join(lines) if lines else "Nothing to do: schema is current and support tables are gone.")


# ── Apply ────────────────────────────────────────────────────────────────────

def _apply_additive(conn, plan: dict) -> None:
    from services.community_search import ensure_fts

    for col, ddl in USER_COLUMNS.items():
        if col in plan["user_columns"]:
            conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ddl}"))
    for table in _tables():
        if table.name in plan["create_tables"]:
            table.create(bind=conn)
            continue
        existing = _columns(conn, table.name)
        for col in table.columns:
            if col.name not in existing:
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {_column_ddl(conn, col)}"))
        for index in table.indexes:
            index.create(bind=conn, checkfirst=True)
    ensure_fts(conn)
    if plan["backfill_scores"]:
        # Downvotes arrived after the first release: every vote that predates
        # them is an upvote (value defaults to 1). Derive the counters once.
        conn.execute(text("""
            UPDATE community_posts SET
                vote_count = (SELECT COUNT(*) FROM community_votes v WHERE v.post_id = community_posts.id AND v.value > 0),
                downvote_count = (SELECT COUNT(*) FROM community_votes v WHERE v.post_id = community_posts.id AND v.value < 0)
        """))
        conn.execute(text("UPDATE community_posts SET score = vote_count - downvote_count"))
    if plan["backfill_engagement"]:
        conn.execute(text("""
            UPDATE users SET community_engaged_at = COALESCE(
                (SELECT MIN(created_at) FROM community_posts p WHERE p.author_id = users.id),
                (SELECT MIN(created_at) FROM community_comments c WHERE c.author_id = users.id),
                (SELECT MIN(created_at) FROM community_votes v WHERE v.user_id = users.id)
            )
            WHERE EXISTS (SELECT 1 FROM community_posts p WHERE p.author_id = users.id)
               OR EXISTS (SELECT 1 FROM community_comments c WHERE c.author_id = users.id)
               OR EXISTS (SELECT 1 FROM community_votes v WHERE v.user_id = users.id)
        """))


def _apply_drop(conn, plan: dict) -> None:
    for table in SUPPORT_TABLES:
        if table in plan["support_tables"]:
            conn.execute(text(f"DROP TABLE {table}"))


DROP_ACTION = "drop the support_* tables"


def run_migration(db, app, apply: bool = False) -> dict:
    """CLI entry: report by default; with apply, run every step incl. the drop."""
    with app.app_context():
        conn = db.engine.connect()
        db_path = conn.engine.url.database
        try:
            plan = inspect_plan(conn)
            print_plan(plan)
            if not _pending(plan, include_drop=True):
                return plan
            if not apply:
                if plan["support_tables"] and db_path:
                    stale = stale_holders(db_path)
                    if stale:
                        print("\nWARNING: --apply will be refused until this is resolved:\n"
                              + describe_stale(stale, db_path, DROP_ACTION))
                print("\nDRY RUN — no changes made. Re-run with --apply to execute.")
                return plan
            # Checked before any change, so a refusal leaves the schema untouched.
            if plan["support_tables"]:
                assert_no_stale_holders(db_path, DROP_ACTION)
            # inspect_plan already autobegan the connection's transaction.
            try:
                _apply_additive(conn, plan)
                _apply_drop(conn, plan)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            after = inspect_plan(conn)
            if _pending(after, include_drop=True):
                raise RuntimeError(f"migration incomplete, still pending: {after}")
            print("\nAPPLIED — community schema is current; support tables dropped.")
            return plan
        except StaleServerError:
            raise  # an operator-ordering refusal, not a fault worth an auto-filed issue
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "community_board"})
            raise
        finally:
            conn.close()


def run_on_boot(db, app) -> dict:
    """Precheck entry: apply the additive steps so the board works right after
    a deploy; never drop anything — just say when the drop is still pending."""
    with app.app_context():
        conn = db.engine.connect()
        try:
            plan = inspect_plan(conn)
            if _pending(plan, include_drop=False):
                try:
                    _apply_additive(conn, plan)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                print("[Migration] community_board: community schema created/updated.")
            if plan["support_tables"]:
                print("[Migration] community_board: support_* tables still present — run "
                      "`python -m migrations.community_board --db <path> --apply` to drop them.")
            return plan
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "community_board"})
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Community board schema + support chat removal (dry-run by default).")
    parser.add_argument("--db", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cricket_sim.db"),
                        help="SQLite database path (default: repository cricket_sim.db).")
    parser.add_argument("--apply", action="store_true", help="Apply the changes (default: dry-run).")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.isfile(db_path):
        parser.error(f"database does not exist: {db_path}")

    # Same escape hatch as the other standalone migrations: bind the app to the
    # explicit file and suppress production startup side effects.
    os.environ["SIMCRICKETX_SKIP_GLOBAL_APP"] = "1"
    os.environ["SIMCRICKETX_PRECHECK_RUNNING"] = "1"
    os.environ["SIMCRICKETX_TEST_MODE"] = "1"
    os.environ["SIMCRICKETX_TEST_DB_URI"] = f"sqlite:///{db_path}"
    print(f"Target database: {db_path}")

    from database import db as _db
    from app import create_app

    try:
        run_migration(_db, create_app(), apply=args.apply)
    except StaleServerError as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(2)
