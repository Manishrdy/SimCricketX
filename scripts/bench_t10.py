"""Deterministic T10 calibration, using the established scoring-regression XI.

Run: .venv/bin/python scripts/bench_t10.py --seeds 120
No database writes or network access. Reports both innings and day/night effects.
"""
import argparse
import collections
import json
import logging
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_scoring_calibration import _match_data, _simulate_first_innings, PITCHES
import engine.match as match_module

BANDS = {'Green': (70, 95), 'Dry': (75, 100), 'Hard': (100, 125),
         'Flat': (115, 145), 'Dead': (130, 165)}


def simulate(pitch, seed, night=False):
    data = _match_data('T10', pitch, seed)
    data.update(is_day_night=night, weather_script={'forecast': 'clear', 'events': []})
    match = match_module.Match(data)
    innings = []
    counters = collections.Counter()
    phases = collections.defaultdict(collections.Counter)
    bowlers = {}
    for _ in range(500):
        number, over = match.innings, match.current_over
        phase = match.fmt.phase_key(over)
        before_score, before_balls = match.score, over * 6 + match.current_ball
        response = match.next_ball()
        if response.get('error'):
            raise AssertionError(response)
        end = response.get('innings_end') or response.get('match_over') or response.get('super_over_required')
        stats = match.first_innings_bowling_stats if end and number == 1 else match.bowler_stats
        legal = sum(s['balls_bowled'] for s in stats.values())
        total = match.first_innings_score if end and number == 1 else match.score
        ball = response.get('ball_data') or {}
        delta = legal - before_balls
        phases[phase]['runs'] += total - before_score
        phases[phase]['legal_balls'] += delta
        counters['dots'] += int(delta > 0 and total == before_score)
        counters['extras'] += int(bool(ball.get('is_extra')))
        if not ball.get('is_extra'):
            counters['fours'] += int(ball.get('runs') == 4)
            counters['sixes'] += int(ball.get('runs') == 6)
        assert all(s['balls_bowled'] <= 12 for s in stats.values()), stats
        bowler = ball.get('bowler')
        if bowler:
            assert bowlers.setdefault(over, bowler) == bowler
            assert over == 0 or bowlers.get(over - 1) != bowler
        if end:
            card = response.get('scorecard_data') or {}
            wickets = card.get('wickets', 0)
            innings.append(dict(innings=number, runs=total, wickets=wickets, legal_balls=legal,
                                **counters, phases=dict(phases)))
            counters = collections.Counter(); phases = collections.defaultdict(collections.Counter); bowlers = {}
        if response.get('match_over') or response.get('super_over_required'):
            return dict(pitch=pitch, seed=seed, night=night, innings=innings,
                        chase_won=match.score > match.first_innings_score,
                        tied=bool(response.get('super_over_required')))
    raise AssertionError('Match did not terminate')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seeds', type=int, default=120)
    parser.add_argument('--output', default='reports/t10_calibration.json')
    args = parser.parse_args()
    match_module.print = lambda *a, **kw: None
    logging.disable(logging.CRITICAL)
    seeds = range(4101, 4101 + args.seeds)
    matches = [simulate(pitch, seed, night) for pitch in PITCHES for night in (False, True) for seed in seeds]
    summary = []
    for pitch in PITCHES:
        for night in (False, True):
            rows = [m for m in matches if m['pitch'] == pitch and m['night'] == night]
            for innings in (1, 2):
                cards = [m['innings'][innings - 1] for m in rows]
                balls = sum(c['legal_balls'] for c in cards)
                phases = collections.defaultdict(collections.Counter)
                for card in cards:
                    for phase, counts in card['phases'].items(): phases[phase].update(counts)
                summary.append(dict(pitch=pitch, night=night, innings=innings,
                    runs=round(statistics.mean(c['runs'] for c in cards), 3),
                    wickets=round(statistics.mean(c['wickets'] for c in cards), 3),
                    dot_pct=round(100 * sum(c.get('dots', 0) for c in cards) / balls, 3),
                    boundaries_per_100=round(100 * sum(c.get('fours', 0) + c.get('sixes', 0) for c in cards) / balls, 3),
                    extras_per_innings=round(statistics.mean(c.get('extras', 0) for c in cards), 3),
                    runs_per_ball=sum(c['runs'] for c in cards) / balls,
                    chase_win_pct=100 * sum(m['chase_won'] for m in rows) / len(rows),
                    tied_matches=sum(m['tied'] for m in rows), phases=dict(phases)))
    day = {r['pitch']: r for r in summary if not r['night'] and r['innings'] == 1}
    t20 = [_simulate_first_innings('T20', 'Hard', seed) for seed in seeds]
    t20_rpb = sum(r['runs'] for r in t20) / sum(r['balls'] for r in t20)
    checks = {pitch: lo <= day[pitch]['runs'] <= hi for pitch,(lo,hi) in BANDS.items()}
    checks['hard_scoring_uplift'] = day['Hard']['runs_per_ball'] >= t20_rpb * 1.10
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(seeds=list(seeds), summary=summary, checks=checks,
                                     t20_hard_runs_per_ball=t20_rpb, match_count=len(matches)), indent=2) + '\n')
    print(json.dumps(dict(first_innings_day=day, checks=checks, report=str(output)), indent=2))
    if not all(checks.values()): raise SystemExit('Calibration gate failed')


if __name__ == '__main__':
    main()
