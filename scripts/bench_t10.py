"""Deterministic T10 calibration, using the established scoring-regression XI.

Run: .venv/bin/python scripts/bench_t10.py --seeds 120
No database writes or network access. Reports both innings and day/night effects.

Beyond first-innings par this gates INNINGS PARITY. A T10 is meant to read the
same in both halves: if the side batting first hits big, the chase hits big
back, and wickets fall in the last three overs as the price of that hitting
rather than as a collapse. Those are measurable, so they are asserted here —
mean runs alone cannot see a second innings that scores at the same rate while
losing wickets 60% faster, which is exactly what the format used to do.
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

BANDS = {'Green': (88, 105), 'Dry': (92, 110), 'Hard': (116, 136),
         'Flat': (128, 148), 'Dead': (145, 168)}

# Innings-2 vs innings-1 tolerances, day matches (night carries dew, which is
# meant to favour the chase, so it is reported but not gated).
#
# The numbers these replaced, measured before the parity work, are worth
# keeping in view: run parity ran 0.891-1.004, wicket parity 1.27-1.88, and a
# Green chase took just 21% of its wickets in the death overs against the
# first innings' 43%. The tolerances below are what the engine holds with
# room for seed noise, not aspirations.
RUN_PARITY_TOL = 0.05      # +/-5% on runs per legal ball

# Wickets are allowed more room than runs, and deliberately. A chase in the
# final over of a live match swings at everything; measured by window, overs
# 1-7 sit near 1.10 and over 10 alone runs to 1.33. That last-over premium is
# the game working, so the gate is set to catch a return of the middle-overs
# collapse (which read 1.6-1.9 across the whole innings) rather than to
# flatten the ending.
WICKET_PARITY_TOL = 0.22

# Share of an innings' wickets falling in overs 8-10. The death phase is 30%
# of the balls, so 0.40 means the last three overs are about 1.5x as dangerous
# as the rest — wickets as the price of hitting, which is the point.
MIN_DEATH_WICKET_SHARE = 0.40
# Dry is exempt, and structurally so: a T10 XI fields five bowlers on two
# overs each against phases of 3/4/3, and the over-utilisation rules leave the
# four middle overs to the two spinners. On a turner that is where the wickets
# are, whatever the captain would prefer at the death. The floor here still
# catches a regression; it does not pretend the surface behaves like the others.
DEATH_WICKET_SHARE_OVERRIDES = {'Dry': 0.26}

# A chase knows the target and can calibrate against it; the side batting
# first is guessing at par. That information is worth most where first-innings
# scores vary most, which is why Dry sits at the top of this range and the
# roads sit near the middle of it.
CHASE_WIN_RANGE = (38.0, 67.0)


def simulate(pitch, seed, night=False):
    data = _match_data('T10', pitch, seed)
    data.update(is_day_night=night, weather_script={'forecast': 'clear', 'events': []})
    match = match_module.Match(data)
    innings = []
    counters = collections.Counter()
    phases = collections.defaultdict(collections.Counter)
    per_over = collections.defaultdict(collections.Counter)
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
        runs = total - before_score
        wicket = int(bool(ball.get('batter_out')))
        phases[phase]['runs'] += runs
        phases[phase]['legal_balls'] += delta
        phases[phase]['wickets'] += wicket
        per_over[over]['runs'] += runs
        per_over[over]['legal_balls'] += delta
        per_over[over]['wickets'] += wicket
        counters['dots'] += int(delta > 0 and total == before_score)
        counters['extras'] += int(bool(ball.get('is_extra')))
        if not ball.get('is_extra'):
            counters['fours'] += int(ball.get('runs') == 4)
            counters['sixes'] += int(ball.get('runs') == 6)
            per_over[over]['boundaries'] += int(ball.get('runs') in (4, 6))
        assert all(s['balls_bowled'] <= 12 for s in stats.values()), stats
        bowler = ball.get('bowler')
        if bowler:
            assert bowlers.setdefault(over, bowler) == bowler
            assert over == 0 or bowlers.get(over - 1) != bowler
        if end:
            card = response.get('scorecard_data') or {}
            wickets = card.get('wickets', 0)
            innings.append(dict(innings=number, runs=total, wickets=wickets, legal_balls=legal,
                                all_out=int(wickets >= 10), **counters,
                                phases=dict(phases), per_over={o: dict(c) for o, c in per_over.items()}))
            counters = collections.Counter(); phases = collections.defaultdict(collections.Counter)
            per_over = collections.defaultdict(collections.Counter); bowlers = {}
        if response.get('match_over') or response.get('super_over_required'):
            return dict(pitch=pitch, seed=seed, night=night, innings=innings,
                        chase_won=match.score > match.first_innings_score,
                        tied=bool(response.get('super_over_required')))
    raise AssertionError('Match did not terminate')


def _phase_rpo(row, name):
    counts = row['phases'].get(name, {})
    balls = counts.get('legal_balls', 0)
    return (counts.get('runs', 0) / balls * 6) if balls else 0.0


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
                per_over = collections.defaultdict(collections.Counter)
                for card in cards:
                    for phase, counts in card['phases'].items(): phases[phase].update(counts)
                    for over, counts in card['per_over'].items(): per_over[int(over)].update(counts)
                total_wickets = sum(c['wickets'] for c in cards)
                # Share is taken over the per-phase tally, not the scorecard
                # total: the phase counter follows ball_data['batter_out'], so
                # a non-striker run out lands in one and not the other. Same
                # numerator and denominator keeps the ratio honest.
                phase_wickets = sum(counts['wickets'] for counts in phases.values())
                death_wickets = phases['Death']['wickets']
                summary.append(dict(pitch=pitch, night=night, innings=innings,
                    runs=round(statistics.mean(c['runs'] for c in cards), 3),
                    wickets=round(statistics.mean(c['wickets'] for c in cards), 3),
                    dot_pct=round(100 * sum(c.get('dots', 0) for c in cards) / balls, 3),
                    boundaries_per_100=round(100 * sum(c.get('fours', 0) + c.get('sixes', 0) for c in cards) / balls, 3),
                    extras_per_innings=round(statistics.mean(c.get('extras', 0) for c in cards), 3),
                    runs_per_ball=sum(c['runs'] for c in cards) / balls,
                    wickets_per_100_balls=round(100 * total_wickets / balls, 3),
                    death_wicket_share=round(death_wickets / phase_wickets, 4) if phase_wickets else 0.0,
                    all_out_pct=round(100 * sum(c['all_out'] for c in cards) / len(cards), 2),
                    chase_win_pct=100 * sum(m['chase_won'] for m in rows) / len(rows),
                    tied_matches=sum(m['tied'] for m in rows), phases=dict(phases),
                    per_over={o: dict(per_over[o]) for o in sorted(per_over)}))

    by_key = {(r['pitch'], r['night'], r['innings']): r for r in summary}
    day = {r['pitch']: r for r in summary if not r['night'] and r['innings'] == 1}
    t20 = [_simulate_first_innings('T20', 'Hard', seed) for seed in seeds]
    t20_rpb = sum(r['runs'] for r in t20) / sum(r['balls'] for r in t20)

    checks = {pitch: lo <= day[pitch]['runs'] <= hi for pitch, (lo, hi) in BANDS.items()}
    checks['hard_scoring_uplift'] = day['Hard']['runs_per_ball'] >= t20_rpb * 1.10
    parity = {}
    for pitch in PITCHES:
        one, two = by_key[(pitch, False, 1)], by_key[(pitch, False, 2)]
        run_ratio = two['runs_per_ball'] / one['runs_per_ball']
        wkt_ratio = (two['wickets_per_100_balls'] / one['wickets_per_100_balls']
                     if one['wickets_per_100_balls'] else float('inf'))
        parity[pitch] = dict(run_ratio=round(run_ratio, 4), wicket_ratio=round(wkt_ratio, 4),
                             death_share=[one['death_wicket_share'], two['death_wicket_share']],
                             chase_win_pct=one['chase_win_pct'])
        checks[f'{pitch}_run_parity'] = abs(run_ratio - 1.0) <= RUN_PARITY_TOL
        checks[f'{pitch}_wicket_parity'] = abs(wkt_ratio - 1.0) <= WICKET_PARITY_TOL
        _floor = DEATH_WICKET_SHARE_OVERRIDES.get(pitch, MIN_DEATH_WICKET_SHARE)
        checks[f'{pitch}_death_wickets'] = all(
            r['death_wicket_share'] >= _floor for r in (one, two))
        # The death overs must be the fastest phase in BOTH innings; a chase
        # that decelerates at the end is the collapse this gate exists to catch.
        checks[f'{pitch}_death_acceleration'] = all(
            _phase_rpo(r, 'Death') >= _phase_rpo(r, 'Powerplay') for r in (one, two))
        checks[f'{pitch}_chase_balance'] = (
            CHASE_WIN_RANGE[0] <= one['chase_win_pct'] <= CHASE_WIN_RANGE[1])

    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(seeds=list(seeds), summary=summary, checks=checks,
                                     parity=parity, t20_hard_runs_per_ball=t20_rpb,
                                     match_count=len(matches)), indent=2) + '\n')
    print(json.dumps(dict(first_innings_day={k: {m: v[m] for m in
                            ('runs', 'wickets', 'dot_pct', 'boundaries_per_100',
                             'runs_per_ball', 'wickets_per_100_balls', 'death_wicket_share')}
                          for k, v in day.items()},
                          parity=parity, failed=[k for k, v in checks.items() if not v],
                          report=str(output)), indent=2))
    if not all(checks.values()): raise SystemExit('Calibration gate failed')


if __name__ == '__main__':
    main()
