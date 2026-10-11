"""Reproducible captaincy calibration with existing reference squads.

Run: .venv/bin/python scripts/bench_captaincy.py --seeds 16 --output /tmp/captaincy.json
Use --full-matches to include chases and First-Class match outcomes. This
script never creates users, opens a database, or writes match archives.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('SIMCRICKETX_TEST_MODE', '1')
os.environ.setdefault('SIMCRICKETX_SKIP_GLOBAL_APP', '1')
os.environ['GITHUB_ISSUE_ON_EXCEPTION_ENABLED'] = 'false'
from engine.captaincy import default_config, PRESETS, ATTRIBUTES, batting_scores, choose_action
from engine.match import Match
from tests.test_scoring_calibration import _match_data, TARGET_BANDS


def simulate(fmt, pitch, seed, config, full=False):
    data = _match_data(fmt, pitch, seed)
    if fmt == 'FC':
        from scripts.bench_fc import _squad
        data['playing_xi'] = {'home': _squad('H'), 'away': _squad('A')}
    data.update(captaincy_model_version=1,
        captaincy={side: {'config': copy.deepcopy(config)} for side in ('home', 'away')})
    match = Match(data)
    match._create_match_archive = lambda: True
    first = None
    started = time.perf_counter()
    evaluations = []
    evaluate = match._captain_evaluate
    def timed(*args, **kwargs):
        start = time.perf_counter()
        result = evaluate(*args, **kwargs)
        evaluations.append(time.perf_counter() - start)
        return result
    match._captain_evaluate = timed
    for _ in range(60000 if fmt == 'FC' else 5000):
        if match.innings >= 4:
            phase = getattr(match, 'super_over_phase', None)
            if phase == 'awaiting_innings1_selection':
                response = match.start_super_over('home')
            elif phase == 'awaiting_innings2_selection':
                response = match.start_super_over_innings2()
            else:
                response = match.next_super_over_ball()
        else:
            response = match.next_ball()
        if response.get('error'):
            raise AssertionError(f'{fmt}/{pitch}/{seed}: {response["error"]}')
        if response.get('innings_end') and first is None:
            sc = response.get('scorecard_data', {})
            first = {'runs': sc.get('total_score', match.first_innings_score or 0), 'wickets': sc.get('wickets', 0)}
            if fmt == 'FC' and match.fc_innings_totals:
                total = match.fc_innings_totals[1]
                first = {'runs': total.get('score', total.get('total', sc.get('total_score', 0))), 'wickets': total.get('wickets', sc.get('wickets', 0))}
            if not full:
                break
        if response.get('match_over'):
            break
    else:
        raise AssertionError(f'{fmt}/{pitch}/{seed}: match did not finish')
    if first is None:
        first = {'runs': match.score, 'wickets': match.wickets}
    return {**first, 'winner': match.winner_is_home, 'status': match.match_status,
        'events': len(match.captaincy_events), 'seconds': time.perf_counter() - started,
        'evaluation_p95_ms': sorted(evaluations)[int(.95 * (len(evaluations)-1))] * 1000 if evaluations else 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=16)
    parser.add_argument('--formats', nargs='+', default=['T10', 'T20', 'ListA', 'FC', 'Hundred'])
    parser.add_argument('--pitches', nargs='+', default=['Green', 'Dry', 'Hard', 'Flat', 'Dead'])
    parser.add_argument('--profiles', nargs='+', default=['neutral', 'attacking', 'conservative', 'weak', 'strong'])
    parser.add_argument('--full-matches', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    configs = {'neutral': default_config(), 'attacking': copy.deepcopy(PRESETS['Attacking']),
               'conservative': copy.deepcopy(PRESETS['Conservative'])}
    for name, quality in [('weak', 0), ('strong', 100)]:
        configs[name] = default_config()
        for key in ATTRIBUTES[:5]:
            configs[name][key] = quality
    report = []
    for fmt in args.formats:
        for pitch in args.pitches:
            for name in args.profiles:
                rows = [simulate(fmt, pitch, seed, configs[name], args.full_matches) for seed in range(args.seeds)]
                row = {'format': fmt, 'pitch': pitch, 'profile': name, 'seeds': args.seeds,
                    'mean_runs': round(statistics.mean(r['runs'] for r in rows), 2),
                    'mean_wickets': round(statistics.mean(r['wickets'] for r in rows), 2),
                    'mean_events': round(statistics.mean(r['events'] for r in rows), 2),
                    'max_evaluation_p95_ms': round(max(r['evaluation_p95_ms'] for r in rows), 3),
                    'statuses': [r['status'] for r in rows], 'home_wins': sum(r['winner'] is True for r in rows)}
                if fmt in TARGET_BANDS and name == 'neutral':
                    low, high, wlow, whigh = TARGET_BANDS[fmt][pitch]
                    row['in_target_band'] = low <= row['mean_runs'] and (high is None or row['mean_runs'] <= high) and wlow <= row['mean_wickets'] <= whigh
                report.append(row)
                print(json.dumps(row), flush=True)
                if args.output:
                    args.output.write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    main()
