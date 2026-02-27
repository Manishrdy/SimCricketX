"""
Quick benchmark: 100 first-innings ListA simulations per pitch type.
Run from project root:  python scripts/bench_lista.py
"""
import random
import statistics
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.match as match_module
match_module.print = lambda *a, **k: None   # silence match prints


def _build_players(prefix):
    players = []
    for i in range(11):
        if i < 6:
            bat  = 78 - i * 2
            bowl = 45 + i
            role = "Batsman"
            will_bowl    = i >= 4
            bowling_type = "Medium-fast" if i >= 4 else "Medium"
        else:
            bat  = 48 - (i - 6) * 2
            bowl = 74 - (i - 6) * 3
            role = "Bowler"
            will_bowl    = True
            bowling_type = ["Fast", "Fast-medium", "Medium-fast", "Off spin", "Leg spin"][min(i - 6, 4)]
        players.append({
            "name":           f"{prefix}_P{i+1}",
            "role":           role,
            "batting_rating": max(20, bat),
            "bowling_rating": max(20, bowl),
            "fielding_rating": 70,
            "batting_hand":   "Right" if i % 3 else "Left",
            "bowling_type":   bowling_type,
            "bowling_hand":   "Right" if i % 2 else "Left",
            "will_bowl":      will_bowl,
            "is_captain":     i == 0,
        })
    bowling_options = [p for p in players if p["will_bowl"]]
    for p in players:
        p["will_bowl"] = False
    for p in bowling_options[:5]:
        p["will_bowl"] = True
    return players


def _simulate_first_innings(pitch, seed):
    random.seed(seed)
    data = {
        "match_id":        f"bench_{pitch}_{seed}",
        "created_by":      "bench",
        "team_home":       "HOM_bench",
        "team_away":       "AWY_bench",
        "stadium":         "Bench Ground",
        "pitch":           pitch,
        "toss":            "Heads",
        "toss_winner":     "HOM",
        "toss_decision":   "Bat",
        "simulation_mode": "auto",
        "match_format":    "ListA",
        "playing_xi":      {"home": _build_players("H"), "away": _build_players("A")},
        "substitutes":     {"home": [], "away": []},
        "is_day_night":    False,
    }
    match = match_module.Match(data)
    for _ in range(2000):
        resp = match.next_ball()
        if resp.get("innings_end") and resp.get("innings_number") == 1:
            sc       = resp.get("scorecard_data", {})
            players  = sc.get("players", [])
            runs     = match.first_innings_score
            wickets  = sc.get("wickets", 0)
            fours    = sum((p.get("fours")  or 0) for p in players)
            sixes    = sum((p.get("sixes")  or 0) for p in players)
            return runs, wickets, fours, sixes
    return None, None, None, None


PITCHES = ["Green", "Dry", "Hard", "Flat", "Dead"]
N = 100

print(f"\nListA first-innings benchmark  ({N} simulations per pitch)\n")
print(f"{'Pitch':<7}  {'AvgRuns':>8}  {'MinRuns':>8}  {'MaxRuns':>8}  {'AvgWkts':>8}  {'Avg4s':>7}  {'Avg6s':>7}  {'AvgRPO':>8}")
print("-" * 72)

for pitch in PITCHES:
    runs_l, wkts_l, fours_l, sixes_l = [], [], [], []
    for seed in range(1, N + 1):
        r, w, f4, f6 = _simulate_first_innings(pitch, seed)
        if r is not None:
            runs_l.append(r)
            wkts_l.append(w)
            fours_l.append(f4)
            sixes_l.append(f6)

    avg_r  = statistics.mean(runs_l)
    avg_w  = statistics.mean(wkts_l)
    avg_4  = statistics.mean(fours_l)
    avg_6  = statistics.mean(sixes_l)
    avg_rpo = avg_r / 50.0

    print(f"{pitch:<7}  {avg_r:>8.1f}  {min(runs_l):>8}  {max(runs_l):>8}  "
          f"{avg_w:>8.2f}  {avg_4:>7.1f}  {avg_6:>7.1f}  {avg_rpo:>8.2f}")

print()
