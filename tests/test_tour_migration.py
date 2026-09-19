from flask import Flask
from sqlalchemy import create_engine, inspect, text
from migrations.add_tours import run_migration


def test_tour_migration_preserves_standalone_and_is_repeatable(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE tournaments (id INTEGER PRIMARY KEY, name VARCHAR(100))'))
        conn.execute(text("INSERT INTO tournaments VALUES (1, 'Existing')"))
    database = type('Database', (), {'engine': engine})()
    app = Flask(__name__)
    run_migration(database, app)
    run_migration(database, app)
    assert 'tours' in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT name, tour_id, tour_order FROM tournaments')).one() == ('Existing', None, None)
    assert any(i['unique'] for i in inspect(engine).get_indexes('tours'))
    engine.dispose()
