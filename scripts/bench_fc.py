"""
Full First-Class (FC) match simulation benchmark.

Simulates N matches per pitch type (5-day FC), and for every match records,
per innings: runs, wickets down, whether that innings ended via declaration
(Yes/No — innings 4 never declares), and overs played.

Writes one row per match to a CSV (--out, default scripts/fc_bench_results.csv)
and prints a per-pitch aggregate summary to stdout.

Run from project root:  python scripts/bench_fc.py
"""
import argparse
import copy
import csv
import logging
import random
import statistics
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.disable(logging.CRITICAL)

import engine.match as match_module
match_module.print = lambda *a, **k: None   # silence match prints


def _squad(prefix, pace=4, spin=2):
    names = [f"{prefix}_P{i+1}" for i in range(11)]
    pace_types = ["Fast", "Fast-medium", "Medium-fast", "Medium"]
    spin_types = ["Off spin", "Leg spin", "Finger spin", "Wrist spin"]
    players = []
    bowler_idx = 0
    for i, n in enumerate(names):
        is_pace = 5 <= i < 5 + pace
        is_spin = 5 + pace <= i < 5 + pace + spin
        will_bowl = is_pace or is_spin
        if is_pace:
            bt = pace_types[bowler_idx % len(pace_types)]
        elif is_spin:
            bt = spin_types[bowler_idx % len(spin_types)]
        else:
            bt = "Medium"
        if will_bowl:
            bowler_idx += 1
        players.append({
            "name": n,
            "role": "Bowler" if will_bowl else "Batsman",
            "batting_rating": 70, "bowling_rating": 72, "fielding_rating": 65,
            "technique_rating": 65, "temperament_rating": 65, "stamina_rating": 60,
            "batting_hand": "Right", "bowling_type": bt, "bowling_hand": "Right",
            "will_bowl": will_bowl, "is_captain": i == 0, "is_wicketkeeper": i == 4,
        })
    return players


HOME = _squad("HOM")
AWAY = _squad("AWY")


def _fc_match(pitch, seed, days=5):
    random.seed(seed)
    data = {
        "match_id": str(uuid.uuid4()), "created_by": "bench",
        "timestamp": "2026-08-09T12:00:00",
        "team_home": "HOM_bench", "team_away": "AWY_bench",
        "stadium": "Bench Ground", "pitch": pitch,
        "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
        "match_format": "FC", "days": days, "simulation_mode": "auto",
        "playing_xi": {"home": copy.deepcopy(HOME), "away": copy.deepcopy(AWAY)},
        "substitutes": {"home": [], "away": []},
        "weather_forecast": "clear",
    }
    return match_module.Match(data)


def _simulate_match(pitch, seed, days=5, limit=60000):
    m = _fc_match(pitch, seed, days=days)

    # Instrument the exact moment an innings is ruled over, BEFORE
    # _fc_transition_to_next_innings() resets fc_innings_declared / advances
    # fc_innings for the next innings. This is the only place that can
    # distinguish "declared" from "all out" — the response dict and the
    # post-call instance state have both already moved on by the time
    # next_ball() returns. Two endings never reach this hook at all (a
    # mid-over 10th-wicket fall, and a last-day time-exhaustion draw) —
    # both are by definition NOT declarations, so they're handled by the
    # "no entry recorded" default below.
    endings = {}  # innings_number -> declared bool
    orig_should_end = m._fc_innings_should_end

    def _wrapped_should_end():
        result = orig_should_end()
        if result:
            endings[m.fc_innings] = bool(m.wickets < 10 and m.fc_innings_declared)
        return result

    m._fc_innings_should_end = _wrapped_should_end

    innings = {}  # innings_number -> dict(runs, wickets, overs, declared)
    for _ in range(limit):
        resp = m.next_ball()
        if "error" in resp:
            return None
        if resp.get("innings_end"):
            n = resp.get("innings_number")
            sc = resp.get("scorecard_data", {}) or {}
            innings[n] = {
                "runs": sc.get("total_score", resp.get("final_score", 0)),
                "wickets": sc.get("wickets", resp.get("wickets", 0)),
                "overs": sc.get("overs", ""),
                "declared": endings.get(n, False),
            }
        if resp.get("match_over"):
            return {"innings": innings, "match_status": m.match_status}
    return None


