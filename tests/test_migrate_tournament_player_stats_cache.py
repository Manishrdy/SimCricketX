"""
Tests for scripts/migrate_tournament_player_stats_cache.py

Covers the production failure:
    INSERT INTO tournament_player_stats_cache (...)  -- no category
    → IntegrityError: NOT NULL constraint failed: ...category

The live table on drifted DBs still has leftover category/data_json columns
from an older JSON-blob cache design. The rebuild must drop them, collapse
duplicate (tournament_id, player_id) rows, backfill team_id, and leave the
ORM able to insert.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.migrate_tournament_player_stats_cache import (
    TABLE,
    GHOST_COLUMNS,
    inspect_schema,
    apply_rebuild,
    main as migrate_main,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "migrate_tournament_player_stats_cache.py"

LEGACY_CREATE = f"""
CREATE TABLE {TABLE} (
    id INTEGER NOT NULL,
    tournament_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    category VARCHAR(20) NOT NULL,
    data_json TEXT NOT NULL,
    updated_at DATETIME,
    team_id INTEGER,
    matches_played INTEGER DEFAULT 0,
    innings_batted INTEGER DEFAULT 0,
    runs_scored INTEGER DEFAULT 0,
    balls_faced INTEGER DEFAULT 0,
    fours INTEGER DEFAULT 0,
    sixes INTEGER DEFAULT 0,
    not_outs INTEGER DEFAULT 0,
    highest_score INTEGER DEFAULT 0,
    fifties INTEGER DEFAULT 0,
    centuries INTEGER DEFAULT 0,
    batting_average FLOAT DEFAULT 0.0,
    batting_strike_rate FLOAT DEFAULT 0.0,
    innings_bowled INTEGER DEFAULT 0,
    overs_bowled VARCHAR(10) DEFAULT '0.0',
    runs_conceded INTEGER DEFAULT 0,
    wickets_taken INTEGER DEFAULT 0,
    maidens INTEGER DEFAULT 0,
    best_bowling_wickets INTEGER DEFAULT 0,
    best_bowling_runs INTEGER DEFAULT 0,
    five_wicket_hauls INTEGER DEFAULT 0,
    bowling_average FLOAT DEFAULT 0.0,
    bowling_economy FLOAT DEFAULT 0.0,
    bowling_strike_rate FLOAT DEFAULT 0.0,
    catches INTEGER DEFAULT 0,
    run_outs INTEGER DEFAULT 0,
    stumpings INTEGER DEFAULT 0,
    PRIMARY KEY (id),
    FOREIGN KEY(tournament_id) REFERENCES tournaments (id),
    FOREIGN KEY(player_id) REFERENCES players (id)
)
"""


def _seed_parents(conn):
    conn.execute("CREATE TABLE tournaments (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE teams (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE players (id INTEGER PRIMARY KEY, team_id INTEGER, "
        "FOREIGN KEY(team_id) REFERENCES teams(id))"
    )
    conn.execute("INSERT INTO tournaments (id) VALUES (33)")
    conn.execute("INSERT INTO teams (id) VALUES (234)")
    conn.execute("INSERT INTO players (id, team_id) VALUES (13184, 234)")


def _legacy_db(path):
    conn = sqlite3.connect(path)
    try:
        _seed_parents(conn)
        conn.execute(LEGACY_CREATE)
        conn.commit()
    finally:
        conn.close()


def _orm_insert_without_category(conn, tournament_id=33, player_id=13184, team_id=234):
    """The exact INSERT shape SQLAlchemy emits today."""
    conn.execute(
        f"""
        INSERT INTO {TABLE} (
            tournament_id, player_id, team_id, matches_played,
            innings_batted, runs_scored, balls_faced, fours, sixes, not_outs,
            highest_score, fifties, centuries, batting_average,
            batting_strike_rate, innings_bowled, overs_bowled, runs_conceded,
            wickets_taken, maidens, best_bowling_wickets, best_bowling_runs,
            five_wicket_hauls, bowling_average, bowling_economy,
            bowling_strike_rate, catches, run_outs, stumpings
        ) VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0, '0.0',
                  0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0, 0)
        """,
        (tournament_id, player_id, team_id),
    )


class TestInspectAndDryRun:
    def test_legacy_schema_needs_rebuild_and_blocks_orm_insert(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            snap = inspect_schema(conn)
            assert snap["needs_rebuild"] is True
            assert set(snap["ghost_columns"]) == set(GHOST_COLUMNS)
            assert snap["has_unique"] is False
            with pytest.raises(sqlite3.IntegrityError, match="category"):
                _orm_insert_without_category(conn)
        finally:
            conn.close()

    def test_dry_run_cli_writes_nothing(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        with sqlite3.connect(db_path) as conn:
            before = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (TABLE,),
            ).fetchone()[0]

        rc = migrate_main(["--db", str(db_path)])
        assert rc == 0

        with sqlite3.connect(db_path) as conn:
            after = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (TABLE,),
            ).fetchone()[0]
        assert after == before
        assert "category" in after

    def test_missing_table_is_skip_not_rebuild(self, tmp_path):
        db_path = tmp_path / "empty.db"
        sqlite3.connect(db_path).close()
        conn = sqlite3.connect(db_path)
        try:
            snap = inspect_schema(conn)
            assert snap["table_exists"] is False
            assert snap["needs_rebuild"] is False
        finally:
            conn.close()


class TestApplyRebuild:
    def test_apply_drops_ghosts_and_allows_orm_insert(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)

        rc = migrate_main(["--db", str(db_path), "--apply"])
        assert rc == 0

        conn = sqlite3.connect(db_path)
        try:
            snap = inspect_schema(conn)
            assert snap["needs_rebuild"] is False
            assert snap["ghost_columns"] == []
            assert snap["has_unique"] is True
            assert snap["has_tournament_index"] is True
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
            for ghost in GHOST_COLUMNS:
                assert ghost not in cols

            _orm_insert_without_category(conn)
            conn.commit()
            row = conn.execute(
                f"SELECT tournament_id, player_id, team_id, matches_played "
                f"FROM {TABLE}"
            ).fetchone()
            assert row == (33, 13184, 234, 0)
        finally:
            conn.close()

    def test_apply_is_idempotent(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        assert migrate_main(["--db", str(db_path), "--apply"]) == 0
        with sqlite3.connect(db_path) as conn:
            sql_once = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (TABLE,),
            ).fetchone()[0]
        assert migrate_main(["--db", str(db_path), "--apply"]) == 0
        with sqlite3.connect(db_path) as conn:
            sql_twice = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (TABLE,),
            ).fetchone()[0]
        assert sql_twice == sql_once

    def test_apply_collapses_category_duplicates_and_backfills_team_id(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                f"""
                INSERT INTO {TABLE}
                    (id, tournament_id, player_id, category, data_json,
                     team_id, matches_played, runs_scored, wickets_taken)
                VALUES
                    (1, 33, 13184, 'batting', '{{}}', NULL, 1, 14, 0),
                    (2, 33, 13184, 'bowling', '{{}}', NULL, 1, 0, 2)
                """
            )
            conn.commit()
        finally:
            conn.close()

        assert migrate_main(["--db", str(db_path), "--apply"]) == 0

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT player_id, team_id, matches_played, runs_scored, "
                f"wickets_taken FROM {TABLE}"
            ).fetchall()
            assert len(rows) == 1
            player_id, team_id, played, runs, wickets = rows[0]
            assert player_id == 13184
            assert team_id == 234
            assert played == 1
            # Richest row wins (runs_scored=14 beats 0).
            assert runs == 14
            assert wickets == 0
        finally:
            conn.close()

    def test_apply_with_backup_writes_snapshot(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        rc = migrate_main(["--db", str(db_path), "--apply", "--backup"])
        assert rc == 0
        backups = list(tmp_path.glob("legacy.db.bak.*"))
        assert len(backups) == 1
        # Backup still has the ghost column.
        bak = sqlite3.connect(backups[0])
        try:
            sql = bak.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (TABLE,),
            ).fetchone()[0]
            assert "category" in sql
        finally:
            bak.close()

    def test_subprocess_dry_run_then_apply(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}

        dry = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db_path)],
            capture_output=True, text=True, check=False, env=env,
        )
        assert dry.returncode == 0
        assert "DRY-RUN" in dry.stdout
        with sqlite3.connect(db_path) as conn:
            assert "category" in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (TABLE,),
            ).fetchone()[0]

        apply = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db_path), "--apply"],
            capture_output=True, text=True, check=False, env=env,
        )
        assert apply.returncode == 0, apply.stdout + apply.stderr
        assert "DONE" in apply.stdout
        cols = {
            r[1]
            for r in sqlite3.connect(db_path).execute(f"PRAGMA table_info({TABLE})")
        }
        assert "category" not in cols


class TestApplyRebuildInProcess:
    def test_apply_rebuild_helper_copies_row_count(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                f"""
                INSERT INTO {TABLE}
                    (id, tournament_id, player_id, category, data_json, team_id)
                VALUES (1, 33, 13184, 'batting', '{{}}', 234)
                """
            )
            conn.commit()
            result = apply_rebuild(conn)
            conn.commit()
            assert result["rebuilt"] is True
            assert result["copied"] == 1
            assert inspect_schema(conn)["needs_rebuild"] is False
        finally:
            conn.close()


def _install_legacy_cache_on_app_db(app):
    """Replace the create_all() cache table with the drifted production schema."""
    from app import db as app_db

    db_path = app_db.engine.url.database
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(LEGACY_CREATE)
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestResimulateAgainstLegacyCache:
    """End-to-end: drifted schema must not eat the match JSON, and after
    --apply the same resimulate path must succeed."""

    def test_resimulate_keeps_json_when_cache_insert_fails_then_succeeds_after_apply(
        self, app, authenticated_client, regular_user, test_team, test_team_2
    ):
        from app import db as app_db, PROJECT_ROOT
        from database.models import (
            TournamentFixture,
            Match as DBMatch,
            MatchScorecard,
            Player as DBPlayer,
        )
        from engine.tournament_engine import TournamentEngine

        db_path = _install_legacy_cache_on_app_db(app)

        engine = TournamentEngine()
        tournament = engine.create_tournament(
            name="Legacy Cache Resim",
            user_id=regular_user.id,
            team_ids=[test_team.id, test_team_2.id],
            mode="round_robin",
        )
        fixture = TournamentFixture.query.filter_by(tournament_id=tournament.id).first()
        batter = DBPlayer.query.filter_by(team_id=test_team.id).order_by(DBPlayer.id).first()

        match_id = "legacy-cache-resim-match"
        match_dir = os.path.join(PROJECT_ROOT, "data", "matches")
        os.makedirs(match_dir, exist_ok=True)
        json_filename = f"test_legacy_cache_{match_id}.json"
        json_path = os.path.join(match_dir, json_filename)
        with open(json_path, "w", encoding="utf-8") as fh:
            fh.write("{}")

        match = DBMatch(
            id=match_id,
            user_id=regular_user.id,
            tournament_id=tournament.id,
            home_team_id=test_team.id,
            away_team_id=test_team_2.id,
            winner_team_id=test_team.id,
            match_format="T20",
            match_json_path=json_filename,
        )
        app_db.session.add(match)
        app_db.session.flush()
        app_db.session.add(MatchScorecard(
            match_id=match_id, player_id=batter.id, team_id=test_team.id,
            innings_number=1, record_type="batting", runs=10, balls=8, is_out=True,
        ))
        fixture.match_id = match_id
        fixture.status = "Completed"
        app_db.session.commit()

        try:
            response = authenticated_client.post(
                f"/fixture/{fixture.id}/resimulate", follow_redirects=True
            )
            assert response.status_code == 200
            # Rolled back: match still there, JSON still on disk.
            assert app_db.session.get(DBMatch, match_id) is not None
            assert os.path.isfile(json_path)

            app_db.session.remove()
            app_db.engine.dispose()
            assert migrate_main(["--db", db_path, "--apply"]) == 0
            app_db.session.remove()

            response = authenticated_client.post(
                f"/fixture/{fixture.id}/resimulate", follow_redirects=True
            )
            assert response.status_code == 200
            app_db.session.expire_all()
            assert app_db.session.get(DBMatch, match_id) is None
            assert not os.path.isfile(json_path)
        finally:
            if os.path.isfile(json_path):
                os.remove(json_path)
