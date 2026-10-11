"""Idempotent, additive team-profile captaincy configuration migration."""
import json
from sqlalchemy import inspect, text
from engine.captaincy import default_config


def run_migration(db, app):
    with app.app_context():
        if 'team_profiles' not in inspect(db.engine).get_table_names():
            return
        with db.engine.begin() as connection:
            columns = {c['name'] for c in inspect(connection).get_columns('team_profiles')}
            if 'captaincy_config' not in columns:
                connection.execute(text('ALTER TABLE team_profiles ADD COLUMN captaincy_config JSON'))
            connection.execute(text('UPDATE team_profiles SET captaincy_config = :config WHERE captaincy_config IS NULL'),
                               {'config': json.dumps(default_config())})
