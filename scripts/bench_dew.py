"""
Dew-factor benchmark: compare 2nd-innings ListA scores in D/N vs day matches.

For each pitch type, simulate N complete matches (both innings) with
is_day_night=True and is_day_night=False and report the difference.

Run from project root:
    python scripts/bench_dew.py
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


def _simulate_full_match(pitch, seed, is_day_night):
    """
    Simulate a complete match; return (1st_runs, 2nd_runs, 2nd_wickets,
    2nd_fours, 2nd_sixes).

    The match signals its end via match_over=True.  innings_end may or may
    not accompany it depending on how the 2nd innings concluded (overs up,
    all out, or target reached during a delivery).
    """
    random.seed(seed)
    data = {
        "match_id":        f"dew_{pitch}_{seed}_{'dn' if is_day_night else 'day'}",
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
        "is_day_night":    is_day_night,
    }
    match = match_module.Match(data)

    first_runs   = None
    second_runs  = None
    second_wkts  = None
    second_fours = None
    second_sixes = None

    # 5000 iterations is more than enough for 2 × 300-ball ListA innings
    for _ in range(5000):
        try:
            resp = match.next_ball()
        except Exception:
            break

        # 1st innings end — always accompanied by innings_end + innings_number=1
        if resp.get("innings_end") and resp.get("innings_number") == 1:
            first_runs = match.first_innings_score

        # Match end — covers all 2nd-innings finish paths:
        #   a) Natural (overs up)   → innings_end=True, innings_number=2, match_over=True
        #   b) All out              → match_over=True  (no innings_end)
        #   c) Target reached       → match_over=True  (no innings_end)
        if resp.get("match_over"):
            # Grab scorecard from response if present; fall back to match state
            sc      = resp.get("scorecard_data", {})
            players = sc.get("players", [])

            second_runs  = match.score
            second_wkts  = sc.get("wickets", match.wickets)

            # Player-level stats may be absent for the "innings==3 guard" branch
            if players:
                second_fours = sum((p.get("fours")  or 0) for p in players)
                second_sixes = sum((p.get("sixes")  or 0) for p in players)
            else:
                # Fall back: sum from batsman_stats dict on the match object
                bs = getattr(match, "batsman_stats", {})
                second_fours = sum(v.get("fours", 0) for v in bs.values())
                second_sixes = sum(v.get("sixes", 0) for v in bs.values())
            break

    return first_runs, second_runs, second_wkts, second_fours, second_sixes


PITCHES = ["Green", "Dry", "Hard", "Flat", "Dead"]
N = 40

print(f"\nListA Dew-Factor Benchmark — {N} complete matches per pitch per condition\n")
print(f"{'Pitch':<7}  {'Cond':<5}  {'1stAvg':>7}  {'2ndAvg':>7}  {'2ndMin':>7}  "
      f"{'2ndMax':>7}  {'2ndWkt':>7}  {'2nd4s':>6}  {'2nd6s':>6}  {'2ndRPO':>7}")
print("-" * 86)

results = {}
for pitch in PITCHES:
    results[pitch] = {}
    for dn in [False, True]:
        r1_l, r2_l, w2_l, f4_l, f6_l = [], [], [], [], []
        for seed in range(1, N + 1):
            r1, r2, w2, f4, f6 = _simulate_full_match(pitch, seed, dn)
            if r2 is not None:
                if r1 is not None:
                    r1_l.append(r1)
                r2_l.append(r2)
                w2_l.append(w2 or 0)
                f4_l.append(f4 or 0)
                f6_l.append(f6 or 0)

        if not r2_l:
            print(f"{pitch:<7}  {'D/N' if dn else 'Day':<5}  (no data)")
            continue

        avg_r1 = statistics.mean(r1_l) if r1_l else 0
        avg_r2 = statistics.mean(r2_l)
        avg_w2 = statistics.mean(w2_l)
        avg_4  = statistics.mean(f4_l)
        avg_6  = statistics.mean(f6_l)
        rpo_2  = avg_r2 / 50.0

        results[pitch][dn] = avg_r2

        label = "D/N" if dn else "Day"
        print(f"{pitch:<7}  {label:<5}  {avg_r1:>7.1f}  {avg_r2:>7.1f}  "
              f"{min(r2_l):>7}  {max(r2_l):>7}  {avg_w2:>7.2f}  "
              f"{avg_4:>6.1f}  {avg_6:>6.1f}  {rpo_2:>7.2f}")

    # Dew delta
    day_avg = results[pitch].get(False)
    dn_avg  = results[pitch].get(True)
    if day_avg is not None and dn_avg is not None:
        delta = dn_avg - day_avg
        sign  = "+" if delta >= 0 else ""
        tag   = "(dew advantage)" if delta > 0 else "(no dew swing)"
        print(f"{'':>7}  {'Dew+' if delta >= 0 else 'Dew-':<5}  {'':>7}  {sign}{delta:>6.1f}  {tag}")
    print()

print()
print("Interpretation:")
print("  Dew+ delta should be positive: dew from over 25 reduces wicket chance")
print("  and boosts boundaries/extras in the 2nd innings of D/N matches.")
