"""Compare scheduled 40/50-over List A with the existing calibration squads.

Run: python scripts/bench_lista_lengths.py --out reports/lista_lengths.json
Fixed seeds, clear weather, day and day/night, both innings, all five pitches.
This is a model baseline, not a claim of historical 40-over scoring averages.
"""
import argparse
import collections
import json
import logging
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.match import Match
from tests.test_scoring_calibration import _match_data, _overs_to_balls, SEEDS, PITCHES


def simulate(length, pitch, seed, day_night):
    data = _match_data('ListA', pitch, seed)
    data.update(scheduled_overs=length, is_day_night=day_night,
                weather_script={'forecast': 'clear', 'events': []})
    match = Match(data)
    innings = []
    phases = collections.defaultdict(lambda: {'runs': 0, 'legal_balls': 0})
    for _ in range(1500):
        number = match.innings
        phase = match.fmt.get_phase(match.current_over).name
        before_score = match.score
        before_balls = match.current_over * 6 + match.current_ball
        response = match.next_ball()
        if response.get('error'):
            raise AssertionError(response)
        if response.get('innings_end') or response.get('match_over') or response.get('super_over_required'):
            card = response['scorecard_data']
            stats = match.first_innings_bowling_stats if number == 1 else match.bowler_stats
            legal = _overs_to_balls(card['overs'])
            total = match.first_innings_score if number == 1 else match.score
            phases[phase]['runs'] += total - before_score
            phases[phase]['legal_balls'] += legal - before_balls
            usage = {name: st.get('balls_bowled', 0) for name, st in stats.items()}
            if max(usage.values(), default=0) > length // 5 * 6:
                raise AssertionError(f'Bowling quota exceeded: {length}/{pitch}/{seed}: {usage}')
            innings.append({'innings': number, 'runs': total, 'wickets': card.get('wickets', 0),
                            'legal_balls': legal,
                            'fours': sum(int(p.get('fours') or 0) for p in card.get('players', []) if str(p.get('fours') or 0).isdigit()),
                            'sixes': sum(int(p.get('sixes') or 0) for p in card.get('players', []) if str(p.get('sixes') or 0).isdigit()),
                            'phases': dict(phases), 'bowler_legal_balls': usage})
            phases = collections.defaultdict(lambda: {'runs': 0, 'legal_balls': 0})
        else:
            phases[phase]['runs'] += match.score - before_score
            phases[phase]['legal_balls'] += match.current_over * 6 + match.current_ball - before_balls
        if response.get('match_over') or response.get('super_over_required'):
            return {'length': length, 'pitch': pitch, 'seed': seed, 'day_night': day_night, 'innings': innings}
    raise AssertionError('Match failed to terminate')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default='reports/lista_lengths.json')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    rows = [simulate(length, pitch, seed, dn) for length in (40,50)
            for pitch in PITCHES for dn in (False,True) for seed in SEEDS]
    summary = []
    for length in (40,50):
        for pitch in PITCHES:
            for dn in (False,True):
                selected = [r for r in rows if (r['length'],r['pitch'],r['day_night']) == (length,pitch,dn)]
                for number in (1,2):
                    innings = [i for r in selected for i in r['innings'] if i['innings'] == number]
                    summary.append(dict(length=length,pitch=pitch,day_night=dn,innings=number,
                        **{key:round(statistics.mean(i[key] for i in innings),2)
                           for key in ('runs','wickets','legal_balls','fours','sixes')}))
    output = Path(args.out); output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({'seeds':SEEDS,'summary':summary,'matches':rows},indent=2)+'\n')
    for row in summary:
        if row['innings'] == 1 and not row['day_night']:
            print(f"{row['length']} {row['pitch']:5}: {row['runs']:6.1f} runs, {row['wickets']:.1f} wickets, {row['legal_balls']:.1f} balls")
    print(f'{len(rows)} matches written to {output}')

if __name__ == '__main__':
    main()
