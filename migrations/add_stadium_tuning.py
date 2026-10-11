"""Add private v3 tuning without adopting venues or rewriting saved matches."""
from sqlalchemy import inspect, text


def run_migration(db, app):
    from database.models import StadiumRevisionV3, StadiumAdoption
    with app.app_context():
        with db.engine.begin() as conn:
            if 'current_profile_version' not in {c['name'] for c in inspect(conn).get_columns('stadiums')}:
                conn.execute(text('ALTER TABLE stadiums ADD COLUMN current_profile_version INTEGER NOT NULL DEFAULT 2'))
            StadiumRevisionV3.__table__.create(conn, checkfirst=True)
            StadiumAdoption.__table__.create(conn, checkfirst=True)
