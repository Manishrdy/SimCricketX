"""Reproducible synthetic calibration; no database, network, or user accounts.

Run: .venv/bin/python scripts/benchmark_match_director.py --seeds 8
Reports ordinary, legacy scripted, and adaptive matches. Small batches are
smoke checks, not evidence of statistically significant balance improvements.
"""
import argparse
from collections import defaultdict
import contextlib
import io
import json
import logging
from pathlib import Path
import random
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.match import Match


def players(prefix, strength):
    return [dict(name=f'{prefix}{i}', role='Batsman' if i < 6 else 'Bowler',
                 batting_rating=(76-i*2 if i < 6 else 44-(i-6)*3)+strength,
                 bowling_rating=(40 if i < 6 else 75-(i-6)*2)+strength,
                 fielding_rating=70+strength, batting_hand='Right', bowling_hand='Right',
                 bowling_type=['Fast','Medium','Off spin','Leg spin','Fast'][i % 5],
                 will_bowl=i >= 6, is_captain=i == 0) for i in range(11)]


def simulate(fmt, pitch, seed, mode, strength, bat_first):
    random.seed(seed)
    data = dict(match_id='director_benchmark', created_by='benchmark',
                team_home='HOM_benchmark', team_away='AWY_benchmark',
                stadium='Synthetic', pitch=pitch, toss='Heads', toss_winner='HOM',
                toss_decision='Bat' if bat_first else 'Bowl', simulation_mode='auto',
                match_format=fmt, playing_xi={'home':players('H',strength),
                                             'away':players('A',0)},
                substitutes={'home':[], 'away':[]}, rain_probability=0,
                scenario_mode=mode, is_day_night=False)
    with contextlib.redirect_stdout(io.StringIO()):
        m = Match(data)
        adjustments = 0
        balls = 0
        dots = boundaries = wickets = 0
        for _ in range(1600):
            if mode == 'adaptive_v1' and m.scenario_engine.get_scenario_bias({}):
                adjustments += 1
            result = m.next_ball()
            if result.get('error'):
                raise RuntimeError(result)
            delivery = result.get('ball_data')
            if delivery:
                runs = delivery.get('runs', 0)
                extra = delivery.get('is_extra', False)
                out = delivery.get('batter_out', False)
                dots += runs == 0 and not extra and not out
                boundaries += runs in (4, 6) and not extra and not out
                wickets += bool(out)
                balls += 1
            if result.get('match_over') or result.get('super_over_required'):
                break
        else:
            raise RuntimeError('Match did not terminate')
    tie = bool(result.get('super_over_required'))
    margin = getattr(m,'margin_value',0) or 0
    kind = getattr(m,'margin_type',None)
    left = (m.fmt.overs-m.current_over)*m.fmt.balls_per_over-m.current_ball
    close = tie or (kind == 'runs' and margin <= 10) or (kind == 'wickets' and (margin <= 2 or left <= 6))
    return dict(first_score=m.first_innings_score, tie=tie, close=close,
                home_win=bool(m.winner_is_home) if not tie else False,
                adjustments=adjustments, dots=dots, boundaries=boundaries,
                wickets=wickets, deliveries=balls)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds',type=int,default=8)
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error('--seeds must be positive')
    logging.disable(logging.CRITICAL)
    groups = defaultdict(list)
    for fmt in ('T10','T20','ListA','Hundred'):
        for pitch in ('Green','Flat'):
            for strength in (0,12):
                for bat_first in (True,False):
                    for seed in range(args.seeds):
                        for mode in (None,'last_ball_six','adaptive_v1'):
                            row = simulate(fmt,pitch,9000+seed,mode,strength,bat_first)
                            groups[(fmt,mode or 'ordinary',strength,bat_first)].append(row)
    output=[]
    for (fmt,mode,strength,bat_first),rows in groups.items():
        output.append(dict(format=fmt, mode=mode, home_strength_bonus=strength,
                           home_bats_first=bat_first, matches=len(rows),
                           **{key:round(statistics.mean(r[key] for r in rows),3)
                              for key in rows[0]}))
    print(json.dumps(output,indent=2))


if __name__ == '__main__':
    main()
