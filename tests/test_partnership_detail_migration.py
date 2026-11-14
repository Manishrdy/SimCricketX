from flask import Flask
from sqlalchemy import create_engine, text

from migrations.add_partnership_detail import run_migration

LEGACY = """CREATE TABLE match_partnerships (
    id INTEGER PRIMARY KEY, match_id VARCHAR(36) NOT NULL, innings_number INTEGER NOT NULL,
    wicket_number INTEGER NOT NULL, batsman1_id INTEGER NOT NULL, batsman2_id INTEGER NOT NULL,
    runs INTEGER, balls INTEGER, batsman1_contribution INTEGER, batsman2_contribution INTEGER,
    start_over FLOAT, end_over FLOAT)"""


def _engine(tmp_path, rows):
    engine = create_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE matches (id VARCHAR(36) PRIMARY KEY, match_format VARCHAR(20))"))
        conn.execute(text("INSERT INTO matches VALUES ('m1', 'T20'), ('m2', 'T20'), ('h1', 'Hundred')"))
        conn.execute(text(LEGACY))
        for r in rows:
            conn.execute(text(
                "INSERT INTO match_partnerships (id, match_id, innings_number, wicket_number, "
                "batsman1_id, batsman2_id, runs, balls, end_over) "
                "VALUES (:id, :m, :i, :w, 1, 2, :r, :b, :e)"), r)
    return engine


def _rows(engine, match_id):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT id, wicket_number, is_unbroken, score_at_fall, fall_over FROM match_partnerships "
            "WHERE match_id = :m ORDER BY innings_number, id"), {"m": match_id}).mappings()]


def test_cleans_duplicates_renumbers_unbroken_and_backfills_fow(tmp_path):
    engine = _engine(tmp_path, [
        # m1 inns 1: two wickets then unbroken stand numbered 2 + an empty duplicate.
        dict(id=1, m="m1", i=1, w=1, r=23, b=22, e=3 + 4 / 6),
        dict(id=2, m="m1", i=1, w=2, r=22, b=17, e=7 + 1 / 6),
        dict(id=3, m="m1", i=1, w=2, r=40, b=30, e=12.0),
        dict(id=4, m="m1", i=1, w=2, r=0, b=0, e=12.0),
        # m1 inns 2: no wicket fell — legacy unbroken numbered 0.
        dict(id=5, m="m1", i=2, w=0, r=86, b=60, e=10.0),
        # m2: a missing row (wicket 2 never saved) -> no FoW backfill.
        dict(id=6, m="m2", i=1, w=1, r=10, b=8, e=1 + 2 / 6),
        dict(id=7, m="m2", i=1, w=3, r=5, b=4, e=4.0),
        # Hundred: fall over is a ball count.
        dict(id=8, m="h1", i=1, w=1, r=12, b=9, e=9 / 5),
    ])
    db = type("Database", (), {"engine": engine})()
    app = Flask(__name__)
    run_migration(db, app)

    assert _rows(engine, "m1") == [
        {"id": 1, "wicket_number": 1, "is_unbroken": 0, "score_at_fall": 23, "fall_over": "3.4"},
        {"id": 2, "wicket_number": 2, "is_unbroken": 0, "score_at_fall": 45, "fall_over": "7.1"},
        {"id": 3, "wicket_number": 3, "is_unbroken": 1, "score_at_fall": None, "fall_over": None},
        {"id": 5, "wicket_number": 1, "is_unbroken": 1, "score_at_fall": None, "fall_over": None},
    ]
    assert [r["score_at_fall"] for r in _rows(engine, "m2")] == [None, None]
    assert _rows(engine, "h1")[0]["fall_over"] == "9"

    before = {m: _rows(engine, m) for m in ("m1", "m2", "h1")}
    run_migration(db, app)
    assert {m: _rows(engine, m) for m in ("m1", "m2", "h1")} == before
    engine.dispose()
