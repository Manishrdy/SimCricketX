#!/usr/bin/env python3
"""
Standalone tournament standings repair.
Connects DIRECTLY to the SQLite file — no Flask, no ORM, no migrations.

Finds every orphaned tournament match (match exists in DB but no
tournament_fixtures.match_id points to it), links it to the correct
fixture, and applies W/L/Pts/NRR deltas to tournament_teams.

Usage:
    python3 scripts/fix_prod_standings.py                 # dry-run
    python3 scripts/fix_prod_standings.py --apply         # commit fixes
    python3 scripts/fix_prod_standings.py --apply --delete-duplicates
    python3 scripts/fix_prod_standings.py --db /path/to/cricket_sim.db --apply
"""

import argparse
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "cricket_sim.db",
)

POINTS_WIN = 2
POINTS_TIE = 1
POINTS_NO_RESULT = 1


# ---------------------------------------------------------------------------
# Cricket overs helpers  (mirrors tournament_engine.py exactly)
# ---------------------------------------------------------------------------

def overs_to_balls(overs):
    if not overs:
        return 0
    try:
        f = float(overs)
    except (ValueError, TypeError):
        return 0
    if f < 0:
        return 0
    whole = int(f)
    partial = round((f - whole) * 10)
    if partial > 5:
        partial = 5
    return whole * 6 + partial


def balls_to_overs(balls):
    return f"{balls // 6}.{balls % 6}"


def add_overs(o1, o2):
    return balls_to_overs(overs_to_balls(o1 or '0.0') + overs_to_balls(o2 or '0.0'))


def nrr_overs(actual_overs, wickets, overs_per_side):
    """ICC rule: all-out innings use full quota for NRR denominator.
    Also defensively caps at full quota — some legacy rows store "20.1" in a
    20-over match (upstream sim accounting bug), which would otherwise skew NRR.
    """
    ops = int(overs_per_side or 20)
    if wickets is not None and int(wickets) >= 10:
        return f"{ops}.0"
    actual = actual_overs or '0.0'
    if overs_to_balls(actual) > ops * 6:
        return f"{ops}.0"
    return actual


def calc_nrr(runs_scored, overs_faced, runs_conceded, overs_bowled):
    bf = overs_to_balls(overs_faced)
    bb = overs_to_balls(overs_bowled)
    rrf = (runs_scored or 0) / (bf / 6.0) if bf > 0 else 0.0
    rra = (runs_conceded or 0) / (bb / 6.0) if bb > 0 else 0.0
    return round(rrf - rra, 6)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_orphans(conn):
    """
    Return (fixable, duplicate_plays, no_fixture).

    fixable        : list of (match_row_dict, fixture_id)
    duplicate_plays: list of match_row_dict
    no_fixture     : list of match_row_dict
    """
    cur = conn.cursor()

    cur.execute("""
        SELECT m.id, m.tournament_id, m.home_team_id, m.away_team_id,
               m.winner_team_id, m.result_description,
               m.home_team_score, m.home_team_overs, m.home_team_wickets,
               m.away_team_score, m.away_team_overs, m.away_team_wickets,
               COALESCE(m.overs_per_side, 20),
               m.date
        FROM matches m
        WHERE m.tournament_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM tournament_fixtures tf WHERE tf.match_id = m.id
          )
        ORDER BY m.tournament_id, m.date DESC
    """)
    cols = ['id','tournament_id','home_team_id','away_team_id',
            'winner_team_id','result_description',
            'home_team_score','home_team_overs','home_team_wickets',
            'away_team_score','away_team_overs','away_team_wickets',
            'overs_per_side','date']
    orphans = [dict(zip(cols, row)) for row in cur.fetchall()]

    claimed   = set()
    fixable   = []
    dupes     = []
    no_fix    = []

    for m in orphans:
        tid, hid, aid = m['tournament_id'], m['home_team_id'], m['away_team_id']

        # Free fixture (unlinked, standings not applied yet)
        cur.execute("""
            SELECT id FROM tournament_fixtures
            WHERE tournament_id=? AND home_team_id=? AND away_team_id=?
              AND match_id IS NULL AND standings_applied=0
            ORDER BY round_number
        """, (tid, hid, aid))
        candidates = [r[0] for r in cur.fetchall() if r[0] not in claimed]

        if candidates:
            fid = candidates[0]
            claimed.add(fid)
            fixable.append((m, fid))
            continue

        # No free fixture — is it claimed by a newer orphan in this pass?
        cur.execute("""
            SELECT id FROM tournament_fixtures
            WHERE tournament_id=? AND home_team_id=? AND away_team_id=?
        """, (tid, hid, aid))
        all_for_pair = [r[0] for r in cur.fetchall()]
        if any(f in claimed for f in all_for_pair):
            dupes.append(m)
            continue

        # Or already linked+applied from a prior successful play?
        cur.execute("""
            SELECT id FROM tournament_fixtures
            WHERE tournament_id=? AND home_team_id=? AND away_team_id=?
              AND match_id IS NOT NULL AND standings_applied=1
        """, (tid, hid, aid))
        if cur.fetchone():
            dupes.append(m)
        else:
            no_fix.append(m)

    return fixable, dupes, no_fix


