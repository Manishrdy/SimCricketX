"""
Idempotency tests for the First-Class (FC) data-model migrations.

Both migrations are additive ALTER TABLE ADD COLUMN steps guarded by a
PRAGMA table_info() check — running them twice must be a no-op the second
time, matching the pattern established by the other add_*.py migrations.
"""

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
