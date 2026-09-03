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
import collections
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


# A realistic first-class XI shape. A uniform-rated squad is useless as a
# calibration instrument: with no tail, every side bats like six No. 3s and
# totals come out far above anything a real team makes. Positions carry the
# spread a real XI has — specialist top six, a keeper, an all-rounder, then
# a genuine tail.
FC_XI = [
    # (bat, bowl, technique, temperament, stamina, role, bowling_type, will_bowl)
    (74, 20, 76, 74, 55, "Batsman",      "Medium",      False),
    (72, 20, 74, 72, 55, "Batsman",      "Medium",      False),
    (78, 25, 80, 78, 55, "Batsman",      "Medium",      False),
    (76, 30, 76, 76, 55, "Batsman",      "Off spin",    False),
    (70, 20, 70, 70, 55, "Batsman",      "Medium",      False),
    (64, 30, 64, 66, 55, "Wicketkeeper", "Medium",      False),
    (58, 68, 58, 62, 68, "All-rounder",  "Fast-medium", True),
    (40, 74, 42, 50, 72, "Bowler",       "Fast",        True),
    (28, 76, 30, 45, 70, "Bowler",       "Fast-medium", True),
    (20, 72, 22, 40, 74, "Bowler",       "Off spin",    True),
    (12, 70, 14, 38, 74, "Bowler",       "Leg spin",    True),
]


def _squad(prefix):
    players = []
    for i, (bat, bowl, tech, temp, stam, role, btype, wb) in enumerate(FC_XI):
        players.append({
            "name": f"{prefix}_P{i+1}", "role": role,
            "batting_rating": bat, "bowling_rating": bowl, "fielding_rating": 68,
            "technique_rating": tech, "temperament_rating": temp,
            "stamina_rating": stam,
            "batting_hand": "Left" if i in (1, 4, 8) else "Right",
            "bowling_type": btype, "bowling_hand": "Right" if i % 2 else "Left",
            "will_bowl": wb, "is_captain": i == 0, "is_wicketkeeper": i == 5,
        })
    return players


HOME = _squad("HOM")
AWAY = _squad("AWY")


# Real first-class cricket is not played under permanent sunshine, and time
# lost to weather is a major reason matches are drawn. Benchmarking every
# game as "clear" understated the draw rate by about a third (16% vs 22%).
# This mix is the default; --forecast pins a single tier when you want to
# isolate the scoring model from the weather.
FORECAST_MIX = ["clear", "clear", "passing_showers", "passing_showers", "rain_around"]


def _forecast_for(seed, override=None):
    return override or FORECAST_MIX[seed % len(FORECAST_MIX)]


def _fc_match(pitch, seed, days=5, forecast=None, is_day_night=False):
    random.seed(seed)
    data = {
        "match_id": str(uuid.uuid4()), "created_by": "bench",
        "timestamp": "2026-08-09T12:00:00",
        "team_home": "HOM_bench", "team_away": "AWY_bench",
        "stadium": "Bench Ground", "pitch": pitch,
        "toss": "Heads", "toss_winner": "HOM", "toss_decision": "Bat",
        "match_format": "FC", "days": days, "simulation_mode": "auto",
        "is_day_night": is_day_night,
        "playing_xi": {"home": copy.deepcopy(HOME), "away": copy.deepcopy(AWAY)},
        "substitutes": {"home": [], "away": []},
        "weather_forecast": _forecast_for(seed, forecast),
    }
    return match_module.Match(data)