# ---------------------------------------------------------------------------
# Standings helpers
# ---------------------------------------------------------------------------

def get_team_stats(conn, tournament_id, team_id):
    cur = conn.cursor()
    cur.execute("""
        SELECT id, played, won, lost, tied, no_result, points,
               runs_scored, overs_faced, runs_conceded, overs_bowled, net_run_rate
        FROM tournament_teams
        WHERE tournament_id=? AND team_id=?
    """, (tournament_id, team_id))
    row = cur.fetchone()
    if row:
        return dict(zip(
            ['id','played','won','lost','tied','no_result','points',
             'runs_scored','overs_faced','runs_conceded','overs_bowled','net_run_rate'],
            row
        )), False
    return {
        'id': None,
        'played':0,'won':0,'lost':0,'tied':0,'no_result':0,'points':0,
        'runs_scored':0,'overs_faced':'0.0',
        'runs_conceded':0,'overs_bowled':'0.0',
        'net_run_rate':0.0,
    }, True


def save_team_stats(conn, tournament_id, team_id, s, is_new):
    cur = conn.cursor()
    if is_new:
        cur.execute("""
            INSERT INTO tournament_teams
            (tournament_id, team_id, played, won, lost, tied, no_result, points,
             runs_scored, overs_faced, runs_conceded, overs_bowled, net_run_rate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (tournament_id, team_id,
              s['played'], s['won'], s['lost'], s['tied'], s['no_result'], s['points'],
              s['runs_scored'], s['overs_faced'],
              s['runs_conceded'], s['overs_bowled'], s['net_run_rate']))
    else:
        cur.execute("""
            UPDATE tournament_teams
            SET played=?, won=?, lost=?, tied=?, no_result=?, points=?,
                runs_scored=?, overs_faced=?, runs_conceded=?, overs_bowled=?,
                net_run_rate=?
            WHERE id=?
        """, (s['played'], s['won'], s['lost'], s['tied'], s['no_result'], s['points'],
              s['runs_scored'], s['overs_faced'],
              s['runs_conceded'], s['overs_bowled'], s['net_run_rate'], s['id']))


def apply_match_to_standings(conn, m, fixture_id, dry_run):
    """
    Apply one match result:
      1. Link fixture (match_id, status, winner_team_id, standings_applied)
      2. Update tournament_teams W/L/Pts/NRR for both teams
    """
    tid  = m['tournament_id']
    hid  = m['home_team_id']
    aid  = m['away_team_id']
    wid  = m['winner_team_id']
    ops  = m['overs_per_side']

    home_s, home_new = get_team_stats(conn, tid, hid)
    away_s, away_new = get_team_stats(conn, tid, aid)

    # --- W/L/Pts ---
    home_s['played'] += 1
    away_s['played'] += 1

    if wid == hid:
        home_s['won']    += 1
        home_s['points'] += POINTS_WIN
        away_s['lost']   += 1
    elif wid == aid:
        away_s['won']    += 1
        away_s['points'] += POINTS_WIN
        home_s['lost']   += 1
    elif wid is None:
        # Tie or No-Result; treat as tie for points (no_result keywords absent here)
        home_s['tied']   += 1
        away_s['tied']   += 1
        home_s['points'] += POINTS_TIE
        away_s['points'] += POINTS_TIE

    # --- NRR ---
    h_overs_nrr = nrr_overs(m['home_team_overs'], m['home_team_wickets'], ops)
    a_overs_nrr = nrr_overs(m['away_team_overs'], m['away_team_wickets'], ops)

    # Home batting / away bowling
    home_s['runs_scored']  = (home_s['runs_scored']  or 0) + (m['home_team_score'] or 0)
    home_s['overs_faced']  = add_overs(home_s['overs_faced'],  h_overs_nrr)
    away_s['runs_conceded'] = (away_s['runs_conceded'] or 0) + (m['home_team_score'] or 0)
    away_s['overs_bowled'] = add_overs(away_s['overs_bowled'], h_overs_nrr)

    # Away batting / home bowling
    away_s['runs_scored']  = (away_s['runs_scored']  or 0) + (m['away_team_score'] or 0)
    away_s['overs_faced']  = add_overs(away_s['overs_faced'],  a_overs_nrr)
    home_s['runs_conceded'] = (home_s['runs_conceded'] or 0) + (m['away_team_score'] or 0)
    home_s['overs_bowled'] = add_overs(home_s['overs_bowled'], a_overs_nrr)

    # Recalculate NRR
    home_s['net_run_rate'] = calc_nrr(
        home_s['runs_scored'], home_s['overs_faced'],
        home_s['runs_conceded'], home_s['overs_bowled']
    )
    away_s['net_run_rate'] = calc_nrr(
        away_s['runs_scored'], away_s['overs_faced'],
        away_s['runs_conceded'], away_s['overs_bowled']
    )

    if dry_run:
        return home_s, away_s

    cur = conn.cursor()
    # Link fixture
    cur.execute("""
        UPDATE tournament_fixtures
        SET match_id=?, status='Completed', winner_team_id=?, standings_applied=1
        WHERE id=?
    """, (m['id'], wid, fixture_id))

    save_team_stats(conn, tid, hid, home_s, home_new)
    save_team_stats(conn, tid, aid, away_s, away_new)

    return home_s, away_s


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def team_name(conn, team_id):
    if team_id is None:
        return '?'
    r = conn.execute("SELECT name FROM teams WHERE id=?", (team_id,)).fetchone()
    return r[0] if r else f"id={team_id}"


def tourn_name(conn, tid):
    r = conn.execute("SELECT name FROM tournaments WHERE id=?", (tid,)).fetchone()
    return r[0] if r else f"id={tid}"


def print_report(conn, fixable, dupes, no_fix):
    sep = "=" * 80
    total = len(fixable) + len(dupes) + len(no_fix)
    print(sep)
    print("Orphaned Tournament Match Report")
    print(sep)
    print(f"  Total orphaned matches  : {total}")
    print(f"  Fixable                 : {len(fixable)}")
    print(f"  Duplicate-play junk     : {len(dupes)}")
    print(f"  No fixture (manual)     : {len(no_fix)}")
    print()

    if fixable:
        by_t = {}
        for m, fid in fixable:
            by_t.setdefault(m['tournament_id'], []).append((m, fid))
        print("-- FIXABLE " + "-" * 69)
        for tid, pairs in by_t.items():
            print(f"\n  {tourn_name(conn, tid)} (id={tid})")
            for m, fid in pairs:
                h = team_name(conn, m['home_team_id'])
                a = team_name(conn, m['away_team_id'])
                w = team_name(conn, m['winner_team_id'])
                print(f"    {m['id']}")
                print(f"    {h} vs {a}  ->  winner: {w}")
                print(f"    {m['home_team_score']}/{m['home_team_wickets']} "
                      f"({m['home_team_overs']}) vs "
                      f"{m['away_team_score']}/{m['away_team_wickets']} "
                      f"({m['away_team_overs']})")
                print(f"    -> fixture #{fid}")
                print()

    if dupes:
        print("-- DUPLICATE-PLAY JUNK (no standings impact) " + "-" * 35)
        for m in dupes:
            h = team_name(conn, m['home_team_id'])
            a = team_name(conn, m['away_team_id'])
            print(f"  {m['id']}  {tourn_name(conn, m['tournament_id'])}  {h} vs {a}")
            print(f"  Result: {m['result_description']}")
        print()

    if no_fix:
        print("-- NO FIXTURE FOUND (manual review needed) " + "-" * 37)
        for m in no_fix:
            h = team_name(conn, m['home_team_id'])
            a = team_name(conn, m['away_team_id'])
            print(f"  {m['id']}  {tourn_name(conn, m['tournament_id'])}  {h} vs {a}")
        print()


def print_standings(conn, tournament_ids):
    print()
    print("POST-REPAIR STANDINGS")
    print("-" * 80)
    for tid in sorted(tournament_ids):
        print(f"\n  {tourn_name(conn, tid)} (id={tid})")
        rows = conn.execute("""
            SELECT tt.team_id, tk.name, tt.played, tt.won, tt.lost,
                   tt.tied, tt.no_result, tt.points, tt.net_run_rate
            FROM tournament_teams tt
            JOIN teams tk ON tk.id = tt.team_id
            WHERE tt.tournament_id=?
            ORDER BY tt.points DESC, tt.net_run_rate DESC
        """, (tid,)).fetchall()
        if rows:
            print(f"  {'Team':<26} {'P':>3} {'W':>3} {'L':>3} {'T':>3} {'NR':>3} "
                  f"{'Pts':>4}  {'NRR':>8}")
            print(f"  {'-'*26} {'-'*3} {'-'*3} {'-'*3} {'-'*3} {'-'*3} "
                  f"{'-'*4}  {'-'*8}")
            for r in rows:
                print(f"  {r[1]:<26} {r[2]:>3} {r[3]:>3} {r[4]:>3} "
                      f"{r[5]:>3} {r[6]:>3} {r[7]:>4}  {r[8]:>+8.3f}")
        else:
            print("  (no standings rows)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(db_path, apply=False, delete_duplicates=False):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        fixable, dupes, no_fix = find_orphans(conn)
        print_report(conn, fixable, dupes, no_fix)

        if not apply and not delete_duplicates:
            print()
            print("DRY RUN — no changes made.")
            print("  --apply              link fixtures and apply standings")
            print("  --delete-duplicates  delete duplicate-play junk rows")
            return

        applied  = 0
        skipped  = 0
        errors   = []
        affected_tournaments = set()

        if apply and fixable:
            print("=" * 80)
            print(f"Applying {len(fixable)} fix(es)...")
            print("=" * 80)
            for m, fid in fixable:
                h = team_name(conn, m['home_team_id'])
                a = team_name(conn, m['away_team_id'])
                print(f"\n  {h} vs {a}  [{m['id'][:8]}...]  -> fixture #{fid}")
                try:
                    conn.execute("BEGIN")
                    apply_match_to_standings(conn, m, fid, dry_run=False)
                    conn.execute("COMMIT")
                    print(f"  OK")
                    applied += 1
                    affected_tournaments.add(m['tournament_id'])
                except Exception as exc:
                    conn.execute("ROLLBACK")
                    msg = f"{m['id']}: {exc}"
                    print(f"  FAILED — {msg}")
                    errors.append(msg)

        deleted_count = 0
        if delete_duplicates and dupes:
            print()
            print("=" * 80)
            print(f"Deleting {len(dupes)} duplicate-play junk row(s)...")
            print("=" * 80)
            for m in dupes:
                try:
                    conn.execute("BEGIN")
                    # Delete dependent rows first (scorecards, partnerships)
                    conn.execute("DELETE FROM match_scorecards WHERE match_id=?", (m['id'],))
                    conn.execute("DELETE FROM match_partnerships WHERE match_id=?", (m['id'],))
                    conn.execute("DELETE FROM matches WHERE id=?", (m['id'],))
                    conn.execute("COMMIT")
                    print(f"  Deleted {m['id']}")
                    deleted_count += 1
                except Exception as exc:
                    conn.execute("ROLLBACK")
                    msg = f"{m['id']}: {exc}"
                    print(f"  FAILED — {msg}")
                    errors.append(msg)

        if applied > 0 and affected_tournaments:
            print_standings(conn, affected_tournaments)

        print()
        print("=" * 80)
        print("SUMMARY")
        print(f"  Standings applied : {applied}")
        print(f"  Skipped           : {skipped}")
        print(f"  Duplicates deleted: {deleted_count}")
        if errors:
            print(f"  Errors            : {len(errors)}")
            for e in errors:
                print(f"    * {e}")
        print("=" * 80)

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Repair orphaned tournament match standings (no Flask required)."
    )
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"Path to SQLite DB (default: {DEFAULT_DB})")
    parser.add_argument("--apply", action="store_true",
                        help="Commit fixes (default: dry-run).")
    parser.add_argument("--delete-duplicates", action="store_true",
                        dest="delete_duplicates",
                        help="Delete duplicate-play junk rows.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: DB not found: {args.db}")
        sys.exit(1)

    run(args.db, apply=args.apply, delete_duplicates=args.delete_duplicates)
