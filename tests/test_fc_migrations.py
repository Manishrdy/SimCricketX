"""
Idempotency tests for the First-Class (FC) data-model migrations.

Both migrations are additive ALTER TABLE ADD COLUMN steps guarded by a
PRAGMA table_info() check — running them twice must be a no-op the second
time, matching the pattern established by the other add_*.py migrations.
"""

import pytest
from sqlalchemy import text

from migrations.add_fc_match_columns import run_migration as run_fc_match_columns
from migrations.add_fc_player_ratings import run_migration as run_fc_player_ratings
from migrations.add_match_weather_summary import run_migration as run_weather_summary


def _columns(app, db, table):
    with app.app_context():
        with db.engine.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def test_add_fc_match_columns_idempotent(app):
    from app import db

    run_fc_match_columns(db, app)
    cols_after_first = _columns(app, db, "matches")
    for col in (
        "days", "follow_on_enforced",
        "home_team_score_innings2", "home_team_wickets_innings2", "home_team_overs_innings2",
        "away_team_score_innings2", "away_team_wickets_innings2", "away_team_overs_innings2",
    ):
        assert col in cols_after_first, f"{col} missing after first migration run"

    # Second run must not raise (idempotency guard) and must leave the same columns.
    run_fc_match_columns(db, app)
    cols_after_second = _columns(app, db, "matches")
    assert cols_after_first == cols_after_second


def test_add_fc_player_ratings_idempotent(app):
    from app import db

    run_fc_player_ratings(db, app)
    cols_after_first = _columns(app, db, "players")
    for col in ("technique_rating", "temperament_rating", "stamina_rating"):
        assert col in cols_after_first, f"{col} missing after first migration run"

    run_fc_player_ratings(db, app)
    cols_after_second = _columns(app, db, "players")
    assert cols_after_first == cols_after_second


def test_add_match_weather_summary_idempotent(app):
    from app import db

    run_weather_summary(db, app)
    cols_after_first = _columns(app, db, "matches")
    for col in (
        "weather_forecast", "weather_affected",
        "weather_minutes_lost", "weather_overs_lost",
    ):
        assert col in cols_after_first

    run_weather_summary(db, app)
    assert _columns(app, db, "matches") == cols_after_first


def test_prerequisite_gaps_flags_a_database_behind_on_earlier_migrations(tmp_path):
    """A drifted `players` table must be reported, not discovered mid-rebuild.

    The cache rebuild runs through the ORM, so SQLAlchemy SELECTs every column
    the model maps. Before this check, a production database that had missed
    `add_fc_player_ratings` failed with a bare `no such column:
    players.technique_rating` — after the ALTER TABLEs had already committed.
    """
    from sqlalchemy import create_engine

    from migrations.extend_fc_statistics_cache import _prerequisite_gaps

    drifted = create_engine(f"sqlite:///{(tmp_path / 'drifted.db').as_posix()}")
    with drifted.connect() as conn:
        conn.execute(text(
            "CREATE TABLE players (id INTEGER PRIMARY KEY, team_id INTEGER, name TEXT)"
        ))
        conn.commit()
        gaps = _prerequisite_gaps(conn)

    assert "technique_rating" in gaps["players"]
    # Tables that simply don't exist yet aren't drift — create_all handles those.
    assert "match_scorecards" not in gaps


def test_prerequisite_gaps_is_empty_on_a_current_schema(app):
    """The check runs on every boot (dry-run); it must not cry wolf."""
    from app import db

    from migrations.extend_fc_statistics_cache import _prerequisite_gaps

    with app.app_context():
        with db.engine.connect() as conn:
            assert _prerequisite_gaps(conn) == {}


def test_apply_aborts_before_touching_the_schema_when_a_prerequisite_is_missing(
    app, monkeypatch
):
    from app import db

    from migrations import extend_fc_statistics_cache as migration

    monkeypatch.setattr(
        migration,
        "_prerequisite_gaps",
        lambda conn: {"players": ["technique_rating"]},
    )

    with pytest.raises(migration.SchemaPrerequisiteError) as excinfo:
        migration.run_migration(db, app, apply=True)

    assert "technique_rating" in str(excinfo.value)
    assert "precheck" in str(excinfo.value)