def _simulate_match(pitch, seed, days=5, limit=60000, forecast=None,
                    is_day_night=False):
    m = _fc_match(pitch, seed, days=days, forecast=forecast,
                  is_day_night=is_day_night)

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
    knocks = []   # every individual innings played in the match
    extras = collections.Counter()   # extra_type -> count, from the ball stream
    stands = []   # runs in each partnership, recorded as it is broken
    pace_wickets_session3 = 0
    bowling_types = {
        player["name"]: (player.get("bowling_type") or "")
        for player in HOME + AWAY
    }
    for _ in range(limit):
        _partnership_before = m.current_partnership_runs
        _session_before = m._fc_current_session()
        resp = m.next_ball()
        if "error" in resp:
            return None
        _bd = resp.get("ball_data") or {}
        if _bd.get("is_extra") and _bd.get("extra_type"):
            extras[_bd["extra_type"]] += 1
        if _bd.get("batter_out"):
            stands.append(_partnership_before)
            if (_session_before == 3
                    and bowling_types.get(_bd.get("bowler"))
                    not in {"Off spin", "Leg spin", "Finger spin", "Wrist spin"}):
                pace_wickets_session3 += 1
        if resp.get("innings_end"):
            n = resp.get("innings_number")
            sc = resp.get("scorecard_data", {}) or {}
            innings[n] = {
                "runs": sc.get("total_score", resp.get("final_score", 0)),
                "wickets": sc.get("wickets", resp.get("wickets", 0)),
                "overs": sc.get("overs", ""),
                "declared": endings.get(n, False),
            }
            # Individual innings, for the score distribution and the
            # dismissal mix. Batting position comes from the squad naming
            # (`HOM_P7` -> 7) rather than the scorecard's row order, which
            # only lists players who actually batted.
            for row in (sc.get("players") or []):
                name = row.get("name") or ""
                try:
                    pos = int(name.rsplit("_P", 1)[1])
                except (IndexError, ValueError):
                    pos = 0
                knocks.append({
                    "pos": pos,
                    "runs": row.get("runs", 0) or 0,
                    "balls": row.get("balls", 0) or 0,
                    "wicket_type": (row.get("wicket_type") or "").strip(),
                })

        if resp.get("match_over"):
            total_runs = sum(row["runs"] for row in innings.values())
            total_overs = sum(_overs_to_float(row["overs"]) for row in innings.values())
            return {"innings": innings, "match_status": m.match_status,
                    "knocks": knocks, "extras": extras, "stands": stands,
                    "pace_wickets_session3": pace_wickets_session3,
                    "total_runs": total_runs, "total_overs": total_overs}
    return None


PITCHES = ["Green", "Dry", "Hard", "Flat", "Dead"]


def _overs_to_float(value):
    """'82.3' (82 overs 3 balls) -> 82.5 overs."""
    if not value:
        return 0.0
    whole, _, part = str(value).partition(".")
    return int(whole or 0) + int(part or 0) / 6.0



# Real first-class reference points, for judging the numbers rather than
# just reading them. Sources are ordinary FC/Test aggregates.
REAL_DISMISSAL_MIX = {
    "Caught": 57.0, "Bowled": 21.0, "LBW": 15.0, "Run Out": 3.5, "Stumped": 2.5,
}
REAL_EXTRAS_MIX = {
    "Leg Bye": 38.0, "Byes": 26.0, "Wide": 18.0, "No Ball": 18.0,
}


def _print_batting_profile(knocks):
    """Hundreds, fifties and ducks per 100 innings, and where the runs come
    from. A tail that contributes like a top order is the loudest sign the
    skill contest has gone flat."""
    if not knocks:
        return
    completed = [k for k in knocks if k["balls"] > 0 or k["wicket_type"]]
    n = max(len(completed), 1)
    hundreds = sum(1 for k in completed if k["runs"] >= 100)
    fifties = sum(1 for k in completed if 50 <= k["runs"] < 100)
    ducks = sum(1 for k in completed if k["runs"] == 0 and k["wicket_type"])
    print(f"\nBatting profile ({len(completed)} individual innings)")
    print(f"    per 100 innings: {hundreds / n * 100:5.1f} hundreds   "
          f"{fifties / n * 100:5.1f} fifties   {ducks / n * 100:5.1f} ducks")
    print(f"    real FC roughly:   3-5 hundreds    10-13 fifties    "
          f"10-14 ducks")

    top = [k for k in completed if 1 <= k["pos"] <= 6]
    tail = [k for k in completed if 9 <= k["pos"] <= 11]
    top_runs = sum(k["runs"] for k in top)
    tail_runs = sum(k["runs"] for k in tail)
    total_runs = max(sum(k["runs"] for k in completed), 1)
    # Shares are of BATTER runs, not team runs — extras are excluded, which
    # lifts every share by a few points against the figures usually quoted.
    print(f"    share of runs:   top six {top_runs / total_runs * 100:.0f}%   "
          f"nos 9-11 {tail_runs / total_runs * 100:.0f}%   "
          f"(real FC ~72-77% / ~8-10%, of batter runs)")

    print(f"\n    {'pos':>4} {'avg':>7} {'SR':>7} {'inns':>6}")
    for pos in range(1, 12):
        at = [k for k in completed if k["pos"] == pos]
        if not at:
            continue
        outs = sum(1 for k in at if k["wicket_type"])
        runs = sum(k["runs"] for k in at)
        balls = max(sum(k["balls"] for k in at), 1)
        print(f"    {pos:>4} {runs / max(outs, 1):7.1f} "
              f"{runs / balls * 100:7.1f} {len(at):6d}")


