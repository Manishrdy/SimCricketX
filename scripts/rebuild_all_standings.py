#!/usr/bin/env python3
"""
One-time rebuild of every tournament's standings from match data.

Iterates every tournament, zeroes its tournament_teams rows, then replays
every league-stage match through the same point/NRR rules the engine uses
at runtime — but with the defensive overs cap applied (so legacy "20.1 in
a 20-over match" rows no longer skew NRR).

This connects DIRECTLY to the SQLite file. No Flask, no SQLAlchemy. Run
against a backup first; `--apply` is opt-in.

Usage:
    python3 scripts/rebuild_all_standings.py                  # dry-run, all tournaments
    python3 scripts/rebuild_all_standings.py --apply          # commit changes
    python3 scripts/rebuild_all_standings.py --tournament 45  # single tournament
    python3 scripts/rebuild_all_standings.py --db /path/to/cricket_sim.db --apply
    python3 scripts/rebuild_all_standings.py --quiet --apply  # suppress per-team rows
"""

from __future__ import annotations

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

LEAGUE_STAGE = "league"

NO_RESULT_KEYWORDS = (
    "abandoned", "no result", "washed out", "called off",
    "no play", "n/r",
)


# ---------------------------------------------------------------------------
# Cricket overs helpers — mirror tournament_engine.py exactly
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
    return balls_to_overs(overs_to_balls(o1 or "0.0") + overs_to_balls(o2 or "0.0"))


def nrr_overs(actual_overs, wickets, overs_per_side):
    """ICC all-out rule + defensive cap on overs > full quota."""
    ops = int(overs_per_side or 20)
    if wickets is not None and int(wickets) >= 10:
        return f"{ops}.0"
    actual = actual_overs or "0.0"
    if overs_to_balls(actual) > ops * 6:
        return f"{ops}.0"
    return actual


def calc_nrr(runs_scored, overs_faced, runs_conceded, overs_bowled):
    bf = overs_to_balls(overs_faced)
    bb = overs_to_balls(overs_bowled)
    rrf = (runs_scored or 0) / (bf / 6.0) if bf > 0 else 0.0
    rra = (runs_conceded or 0) / (bb / 6.0) if bb > 0 else 0.0
    return round(rrf - rra, 6)


