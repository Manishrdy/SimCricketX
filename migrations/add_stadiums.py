"""Add stadiums without changing any existing team's venue behaviour."""
from sqlalchemy import inspect, text


def run_migration(db, app):
    from database.models import Stadium, StadiumRevision
    with app.app_context():
        Stadium.__table__.create(db.engine, checkfirst=True)
        StadiumRevision.__table__.create(db.engine, checkfirst=True)
        additions = {
            'teams': {'stadium_id': 'VARCHAR(36) REFERENCES stadiums(id)',
                      'venue_mode': "VARCHAR(16) NOT NULL DEFAULT 'legacy'"},
            'matches': {'stadium_id': 'VARCHAR(36) REFERENCES stadiums(id)',
                        'stadium_revision': 'INTEGER', 'conditions_model_version': 'VARCHAR(24)',
                        'conditions_snapshot': 'JSON'},
            'tournament_fixtures': {'stadium_id': 'VARCHAR(36) REFERENCES stadiums(id)'},
        }
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        with db.engine.begin() as conn:
            for table, columns in additions.items():
                if table not in tables:
                    continue
                existing = {c['name'] for c in inspector.get_columns(table)}
                for name, definition in columns.items():
                    if name not in existing:
                        conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
        from migrations.simplify_stadiums import convert_existing
        with db.engine.begin() as conn:
            convert_existing(conn)
        from migrations.add_stadium_tuning import run_migration as add_tuning
        add_tuning(db, app)
        from services.stadiums import seed_catalogue
        if "users" in tables:
            seed_catalogue()
