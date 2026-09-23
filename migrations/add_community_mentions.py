"""Community board @-tags: the community_mentions table.

Read-only by default; ``--apply`` makes the changes:

    python -m migrations.add_community_mentions --db ./cricket_sim.db            # dry run
    python -m migrations.add_community_mentions --db ./cricket_sim.db --apply    # apply

What --apply does (purely additive, idempotent, safe to re-run):
  1. creates community_mentions (one row per @-tag: post_id, comment_id NULL =
     tagged in the post itself, user_id tagged, author_id tagger, name as
     written, created_at), or adds any columns it is missing
  2. creates ix_community_mentions_user (user_id, created_at) and
     ix_community_mentions_target (post_id, comment_id)

DDL comes from the CommunityMention ORM model, so it cannot drift from
database/models.py. Nothing existing is altered or deleted.

Requires the community board tables (community_posts, community_comments)
to exist already: run ``python -m migrations.community_board --apply`` first.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from utils.exception_tracker import log_exception

# Tables community_mentions has foreign keys into.
PREREQUISITES = ("users", "community_posts", "community_comments")


def _table():
    from database.models import CommunityMention
    return CommunityMention.__table__


def _has_table(conn, table: str) -> bool:
    return conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table}).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def _index_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()}


# ── Plan ─────────────────────────────────────────────────────────────────────

def inspect_plan(conn) -> dict:
    """Everything --apply would change, without changing anything."""
    table = _table()
    plan = {
        "missing_prerequisites": [t for t in PREREQUISITES if not _has_table(conn, t)],
        "create_table": False,
        "add_columns": [],
        "add_indexes": [],
    }
    if not _has_table(conn, table.name):
        plan["create_table"] = True
        plan["add_indexes"] = [i.name for i in table.indexes]
        return plan
    existing = _columns(conn, table.name)
    plan["add_columns"] = [c.name for c in table.columns if c.name not in existing]
    indexes = _index_names(conn)
    plan["add_indexes"] = [i.name for i in table.indexes if i.name not in indexes]
    return plan


def _pending(plan: dict) -> bool:
    return bool(plan["create_table"] or plan["add_columns"] or plan["add_indexes"])


def print_plan(plan: dict) -> None:
    print("=" * 64)
    print("Community @-tags migration (community_mentions)")
    print("=" * 64)
    if plan["missing_prerequisites"]:
        print("BLOCKED: missing table(s) " + ", ".join(plan["missing_prerequisites"])
              + ". Run `python -m migrations.community_board --db <path> --apply` first.")
        return
    lines = []
    if plan["create_table"]:
        lines.append(f"create table {_table().name}")
    if plan["add_columns"]:
        lines.append(f"add columns  {_table().name}: {', '.join(plan['add_columns'])}")
    for idx in plan["add_indexes"]:
        lines.append(f"add index    {idx}")
    print("\n".join(lines) if lines else "Nothing to do: community_mentions is current.")


# ── Apply ────────────────────────────────────────────────────────────────────

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


def _apply(conn, plan: dict) -> None:
    table = _table()
    if plan["create_table"]:
        table.create(bind=conn)  # also creates the model's indexes
        return
    for col in table.columns:
        if col.name in plan["add_columns"]:
            conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {_column_ddl(conn, col)}"))
    for index in table.indexes:
        index.create(bind=conn, checkfirst=True)


def run_migration(db, app, apply: bool = False) -> dict:
    """CLI entry: report by default; with apply, make the changes and verify."""
    with app.app_context():
        conn = db.engine.connect()
        try:
            plan = inspect_plan(conn)
            print_plan(plan)
            if plan["missing_prerequisites"]:
                raise SystemExit(2)
            if not _pending(plan):
                return plan
            if not apply:
                print("\nDRY RUN — no changes made. Re-run with --apply to execute.")
                return plan
            # inspect_plan already autobegan the connection's transaction.
            try:
                _apply(conn, plan)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            after = inspect_plan(conn)
            if _pending(after):
                raise RuntimeError(f"migration incomplete, still pending: {after}")
            print("\nAPPLIED — community_mentions is current.")
            return plan
        except SystemExit:
            raise
        except Exception as exc:
            log_exception(exc, source="sqlite", context={"migration": "add_community_mentions"})
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create the community_mentions table (dry-run by default).")
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

    run_migration(_db, create_app(), apply=args.apply)
