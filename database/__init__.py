from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    # SQLite ships with FK enforcement OFF per connection. Without it, our
    # ondelete='CASCADE' rules (e.g. match_scorecards.player_id) silently
    # fail — deleting a Player leaves scorecards orphaned, breaking stats.
    # Enable it for every new connection; no-op for non-SQLite drivers.
    driver = type(dbapi_connection).__module__
    if "sqlite" not in driver:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.close()
