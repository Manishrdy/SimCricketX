"""Seeded T20/List A balance report; no database writes or network access.

Run: .venv/bin/python scripts/bench_psychological_risk.py --seeds 40
Uses the same balanced XI as the scoring calibration suite. Reports sampled
wicket-category probabilities (before fielding/free-hit resolution), not final
dismissal probabilities. Twelve-ball recovery windows exclude censored windows
at innings end and can therefore favor survivors; read alongside all-out rates.
"""
import argparse
from collections import defaultdict
import json
import logging
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_scoring_calibration import _match_data, PITCHES
from engine import ball_outcome
import engine.match as match_module


def simulate(fmt, pitch, seed):
    data = _match_data(fmt, pitch, seed)
    data['weather_script'] = {'forecast': 'clear', 'events': []}
    match = match_module.Match(data)
    innings, probabilities, windows = [], [], []
    recent, window = [], None
    original = ball_outcome.random.choices
    def sample(population, weights=None, **kwargs):
        if weights is not None and 'Wicket' in population and 'Dot' in population:
            probabilities.append(dict(zip(population, weights))['Wicket'])
        return original(population, weights=weights, **kwargs)
    # random_source exposes a dynamic __getattr__; restore its own namespace,
    # not an accidentally shadowed global random function.
    namespace = vars(ball_outcome.random)
    previous = namespace.get('choices')
    namespace['choices'] = sample
    try:
        for _ in range(1500):
            number = match.innings
            before_balls = match.current_over * match.balls_per_over + match.current_ball
            response = match.next_ball()
            if response.get('error'):
                raise AssertionError(response)
            end = response.get('innings_end') or response.get('match_over') or response.get('super_over_required')
            stats = match.first_innings_bowling_stats if end and number == 1 else match.bowler_stats
            legal = sum(s['balls_bowled'] for s in stats.values())
            wicket = bool((response.get('ball_data') or {}).get('batter_out'))
            if legal > before_balls:
                recent = [b for b in recent if legal - b < 18]
                if window is not None:
                    window['balls'] += 1
                    window['wickets'] += int(wicket)
                    if window['balls'] == 12:
                        windows.append(window['wickets'])
                        window = None
                elif wicket and recent:
                    window = dict(balls=0, wickets=0)
                if wicket:
                    recent.append(legal)
            if end:
                card = response.get('scorecard_data') or {}
                innings.append(dict(number=number,
                    runs=match.first_innings_score if number == 1 else match.score,
                    wickets=card.get('wickets', 0), balls=legal))
                recent, window = [], None
            if response.get('match_over') or response.get('super_over_required'):
                return dict(innings=innings, probabilities=probabilities, windows=windows,
                            chase_won=match.score > match.first_innings_score)
        raise AssertionError('Match did not finish')
    finally:
        if previous is None:
            namespace.pop('choices', None)
        else:
            namespace['choices'] = previous


def report(seeds=40, start_seed=5100):
    result = {}
    for fmt in ('T20', 'ListA'):
        for pitch in PITCHES:
            rows = [simulate(fmt, pitch, seed) for seed in range(start_seed, start_seed+seeds)]
            grouped = defaultdict(list)
            for row in rows:
                for innings in row['innings']:
                    grouped[innings['number']].append(innings)
            probabilities = [p for row in rows for p in row['probabilities']]
            windows = [w for row in rows for w in row['windows']]
            result[fmt+'/'+pitch] = dict(
                innings={str(n): dict(runs=statistics.mean(r['runs'] for r in rs),
                    wickets=statistics.mean(r['wickets'] for r in rs),
                    all_out_pct=100*statistics.mean(r['wickets'] == 10 for r in rs))
                    for n, rs in grouped.items()},
                chase_win_pct=100*statistics.mean(r['chase_won'] for r in rows),
                sampled_deliveries=len(probabilities),
                cap_hits=sum(p >= .4-1e-10 for p in probabilities),
                max_wicket_probability=max(probabilities),
                completed_recovery_windows=len(windows),
                wickets_next_12=statistics.mean(windows) if windows else None,
                wicket_free_recovery_pct=100*statistics.mean(w == 0 for w in windows) if windows else None)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=40)
    parser.add_argument('--start-seed', type=int, default=5100)
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error('--seeds must be positive')
    logging.disable(logging.CRITICAL)
    match_module.print = lambda *a, **k: None
    print(json.dumps(report(args.seeds, args.start_seed), indent=2))
