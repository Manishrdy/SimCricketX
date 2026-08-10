"""
Player Batting/Bowling Style Columns Migration
==============================================

Adds two additive columns to `players`:

  players.batting_style  VARCHAR(20), nullable — 'Anchor' | 'Accumulator' | 'Power'
  players.bowling_style  VARCHAR(20), nullable — 'Economist' | 'Balanced' | 'Strike'

Why these exist
---------------
`batting_rating` says how GOOD a player is. It cannot say how they PLAY.
An anchor and a power hitter of identical rating should produce visibly
different innings — the anchor rotating strike and surviving, the power
hitter hitting more dots AND more sixes and getting out more often. Style
is the axis that carries that; rating is the axis that carries quality.

Neither column is read by the engine until the style axis ships. Until
then every value here is inert.

Deliberately NOT registered in migrations/precheck.py
-----------------------------------------------------
This is a one-time backfill, run manually against a known database, after
a `--check` rehearsal. It is not part of the startup chain.

Columns are added WITHOUT a DDL default, so pre-existing rows read NULL.
That is load-bearing: NULL means "never assigned", which lets the backfill
use `WHERE ... IS NULL` and therefore never overwrite a style a user has
since chosen. Re-runs are no-ops. New rows get their default from the
SQLAlchemy model, not from the DDL.

Modes
-----
  neutral    (recommended) Every player gets the identity style
             ('Accumulator' / 'Balanced'), whose multipliers are all 1.0.
             Zero behaviour change. Users assign real styles themselves.

  heuristic  Derives a style from role + rating. See _heuristic_* below.
             This INVENTS a playing personality for every player from data
             that does not describe playing personality. Read the caveat
             in the module docstring of the review notes before using it.

Usage
-----
    # read-only rehearsal (default) — mutates nothing
    python -m migrations.add_player_styles --check --db cricket_sim.db
    python -m migrations.add_player_styles --check --mode heuristic --db cricket_sim.db

    # apply
    python -m migrations.add_player_styles --apply --mode neutral --db cricket_sim.db

`--check` opens the database read-only and never boots Flask, so it cannot
trigger the startup precheck chain as a side effect.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BATTING_STYLES = ("Anchor", "Accumulator", "Power")
BOWLING_STYLES = ("Economist", "Balanced", "Strike")

NEUTRAL_BATTING = "Accumulator"
NEUTRAL_BOWLING = "Balanced"


# ---------------------------------------------------------------------------
# Style derivation
# ---------------------------------------------------------------------------

def _heuristic_batting(role, batting_rating):
    """Guess a batting style from role + rating.

    Caveat: rating measures quality, not intent. A 90-rated batter is equally
    likely to be an anchor or a destroyer in real cricket; this function
    cannot tell them apart and simply calls the high-rated ones aggressive.
    """
    role = (role or "").strip()
    br = batting_rating or 0
    if role == "Bowler" or br < 40:
        # Tailenders: leave neutral. Their rating already models the weakness;
        # stamping 'Power' would double-penalise them with extra dots+wickets.
        return NEUTRAL_BATTING
    if br >= 82 and role in ("Batsman", "Wicketkeeper"):
        return "Power"
    if br >= 68:
        return "Accumulator"
    return "Anchor"


def _heuristic_bowling(bowling_rating):
    br = bowling_rating or 0
    if br <= 0:
        return NEUTRAL_BOWLING      # does not bowl
    if br >= 80:
        return "Strike"
    if br >= 60:
        return "Balanced"
    return "Economist"


def _assign(row, mode):
    """row: (id, name, role, batting_rating, bowling_rating) -> (bat_style, bowl_style)"""
    _id, _name, role, bat, bowl = row
    if mode == "neutral":
        return NEUTRAL_BATTING, NEUTRAL_BOWLING
    return _heuristic_batting(role, bat), _heuristic_bowling(bowl)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _column_exists(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists(conn, table):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# Sanity check (read-only)
# ---------------------------------------------------------------------------

def check(db_path, mode):
    if not os.path.exists(db_path):
        print(f"  DB not found: {db_path}")
        return False

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        print(f"  database          : {db_path} ({os.path.getsize(db_path)/1e6:.1f} MB)")

        if not _table_exists(conn, "players"):
            print("  players table     : ABSENT — nothing to migrate.")
            return False

        total = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        has_bat = _column_exists(conn, "players", "batting_style")
        has_bowl = _column_exists(conn, "players", "bowling_style")
        print(f"  players rows      : {total:,}")
        print(f"  batting_style col : {'PRESENT' if has_bat else 'absent (will be added)'}")
        print(f"  bowling_style col : {'PRESENT' if has_bowl else 'absent (will be added)'}")

        if has_bat:
            todo = conn.execute(
                "SELECT COUNT(*) FROM players WHERE batting_style IS NULL"
            ).fetchone()[0]
            print(f"  rows still NULL   : {todo:,}  (only these would be written)")

        rows = conn.execute(
            "SELECT id, name, role, batting_rating, bowling_rating FROM players"
        ).fetchall()

        bat_dist, bowl_dist = {}, {}
        for r in rows:
            b, w = _assign(r, mode)
            bat_dist[b] = bat_dist.get(b, 0) + 1
            bowl_dist[w] = bowl_dist.get(w, 0) + 1

        print(f"\n  --- proposed distribution (mode={mode}) ---")
        print("  batting_style")
        for s in BATTING_STYLES:
            n = bat_dist.get(s, 0)
            print(f"    {s:<14} {n:>7,}  ({n/total*100:5.1f}%)")
        print("  bowling_style")
        for s in BOWLING_STYLES:
            n = bowl_dist.get(s, 0)
            print(f"    {s:<14} {n:>7,}  ({n/total*100:5.1f}%)")

        # Eyeball test: one real squad, as a cricket XI rather than a row count.
        squad = conn.execute("""
            SELECT p.id, p.name, p.role, p.batting_rating, p.bowling_rating
            FROM players p
            WHERE p.profile_id = (
                SELECT profile_id FROM players
                WHERE profile_id IS NOT NULL
                GROUP BY profile_id HAVING COUNT(*) >= 11 LIMIT 1
            )
            ORDER BY p.batting_rating DESC LIMIT 13
        """).fetchall()
        if squad:
            print(f"\n  --- eyeball test: one real squad (mode={mode}) ---")
            print(f"    {'name':<22} {'role':<14} {'bat':>4} {'bowl':>5}  "
                  f"{'batting_style':<14} {'bowling_style'}")
            for r in squad:
                b, w = _assign(r, mode)
                print(f"    {str(r[1])[:22]:<22} {str(r[2] or ''):<14} "
                      f"{r[3] or 0:>4} {r[4] or 0:>5}  {b:<14} {w}")
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply(db_path, mode, make_backup=True):
    if not os.path.exists(db_path):
        print(f"  DB not found: {db_path}")
        return False

    if make_backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{db_path}.pre_styles_{stamp}.bak"
        shutil.copy2(db_path, backup)
        print(f"  backup written    : {backup}")

    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, "players"):
            print("  players table absent — nothing to do.")
            return False

        added = []
        if not _column_exists(conn, "players", "batting_style"):
            conn.execute("ALTER TABLE players ADD COLUMN batting_style VARCHAR(20)")
            added.append("batting_style")
        if not _column_exists(conn, "players", "bowling_style"):
            conn.execute("ALTER TABLE players ADD COLUMN bowling_style VARCHAR(20)")
            added.append("bowling_style")
        print(f"  columns added     : {', '.join(added) if added else 'none (already present)'}")

        if mode == "neutral":
            cur = conn.execute(
                "UPDATE players SET batting_style=?, bowling_style=? "
                "WHERE batting_style IS NULL OR bowling_style IS NULL",
                (NEUTRAL_BATTING, NEUTRAL_BOWLING),
            )
            written = cur.rowcount
        else:
            rows = conn.execute(
                "SELECT id, name, role, batting_rating, bowling_rating FROM players "
                "WHERE batting_style IS NULL OR bowling_style IS NULL"
            ).fetchall()
            payload = [(*_assign(r, mode), r[0]) for r in rows]
            conn.executemany(
                "UPDATE players SET batting_style=?, bowling_style=? WHERE id=?", payload
            )
            written = len(payload)

        conn.commit()
        print(f"  rows written      : {written:,}")

        left = conn.execute(
            "SELECT COUNT(*) FROM players WHERE batting_style IS NULL OR bowling_style IS NULL"
        ).fetchone()[0]
        print(f"  rows still NULL   : {left:,}  (expected 0)")
        return left == 0
    except Exception as exc:
        conn.rollback()
        print(f"  FAILED — {exc}")
        raise
    finally:
        conn.close()


def run_migration(db, app):
    """Kept for signature-compatibility with the precheck registry.

    This migration is intentionally NOT registered there — it is a one-time
    manual backfill. Provided so it can be promoted later without a rewrite.
    """
    with app.app_context():
        uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        path = uri.replace("sqlite:///", "") if uri.startswith("sqlite:///") else None
        if not path:
            print("[Migration] add_player_styles: non-sqlite URI, skipping.")
            return
        apply(path, "neutral", make_backup=False)


def main():
    ap = argparse.ArgumentParser(description="Add and backfill player style columns.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="read-only rehearsal (default)")
    g.add_argument("--apply", action="store_true", help="write changes")
    ap.add_argument("--mode", choices=("neutral", "heuristic"), default="neutral")
    ap.add_argument("--db", default="cricket_sim.db", help="path to the sqlite database")
    ap.add_argument("--no-backup", action="store_true", help="skip the pre-apply file copy")
    args = ap.parse_args()

    doing = "APPLY" if args.apply else "CHECK (read-only)"
    print("=" * 72)
    print(f"Player Styles Migration — {doing} — mode={args.mode}")
    print("=" * 72)

    if args.apply:
        ok = apply(args.db, args.mode, make_backup=not args.no_backup)
    else:
        ok = check(args.db, args.mode)

    print("=" * 72)
    print("Done." if ok else "Finished with warnings — read the output above.")


if __name__ == "__main__":
    main()