def _print_dismissal_mix(knocks):
    """How batters actually got out, against how they get out in the real
    game. A quarter of spin wickets being stumpings is the first thing a
    cricket person spots on a scorecard."""
    got_out = [k for k in knocks if k["wicket_type"]]
    if not got_out:
        return
    counts = collections.Counter(k["wicket_type"] for k in got_out)
    total = len(got_out)
    print(f"\nDismissal mix ({total} wickets)")
    print(f"    {'type':<12} {'sim':>7} {'real FC':>9}")
    for kind in sorted(counts, key=lambda k: -counts[k]):
        real = REAL_DISMISSAL_MIX.get(kind)
        real_s = f"{real:.1f}%" if real is not None else "-"
        print(f"    {kind:<12} {counts[kind] / total * 100:6.1f}% {real_s:>9}")


def _print_partnerships(stands):
    """How stands are distributed. A long partnership is the passage of play
    that breaks an attack's back, so it should be visible here as well as
    felt in the wicket odds."""
    stands = sorted(s for s in stands if s is not None)
    if not stands:
        return
    n = len(stands)
    print(f"\nPartnerships ({n} broken)")
    print(f"    median {stands[n // 2]}   p75 {stands[int(n * 0.75)]}   "
          f"p90 {stands[int(n * 0.90)]}   best {stands[-1]}")
    print(f"    reaching 50: {sum(1 for s in stands if s >= 50) / n * 100:4.1f}%   "
          f"reaching 100: {sum(1 for s in stands if s >= 100) / n * 100:4.1f}%")
    print(f"    real FC:      ~18-22%           ~5-7%")


def _print_extras_mix(extras):
    total = sum(extras.values())
    if not total:
        return
    print(f"\nExtras mix ({total} extras)")
    print(f"    {'type':<12} {'sim':>7} {'real FC':>9}")
    for kind in sorted(extras, key=lambda k: -extras[k]):
        real = REAL_EXTRAS_MIX.get(kind)
        real_s = f"{real:.1f}%" if real is not None else "-"
        print(f"    {kind:<12} {extras[kind] / total * 100:6.1f}% {real_s:>9}")


