"""Add the in-flight match reservation column to tournament fixtures.

Without it, two tabs (or a double-submit) could each POST /match/setup for
the same Scheduled fixture and get two different match ids back, producing
two competing results for one fixture. See utils/fixture_rules.py.
"""

from sqlalchemy import inspect, text


def run_migration(db, app):
    """Add the nullable claim column and its lookup index idempotently."""
    with app.app_context():
        inspector = inspect(db.engine)
        if "tournament_fixtures" not in inspector.get_table_names():
            return

        columns = {column["name"] for column in inspector.get_columns("tournament_fixtures")}
        added = "active_match_id" not in columns
        with db.engine.begin() as connection:
            if added:
                connection.execute(text(
                    "ALTER TABLE tournament_fixtures ADD COLUMN active_match_id VARCHAR(36)"
                ))
            # Claims are looked up by match id when a match finishes or is
            # abandoned, which is the only non-primary-key access path.
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_fixture_active_match "
                "ON tournament_fixtures(active_match_id)"
            ))

        print(
            "[Migration] add_fixture_active_match: "
            + ("added tournament_fixtures.active_match_id." if added
               else "already applied.")
        )
