"""Coverage for the tournament creation-token schema migration."""

from flask import Flask
from sqlalchemy import create_engine, inspect, text

from migrations.add_tournament_creation_token import run_migration


class _Database:
    def __init__(self, engine):
        self.engine = engine


def test_creation_token_migration_upgrades_legacy_table_idempotently(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE tournaments ("
            "id INTEGER PRIMARY KEY, "
            "user_id VARCHAR(120) NOT NULL, "
            "name VARCHAR(100) NOT NULL"
            ")"
        ))

    app = Flask(__name__)
    database = _Database(engine)
    run_migration(database, app)
    run_migration(database, app)

    schema = inspect(engine)
    assert "creation_token" in {
        column["name"] for column in schema.get_columns("tournaments")
    }
    assert any(
        index["name"] == "uq_tournament_user_creation_token"
        and index["unique"]
        for index in schema.get_indexes("tournaments")
    )
