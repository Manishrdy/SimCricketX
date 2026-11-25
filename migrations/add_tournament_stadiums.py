"""Persist venue pools only for newly created standalone tournaments."""
from sqlalchemy import inspect, text


def run_migration(db, app):
    with app.app_context():
        inspector = inspect(db.engine)
        if "tournaments" not in inspector.get_table_names():
            return
        if "stadium_ids" not in {c["name"] for c in inspector.get_columns("tournaments")}:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE tournaments ADD COLUMN stadium_ids JSON"))
