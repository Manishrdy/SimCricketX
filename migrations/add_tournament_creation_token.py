"""Add persisted idempotency tokens for tournament creation."""

from sqlalchemy import inspect, text


def run_migration(db, app):
    """Add the nullable token and its per-user unique index idempotently."""
    with app.app_context():
        inspector = inspect(db.engine)
        if "tournaments" not in inspector.get_table_names():
            return

        columns = {column["name"] for column in inspector.get_columns("tournaments")}
        with db.engine.begin() as connection:
            if "creation_token" not in columns:
                connection.execute(text(
                    "ALTER TABLE tournaments ADD COLUMN creation_token VARCHAR(64)"
                ))
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tournament_user_creation_token "
                "ON tournaments(user_id, creation_token)"
            ))