def is_no_result(match):
    """Mirror engine._is_no_result: winner=None + (NR keyword OR no balls)."""
    if match["winner_team_id"]:
        return False
    desc = (match["result_description"] or "").lower()
    if any(kw in desc for kw in NO_RESULT_KEYWORDS):
        return True
    if (
        overs_to_balls(match["home_team_overs"]) == 0
        and overs_to_balls(match["away_team_overs"]) == 0
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Per-tournament rebuild
# ---------------------------------------------------------------------------

def fresh_team_state(team_id):
    return {
        "team_id": team_id,
        "played": 0, "won": 0, "lost": 0, "tied": 0, "no_result": 0,
        "points": 0,
        "runs_scored": 0, "overs_faced": "0.0",
        "runs_conceded": 0, "overs_bowled": "0.0",
        "net_run_rate": 0.0,
    }


def apply_match(home, away, match):
    """Apply one league match's deltas to the two team states (in place)."""
    home["played"] += 1
    away["played"] += 1

    nr = is_no_result(match)
    wid = match["winner_team_id"]

    if nr:
        home["no_result"] += 1
        away["no_result"] += 1
        home["points"] += POINTS_NO_RESULT
        away["points"] += POINTS_NO_RESULT
    elif wid == match["home_team_id"]:
        home["won"] += 1
        home["points"] += POINTS_WIN
        away["lost"] += 1
    elif wid == match["away_team_id"]:
        away["won"] += 1
        away["points"] += POINTS_WIN
        home["lost"] += 1
    else:
        home["tied"] += 1
        away["tied"] += 1
        home["points"] += POINTS_TIE
        away["points"] += POINTS_TIE

    if nr:
        return  # NRR not updated for no-results

    ops = match["overs_per_side"]
    h_overs = nrr_overs(match["home_team_overs"], match["home_team_wickets"], ops)
    a_overs = nrr_overs(match["away_team_overs"], match["away_team_wickets"], ops)

    # Home batting / away bowling
    home["runs_scored"] += match["home_team_score"] or 0
    home["overs_faced"] = add_overs(home["overs_faced"], h_overs)
    away["runs_conceded"] += match["home_team_score"] or 0
    away["overs_bowled"] = add_overs(away["overs_bowled"], h_overs)

    # Away batting / home bowling
    away["runs_scored"] += match["away_team_score"] or 0
    away["overs_faced"] = add_overs(away["overs_faced"], a_overs)
    home["runs_conceded"] += match["away_team_score"] or 0
    home["overs_bowled"] = add_overs(home["overs_bowled"], a_overs)

    home["net_run_rate"] = calc_nrr(
        home["runs_scored"], home["overs_faced"],
        home["runs_conceded"], home["overs_bowled"],
    )
    away["net_run_rate"] = calc_nrr(
        away["runs_scored"], away["overs_faced"],
        away["runs_conceded"], away["overs_bowled"],
    )


def rebuild_tournament(conn, tournament, *, quiet=False):
    """Return (per_team_diff, league_match_count)."""
    tid = tournament["id"]

    # Tournament teams (skip BYE/TBD placeholders)
    rows = conn.execute(
        """
        SELECT tt.id AS tt_id, tt.team_id, t.short_code, t.is_placeholder,
               tt.played, tt.won, tt.lost, tt.tied, tt.no_result, tt.points,
               tt.runs_scored, tt.overs_faced, tt.runs_conceded, tt.overs_bowled,
               tt.net_run_rate
        FROM tournament_teams tt
        JOIN teams t ON t.id = tt.team_id
        WHERE tt.tournament_id = ?
        """,
        (tid,),
    ).fetchall()

    states = {}
    before = {}
    tt_id_by_team = {}
    for r in rows:
        if r["is_placeholder"]:
            continue
        states[r["team_id"]] = fresh_team_state(r["team_id"])
        before[r["team_id"]] = {
            "name": r["short_code"],
            "played": r["played"], "won": r["won"], "lost": r["lost"],
            "tied": r["tied"], "no_result": r["no_result"], "points": r["points"],
            "runs_scored": r["runs_scored"], "overs_faced": r["overs_faced"],
            "runs_conceded": r["runs_conceded"], "overs_bowled": r["overs_bowled"],
            "net_run_rate": r["net_run_rate"] or 0.0,
        }
        tt_id_by_team[r["team_id"]] = r["tt_id"]

    # League fixtures with linked completed matches
    matches = conn.execute(
        """
        SELECT m.id, m.home_team_id, m.away_team_id, m.winner_team_id,
               m.home_team_score, m.home_team_wickets, m.home_team_overs,
               m.away_team_score, m.away_team_wickets, m.away_team_overs,
               COALESCE(m.overs_per_side, 20) AS overs_per_side,
               m.result_description, m.date
        FROM tournament_fixtures tf
        JOIN matches m ON m.id = tf.match_id
        WHERE tf.tournament_id = ? AND tf.stage = ?
          AND tf.status = 'Completed'
        ORDER BY COALESCE(m.date, tf.id)
        """,
        (tid, LEAGUE_STAGE),
    ).fetchall()

    skipped = 0
    for m in matches:
        h, a = m["home_team_id"], m["away_team_id"]
        if h not in states or a not in states:
            # Match references a team that isn't in this tournament's roster
            # (e.g. data drift). Skip rather than corrupt.
            skipped += 1
            continue
        apply_match(states[h], states[a], dict(m))

    diffs = []
    for team_id, s in states.items():
        b = before[team_id]
        diffs.append({
            "team_id": team_id,
            "name": b["name"],
            "before": b,
            "after": {
                "played": s["played"], "won": s["won"], "lost": s["lost"],
                "tied": s["tied"], "no_result": s["no_result"], "points": s["points"],
                "runs_scored": s["runs_scored"], "overs_faced": s["overs_faced"],
                "runs_conceded": s["runs_conceded"], "overs_bowled": s["overs_bowled"],
                "net_run_rate": s["net_run_rate"],
            },
            "tt_id": tt_id_by_team[team_id],
        })

    if not quiet:
        print_tournament_diff(tournament, diffs, len(matches), skipped)

    return diffs, len(matches)


def write_team_states(conn, diffs):
    cur = conn.cursor()
    for d in diffs:
        a = d["after"]
        cur.execute(
            """
            UPDATE tournament_teams
            SET played=?, won=?, lost=?, tied=?, no_result=?, points=?,
                runs_scored=?, overs_faced=?, runs_conceded=?, overs_bowled=?,
                net_run_rate=?
            WHERE id=?
            """,
            (
                a["played"], a["won"], a["lost"], a["tied"], a["no_result"],
                a["points"], a["runs_scored"], a["overs_faced"],
                a["runs_conceded"], a["overs_bowled"], a["net_run_rate"],
                d["tt_id"],
            ),
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_tournament_diff(tournament, diffs, n_matches, skipped):
    name = tournament["name"] or f"Tournament {tournament['id']}"
    print(f"\n=== Tournament {tournament['id']}: {name} "
          f"({tournament['status']}) ===")
    print(f"    {n_matches} league match(es) replayed"
          + (f", {skipped} skipped (team not in roster)" if skipped else ""))
    if not diffs:
        print("    (no non-placeholder teams)")
        return

    # Sort by post-rebuild standings
    diffs.sort(
        key=lambda d: (-d["after"]["points"], -d["after"]["net_run_rate"])
    )

    header = f"    {'Team':<6}  {'P':>2} {'W':>2} {'L':>2} {'T':>2} {'NR':>2} {'Pts':>4}  {'NRR':>8}  {'Δ NRR':>8}"
    print(header)
    print("    " + "-" * (len(header) - 4))
    for d in diffs:
        a, b = d["after"], d["before"]
        delta = a["net_run_rate"] - (b["net_run_rate"] or 0.0)
        print(
            f"    {d['name']:<6}  "
            f"{a['played']:>2} {a['won']:>2} {a['lost']:>2} "
            f"{a['tied']:>2} {a['no_result']:>2} {a['points']:>4}  "
            f"{a['net_run_rate']:>+8.4f}  {delta:>+8.4f}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to SQLite DB")
    parser.add_argument("--apply", action="store_true",
                        help="Commit changes (default: dry-run)")
    parser.add_argument("--tournament", type=int, default=None,
                        help="Rebuild only this tournament id")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-team rows; print summary only")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: DB not found at {args.db}", file=sys.stderr)
        sys.exit(2)

    print(f"DB: {args.db}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN (use --apply to commit)'}")
    if args.tournament:
        print(f"Tournament filter: {args.tournament}")
    if args.apply:
        print("WARNING: --apply will overwrite tournament_teams rows.")
        print("         Ensure you have a backup before continuing.")
        try:
            confirm = input("Type 'yes' to proceed: ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm != "yes":
            print("Aborted.")
            sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    where = "WHERE id = ?" if args.tournament else ""
    params = (args.tournament,) if args.tournament else ()
    tournaments = conn.execute(
        f"SELECT id, name, status FROM tournaments {where} ORDER BY id",
        params,
    ).fetchall()

    if not tournaments:
        print("No tournaments found.")
        return

    total_matches = 0
    total_team_rows = 0
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        for t in tournaments:
            diffs, n = rebuild_tournament(conn, t, quiet=args.quiet)
            total_matches += n
            total_team_rows += len(diffs)
            if args.apply:
                write_team_states(conn, diffs)

        if args.apply:
            conn.commit()
            print(f"\n✓ Committed. {len(tournaments)} tournament(s), "
                  f"{total_team_rows} team row(s), "
                  f"{total_matches} league match(es) replayed.")
        else:
            conn.rollback()
            print(f"\nDry-run complete. {len(tournaments)} tournament(s), "
                  f"{total_team_rows} team row(s), "
                  f"{total_matches} league match(es) would be replayed.")
            print("Pass --apply to commit.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