PITCHES = ["Green", "Dry", "Hard", "Flat", "Dead"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-pitch", type=int, default=100)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "fc_bench_results.csv"))
    args = ap.parse_args()

    fieldnames = ["pitch", "seed", "match_status"]
    for n in (1, 2, 3, 4):
        fieldnames += [f"innings{n}_runs", f"innings{n}_wickets"]
        if n < 4:
            fieldnames.append(f"innings{n}_declared")
        fieldnames.append(f"innings{n}_overs")

    rows = []
    for pitch in PITCHES:
        for seed in range(1, args.per_pitch + 1):
            res = _simulate_match(pitch, seed, days=args.days)
            row = {"pitch": pitch, "seed": seed,
                   "match_status": res["match_status"] if res else "error"}
            innings = res["innings"] if res else {}
            for n in (1, 2, 3, 4):
                data = innings.get(n)
                row[f"innings{n}_runs"] = data["runs"] if data else ""
                row[f"innings{n}_wickets"] = data["wickets"] if data else ""
                if n < 4:
                    row[f"innings{n}_declared"] = ("Yes" if data["declared"] else "No") if data else ""
                row[f"innings{n}_overs"] = data["overs"] if data else ""
            rows.append(row)

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"\nWrote {len(rows)} match rows to {args.out}\n")

    print(f"FC benchmark  ({args.per_pitch} games per pitch, {args.days}-day matches)\n")
    hdr = f"{'Pitch':<7}  {'Games':>5}"
    for n in (1, 2, 3, 4):
        hdr += f"  {'Inn'+str(n)+'Runs':>10}  {'Inn'+str(n)+'Wkts':>9}"
        if n < 4:
            hdr += f"  {'Inn'+str(n)+'Decl%':>9}"
    print(hdr)
    print("-" * len(hdr))

    declared_counts = {n: {p: 0 for p in PITCHES} for n in (1, 2, 3)}
    reached_counts = {n: {p: 0 for p in PITCHES} for n in (1, 2, 3, 4)}

    for pitch in PITCHES:
        prows = [r for r in rows if r["pitch"] == pitch and r["match_status"] != "error"]
        line = f"{pitch:<7}  {len(prows):>5}"
        for n in (1, 2, 3, 4):
            runs_l = [r[f"innings{n}_runs"] for r in prows if r[f"innings{n}_runs"] != ""]
            wkts_l = [r[f"innings{n}_wickets"] for r in prows if r[f"innings{n}_wickets"] != ""]
            reached_counts[n][pitch] = len(runs_l)
            avg_r = statistics.mean(runs_l) if runs_l else 0
            avg_w = statistics.mean(wkts_l) if wkts_l else 0
            line += f"  {avg_r:>10.1f}  {avg_w:>9.2f}"
            if n < 4:
                decl_l = [r[f"innings{n}_declared"] for r in prows if r[f"innings{n}_declared"] != ""]
                declared_counts[n][pitch] = decl_l.count("Yes")
                decl_pct = (decl_l.count("Yes") / len(decl_l) * 100) if decl_l else 0
                line += f"  {decl_pct:>8.0f}%"
        print(line)

    print()
    for n in (1, 2, 3):
        total_reached = sum(reached_counts[n].values())
        total_declared = sum(declared_counts[n].values())
        print(f"Innings {n} declarations: {total_declared} / {total_reached} games that reached innings {n}")
        for p in PITCHES:
            if reached_counts[n][p]:
                print(f"    {p}: {declared_counts[n][p]} / {reached_counts[n][p]}")
    print()


if __name__ == "__main__":
    main()
