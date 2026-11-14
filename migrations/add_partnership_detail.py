"""
match_partnerships detail columns + legacy cleanup
==================================================

Adds the columns partnerships and fall of wickets need:
    batsman1_balls, batsman2_balls, fours, sixes, is_unbroken,
    score_at_fall, fall_over, wicket_type, dismissed_slot

then repairs the rows saved before them. The old engine had two defects:

* the stand still batting at the end of an innings was numbered with the
  wickets already down (so "6th wkt" twice, or "0th wkt" when none fell);
* a last-ball finish could save that stand twice, the copy 0 runs off 0 balls.

Cleanup, per (match, innings), rows in insertion (id) order, legacy rows only:

1. delete 0-run/0-ball rows that repeat an earlier row's wicket number;
2. a row whose wicket number is 0, or equal to the previous row's, is the
   unbroken stand: mark it and number it +1;
3. backfill score_at_fall (running total of stand runs — stand runs include
   extras) and fall_over (from end_over) for broken stands, but only when the
   innings' broken stands run 1..k with none missing, since a missing row
   would make the running total wrong.

Fours, sixes, batter balls and the dismissed batter cannot be recovered and
stay NULL.

Idempotent: a legacy row is one whose is_unbroken is NULL (the column is added
without a default); every processed row gets 0 or 1, so a re-run finds nothing.
"""

import os
import sys
from itertools import groupby

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from utils.partnership_format import legacy_fall_over

TABLE = "match_partnerships"
COLUMNS = {
    "batsman1_balls": "INTEGER",
    "batsman2_balls": "INTEGER",
    "fours": "INTEGER",
    "sixes": "INTEGER",
    "is_unbroken": "BOOLEAN",
    "score_at_fall": "INTEGER",
    "fall_over": "VARCHAR(8)",
    "wicket_type": "VARCHAR(30)",
    "dismissed_slot": "INTEGER",
}


def _columns(conn):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({TABLE})")).fetchall()}


def _repair_group(conn, rows, balls_per_over, is_hundred, counts):
    # 1. Empty duplicates.
    kept, seen = [], set()
    for r in rows:
        if r["wicket_number"] in seen and not r["runs"] and not r["balls"]:
            conn.execute(text(f"DELETE FROM {TABLE} WHERE id = :id"), {"id": r["id"]})
            counts["deleted"] += 1
            continue
        seen.add(r["wicket_number"])
        kept.append(r)

    # 2. The unbroken stand.
    prev = None
    for r in kept:
        r["unbroken"] = r["wicket_number"] == 0 or (
            prev is not None and r["wicket_number"] == prev["wicket_number"])
        if r["unbroken"]:
            r["wicket_number"] += 1
            counts["renumbered"] += 1
        prev = r

    # 3. Fall of wickets, if the broken stands are complete.
    broken = [r for r in kept if not r["unbroken"]]
    complete = [r["wicket_number"] for r in broken] == list(range(1, len(broken) + 1))
    running = 0
    for r in kept:
        running += r["runs"] or 0
        score = fall_over = None
        if complete and not r["unbroken"]:
            score = running
            fall_over = legacy_fall_over(r["end_over"], balls_per_over, is_hundred)
            counts["backfilled"] += 1
        conn.execute(text(
            f"UPDATE {TABLE} SET is_unbroken = :u, wicket_number = :w, "
            "score_at_fall = COALESCE(score_at_fall, :s), fall_over = COALESCE(fall_over, :f) "
            "WHERE id = :id"
        ), {"u": 1 if r["unbroken"] else 0, "w": r["wicket_number"],
            "s": score, "f": fall_over, "id": r["id"]})


def run_migration(db, app):
    with app.app_context(), db.engine.begin() as conn:
        exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": TABLE}).fetchone()
        if not exists:
            print(f"[Migration] add_partnership_detail: {TABLE} absent — nothing to do.")
            return

        have = _columns(conn)
        added = [c for c in COLUMNS if c not in have]
        for col in added:
            conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {col} {COLUMNS[col]}"))

        has_matches = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='matches'")).fetchone()
        fmt_join = ("LEFT JOIN matches m ON m.id = p.match_id" if has_matches else "")
        fmt_col = ("m.match_format" if has_matches else "NULL")
        rows = conn.execute(text(
            f"SELECT p.id, p.match_id, p.innings_number, p.wicket_number, p.runs, p.balls, "
            f"p.end_over, {fmt_col} AS fmt FROM {TABLE} p {fmt_join} "
            "WHERE p.match_id IN (SELECT DISTINCT match_id FROM match_partnerships WHERE is_unbroken IS NULL) "
            "ORDER BY p.match_id, p.innings_number, p.id"
        )).mappings().all()

        counts = {"deleted": 0, "renumbered": 0, "backfilled": 0, "groups": 0}
        for (_, _), group in groupby(rows, key=lambda r: (r["match_id"], r["innings_number"])):
            group = [dict(r) for r in group]
            is_hundred = group[0]["fmt"] == "Hundred"
            _repair_group(conn, group, 5 if is_hundred else 6, is_hundred, counts)
            counts["groups"] += 1

        print(
            f"[Migration] add_partnership_detail: added {len(added)} column(s); "
            f"repaired {counts['groups']} innings — deleted {counts['deleted']} empty "
            f"duplicate(s), renumbered {counts['renumbered']} unbroken stand(s), "
            f"backfilled fall of wickets on {counts['backfilled']} stand(s)."
        )


if __name__ == "__main__":
    from database import db as _db
    from app import create_app

    run_migration(_db, create_app())
