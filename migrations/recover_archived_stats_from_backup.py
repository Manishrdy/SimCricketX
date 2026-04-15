"""
Recover Archived Stats From Backup
==================================

Rehydrates missing/incomplete `match_scorecards` and `match_partnerships` in a
target DB from a known-good SQLite backup DB.

Why this exists:
- Historical scorecards are keyed to `players.id`.
- If squad operations delete/recreate Player rows, old `players.id` values can
  disappear and archived stats become unreachable (or are later deleted by
  orphan cleanup).
- This script copies stats back and remaps player IDs to current rows using:
    team_id + player name + match format profile.

Usage:
  python -m migrations.recover_archived_stats_from_backup \
      --source-db /path/to/good_backup.db \
      --target-db /path/to/current.db

  # Dry-run (default): report only, no writes.
  # Apply:
  python -m migrations.recover_archived_stats_from_backup \
      --source-db /path/to/good_backup.db \
      --target-db /path/to/current.db \
      --apply

  # Restrict to specific matches:
  python -m migrations.recover_archived_stats_from_backup \
      --source-db /path/to/good_backup.db \
      --target-db /path/to/current.db \
      --match-id <uuid> --match-id <uuid>
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections import defaultdict
from urllib.parse import unquote


def _dict_rows(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row


def _table_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _common_insert_cols(src_cols: list[str], dst_cols: list[str], *, skip: set[str]) -> list[str]:
    return [c for c in src_cols if c in dst_cols and c not in skip]


def _build_target_player_indexes(target: sqlite3.Connection):
    """
    Build lookup indexes for remapping source player IDs to target player IDs.

    Keys:
      (team_id, lower(name), format) -> [player_id]
      (team_id, lower(name), "")     -> [player_id]  # format-agnostic fallback
    """
    idx_exact = defaultdict(list)
    idx_fallback = defaultdict(list)

    rows = target.execute(
        """
        SELECT p.id, p.team_id, p.name, tp.format_type
        FROM players p
        LEFT JOIN team_profiles tp ON p.profile_id = tp.id
        """
    ).fetchall()
    for r in rows:
        team_id = r["team_id"]
        name = (r["name"] or "").strip().lower()
        fmt = (r["format_type"] or "").strip()
        if not name:
            continue
        idx_exact[(team_id, name, fmt)].append(r["id"])
        idx_fallback[(team_id, name, "")].append(r["id"])

    return idx_exact, idx_fallback


def _map_player_id(
    source: sqlite3.Connection,
    target_idx_exact,
    target_idx_fallback,
    *,
    source_player_id: int,
    match_format: str,
):
    sp = source.execute(
        "SELECT id, team_id, name FROM players WHERE id = ?",
        (source_player_id,),
    ).fetchone()
    if not sp:
        return None, "missing_source_player"

    team_id = sp["team_id"]
    name = (sp["name"] or "").strip().lower()
    if not name:
        return None, "blank_source_name"

    exact = target_idx_exact.get((team_id, name, match_format or ""), [])
    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        return None, "ambiguous_exact"

    fallback = target_idx_fallback.get((team_id, name, ""), [])
    if len(fallback) == 1:
        return fallback[0], "fallback"
    if len(fallback) > 1:
        return None, "ambiguous_fallback"

    return None, "no_target_match"


def _scorecard_exists(target: sqlite3.Connection, row: dict) -> bool:
    q = """
        SELECT 1
        FROM match_scorecards
        WHERE match_id = ?
          AND player_id = ?
          AND team_id = ?
          AND COALESCE(innings_number, 0) = COALESCE(?, 0)
          AND COALESCE(record_type, '') = COALESCE(?, '')
          AND COALESCE(position, -1) = COALESCE(?, -1)
          AND COALESCE(runs, 0) = COALESCE(?, 0)
          AND COALESCE(balls, 0) = COALESCE(?, 0)
          AND COALESCE(balls_bowled, 0) = COALESCE(?, 0)
          AND COALESCE(runs_conceded, 0) = COALESCE(?, 0)
          AND COALESCE(wickets, 0) = COALESCE(?, 0)
          AND COALESCE(catches, 0) = COALESCE(?, 0)
          AND COALESCE(run_outs, 0) = COALESCE(?, 0)
          AND COALESCE(stumpings, 0) = COALESCE(?, 0)
        LIMIT 1
    """
    args = (
        row.get("match_id"),
        row.get("player_id"),
        row.get("team_id"),
        row.get("innings_number"),
        row.get("record_type"),
        row.get("position"),
        row.get("runs"),
        row.get("balls"),
        row.get("balls_bowled"),
        row.get("runs_conceded"),
        row.get("wickets"),
        row.get("catches"),
        row.get("run_outs"),
        row.get("stumpings"),
    )
    return target.execute(q, args).fetchone() is not None


def _partnership_exists(target: sqlite3.Connection, row: dict) -> bool:
    q = """
        SELECT 1
        FROM match_partnerships
        WHERE match_id = ?
          AND innings_number = ?
          AND wicket_number = ?
          AND batsman1_id = ?
          AND batsman2_id = ?
          AND runs = ?
          AND balls = ?
        LIMIT 1
    """
    args = (
        row.get("match_id"),
        row.get("innings_number"),
        row.get("wicket_number"),
        row.get("batsman1_id"),
        row.get("batsman2_id"),
        row.get("runs"),
        row.get("balls"),
    )
    return target.execute(q, args).fetchone() is not None


def _insert_row(conn: sqlite3.Connection, table: str, row: dict, cols: list[str]):
    placeholders = ", ".join(["?"] * len(cols))
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    conn.execute(sql, tuple(row.get(c) for c in cols))


def recover(source_db: str, target_db: str, *, match_ids: list[str] | None = None, apply: bool = False):
    if not os.path.exists(source_db):
        raise FileNotFoundError(f"Source DB not found: {source_db}")
    if not os.path.exists(target_db):
        raise FileNotFoundError(f"Target DB not found: {target_db}")

    src = sqlite3.connect(source_db)
    dst = sqlite3.connect(target_db)
    _dict_rows(src)
    _dict_rows(dst)

    try:
        src_sc_cols = _table_cols(src, "match_scorecards")
        dst_sc_cols = _table_cols(dst, "match_scorecards")
        sc_insert_cols = _common_insert_cols(src_sc_cols, dst_sc_cols, skip={"id", "player_id"})
        if "player_id" not in dst_sc_cols:
            raise RuntimeError("Target match_scorecards missing player_id column.")

        src_mp_cols = _table_cols(src, "match_partnerships")
        dst_mp_cols = _table_cols(dst, "match_partnerships")
        mp_insert_cols = _common_insert_cols(src_mp_cols, dst_mp_cols, skip={"id", "batsman1_id", "batsman2_id"})

        if match_ids:
            placeholders = ", ".join(["?"] * len(match_ids))
            matches = src.execute(
                f"SELECT id, match_format FROM matches WHERE id IN ({placeholders})",
                tuple(match_ids),
            ).fetchall()
        else:
            matches = src.execute("SELECT id, match_format FROM matches").fetchall()

        idx_exact, idx_fallback = _build_target_player_indexes(dst)

        stats = {
            "matches_scanned": 0,
            "scorecards_seen": 0,
            "scorecards_inserted": 0,
            "scorecards_already_present": 0,
            "scorecards_unmapped": 0,
            "partnerships_seen": 0,
            "partnerships_inserted": 0,
            "partnerships_already_present": 0,
            "partnerships_unmapped": 0,
            "map_reason_counts": defaultdict(int),
        }

        if apply:
            dst.execute("BEGIN")

        for m in matches:
            match_id = m["id"]
            match_format = (m["match_format"] or "").strip()
            stats["matches_scanned"] += 1

            # Scorecards
            sc_rows = src.execute(
                "SELECT * FROM match_scorecards WHERE match_id = ?",
                (match_id,),
            ).fetchall()
            for r in sc_rows:
                stats["scorecards_seen"] += 1
                mapped_pid, reason = _map_player_id(
                    src,
                    idx_exact,
                    idx_fallback,
                    source_player_id=r["player_id"],
                    match_format=match_format,
                )
                stats["map_reason_counts"][f"scorecard:{reason}"] += 1
                if mapped_pid is None:
                    stats["scorecards_unmapped"] += 1
                    continue

                row = {c: r[c] for c in sc_insert_cols}
                row["player_id"] = mapped_pid

                if _scorecard_exists(dst, row):
                    stats["scorecards_already_present"] += 1
                    continue
                if apply:
                    _insert_row(dst, "match_scorecards", row, sc_insert_cols + ["player_id"])
                stats["scorecards_inserted"] += 1

            # Partnerships
            mp_rows = src.execute(
                "SELECT * FROM match_partnerships WHERE match_id = ?",
                (match_id,),
            ).fetchall()
            for r in mp_rows:
                stats["partnerships_seen"] += 1

                b1, rs1 = _map_player_id(
                    src, idx_exact, idx_fallback,
                    source_player_id=r["batsman1_id"], match_format=match_format
                )
                b2, rs2 = _map_player_id(
                    src, idx_exact, idx_fallback,
                    source_player_id=r["batsman2_id"], match_format=match_format
                )
                stats["map_reason_counts"][f"partnership_b1:{rs1}"] += 1
                stats["map_reason_counts"][f"partnership_b2:{rs2}"] += 1
                if b1 is None or b2 is None:
                    stats["partnerships_unmapped"] += 1
                    continue

                row = {c: r[c] for c in mp_insert_cols}
                row["batsman1_id"] = b1
                row["batsman2_id"] = b2

                if _partnership_exists(dst, row):
                    stats["partnerships_already_present"] += 1
                    continue
                if apply:
                    _insert_row(dst, "match_partnerships", row, mp_insert_cols + ["batsman1_id", "batsman2_id"])
                stats["partnerships_inserted"] += 1

        if apply:
            dst.commit()

        print("=" * 72)
        print("Recover Archived Stats Report")
        print("=" * 72)
        print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
        print(f"Source DB: {source_db}")
        print(f"Target DB: {target_db}")
        print(f"Matches scanned: {stats['matches_scanned']}")
        print(f"Scorecards: seen={stats['scorecards_seen']}, inserted={stats['scorecards_inserted']}, "
              f"already={stats['scorecards_already_present']}, unmapped={stats['scorecards_unmapped']}")
        print(f"Partnerships: seen={stats['partnerships_seen']}, inserted={stats['partnerships_inserted']}, "
              f"already={stats['partnerships_already_present']}, unmapped={stats['partnerships_unmapped']}")
        print("Mapping reasons:")
        for k in sorted(stats["map_reason_counts"]):
            print(f"  {k:<34} {stats['map_reason_counts'][k]}")
        print("=" * 72)

        return stats
    except Exception:
        if apply:
            dst.rollback()
        raise
    finally:
        src.close()
        dst.close()


def _sqlite_path_from_uri(uri: str) -> str | None:
    if not uri or not uri.startswith("sqlite:///"):
        return None
    raw = uri[len("sqlite:///"):]
    raw = unquote(raw)
    # sqlite:///relative/path.db -> relative to cwd
    return os.path.abspath(raw)


def run_migration(db, app):
    """
    Precheck hook.

    Defaults to a safe no-op unless PRECHECK_RECOVERY_SOURCE_DB is provided.
    This keeps startup deterministic while allowing explicit recovery rehearsal:

      PRECHECK_RECOVERY_SOURCE_DB=/path/good.db python -m migrations.precheck
    """
    source_db = os.getenv("PRECHECK_RECOVERY_SOURCE_DB", "").strip()
    if not source_db:
        print("[Migration] recover_archived_stats_from_backup: skipped (set PRECHECK_RECOVERY_SOURCE_DB to enable).")
        return

    target_db = _sqlite_path_from_uri(app.config.get("SQLALCHEMY_DATABASE_URI", "")) or "cricket_sim.db"
    raw_match_ids = (os.getenv("PRECHECK_RECOVERY_MATCH_IDS", "") or "").strip()
    match_ids = [m.strip() for m in raw_match_ids.split(",") if m.strip()] or None
    apply_flag = (os.getenv("PRECHECK_RECOVERY_APPLY", "0").strip().lower() in ("1", "true", "yes"))

    print("[Migration] recover_archived_stats_from_backup: running "
          f"{'APPLY' if apply_flag else 'DRY-RUN'} "
          f"(source={source_db}, target={target_db})")
    recover(
        source_db=source_db,
        target_db=target_db,
        match_ids=match_ids,
        apply=apply_flag,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recover archived scorecards/partnerships from a backup DB.")
    parser.add_argument("--source-db", required=True, help="Path to known-good backup SQLite DB.")
    parser.add_argument("--target-db", default="cricket_sim.db", help="Path to target SQLite DB.")
    parser.add_argument("--match-id", action="append", default=[], help="Optional match_id filter (repeatable).")
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry-run).")
    args = parser.parse_args()

    recover(
        source_db=args.source_db,
        target_db=args.target_db,
        match_ids=args.match_id or None,
        apply=args.apply,
    )
