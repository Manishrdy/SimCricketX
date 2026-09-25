"""Add format-specific ball allocations and auditable NRR contributions."""
from sqlalchemy import inspect, text


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        if inspect(conn).has_table("matches") and "format_metadata" not in {c["name"] for c in inspect(conn).get_columns("matches")}:
            conn.execute(text("ALTER TABLE matches ADD COLUMN format_metadata JSON"))
