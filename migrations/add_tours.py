"""Add tours without changing existing tournament membership; safe to repeat."""
from sqlalchemy import inspect, text
from database.models import Tour


def run_migration(db, app):
    with app.app_context():
        Tour.__table__.create(db.engine, checkfirst=True)
        inspector = inspect(db.engine)
        if 'tournaments' not in inspector.get_table_names():
            return
        columns = {c['name'] for c in inspector.get_columns('tournaments')}
        with db.engine.begin() as connection:
            for name, sql_type in (
                ('tour_id', 'INTEGER REFERENCES tours(id)'),
                ('tour_order', 'INTEGER'),
                ('tour_started_at', 'TIMESTAMP'),
            ):
                if name not in columns:
                    connection.execute(text(f'ALTER TABLE tournaments ADD COLUMN {name} {sql_type}'))
            connection.execute(text('CREATE INDEX IF NOT EXISTS ix_tournaments_tour_id ON tournaments(tour_id)'))