def _compare_day_night(per_pitch, days, forecast):
    """Paired-seed calibration for the pink-ball model."""
    paired = []
    for pitch in PITCHES:
        for seed in range(1, per_pitch + 1):
            day = _simulate_match(pitch, seed, days=days, forecast=forecast,
                                  is_day_night=False)
            night = _simulate_match(pitch, seed, days=days, forecast=forecast,
                                    is_day_night=True)
            if day and night:
                paired.append((day, night))

    def aggregate(index):
        rows = [pair[index] for pair in paired]
        runs = sum(row["total_runs"] for row in rows)
        overs = sum(row["total_overs"] for row in rows)
        return {
            "pace_wickets_session3": sum(row["pace_wickets_session3"] for row in rows),
            "rpo": runs / overs if overs else 0.0,
            "draw_pct": (sum(row["match_status"] == "drawn" for row in rows)
                         / max(len(rows), 1) * 100),
        }

    day, night = aggregate(0), aggregate(1)
    rpo_delta = ((night["rpo"] / day["rpo"] - 1) * 100) if day["rpo"] else 0.0
    draw_delta = night["draw_pct"] - day["draw_pct"]
    print(f"FC day/night paired benchmark ({len(paired)} seed pairs)")
    print(f"Third-session pace wickets: day {day['pace_wickets_session3']}, "
          f"day/night {night['pace_wickets_session3']}")
    print(f"Runs per over: day {day['rpo']:.2f}, day/night {night['rpo']:.2f} "
          f"({rpo_delta:+.1f}%)")
    print(f"Draw rate: day {day['draw_pct']:.1f}%, day/night {night['draw_pct']:.1f}% "
          f"({draw_delta:+.1f} points)")
    if len(paired) < 100:
        print("Guardrails: NOT EVALUATED (fewer than 100 seed pairs; "
              "use --per-pitch 20 or more for calibration)")
    else:
        print("Guardrails: "
              f"pace advantage={'PASS' if night['pace_wickets_session3'] > day['pace_wickets_session3'] else 'FAIL'}, "
              f"RPO={'PASS' if abs(rpo_delta) <= 8 else 'FAIL'}, "
              f"draw rate={'PASS' if abs(draw_delta) <= 5 else 'FAIL'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-pitch", type=int, default=100)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "fc_bench_results.csv"))
    ap.add_argument("--forecast", default=None,
                    help="pin one weather tier (clear/passing_showers/rain_around/"
                         "storm_warning) instead of the realistic mix")
    ap.add_argument("--compare-day-night", action="store_true",
                    help="run paired day vs pink-ball day/night calibration and exit")
    args = ap.parse_args()

    if args.compare_day_night:
        _compare_day_night(args.per_pitch, args.days, args.forecast)
        return

    fieldnames = ["pitch", "seed", "match_status"]
    for n in (1, 2, 3, 4):
        fieldnames += [f"innings{n}_runs", f"innings{n}_wickets"]
        if n < 4:
            fieldnames.append(f"innings{n}_declared")
        fieldnames.append(f"innings{n}_overs")

    rows = []
    all_knocks = []
    all_extras = collections.Counter()
    all_stands = []
    for pitch in PITCHES:
        for seed in range(1, args.per_pitch + 1):
            res = _simulate_match(pitch, seed, days=args.days,
                                  forecast=args.forecast)
            if res:
                all_knocks.extend(res.get("knocks") or [])
                all_extras.update(res.get("extras") or {})
                all_stands.extend(res.get("stands") or [])
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

    # Result distribution and the whole-match economy — the numbers that say
    # whether this reads like first-class cricket at all.
    print()
    status_counts = collections.Counter(r["match_status"] for r in rows)
    total_runs = total_wkts = 0
    total_overs = 0.0
    for r in rows:
        for n in (1, 2, 3, 4):
            if r[f"innings{n}_runs"] == "":
                continue
            total_runs += int(r[f"innings{n}_runs"])
            total_wkts += int(r[f"innings{n}_wickets"])
            total_overs += _overs_to_float(r[f"innings{n}_overs"])
    played = max(len(rows), 1)
    print(f"Results: " + ", ".join(f"{k} {v} ({v / played * 100:.0f}%)"
                                   for k, v in sorted(status_counts.items())))
    for p in PITCHES:
        prows = [r for r in rows if r["pitch"] == p]
        drawn = sum(1 for r in prows if r["match_status"] == "drawn")
        print(f"    {p:<7} drawn {drawn}/{len(prows)}")
    if total_overs and total_wkts:
        print(f"\nEconomy: {total_runs / total_overs:.2f} RPO, "
              f"{total_runs / total_wkts:.1f} runs/wicket, "
              f"{total_overs * 6 / total_wkts:.1f} balls/wicket")

    _print_batting_profile(all_knocks)
    _print_dismissal_mix(all_knocks)
    _print_extras_mix(all_extras)
    _print_partnerships(all_stands)

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
