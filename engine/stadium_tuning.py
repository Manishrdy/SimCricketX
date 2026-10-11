"""Snapshot-only v3 pitch adjustments and integer legal-ball phases."""
from copy import deepcopy
import math

RUNS = ('Single', 'Double', 'Three', 'Four', 'Six')


def phase_ranges(fmt):
    if fmt.name == 'FC':
        return {}
    total = fmt.overs * fmt.balls_per_over
    if fmt.name == 'Hundred':
        pp_end = fmt.powerplay_balls
        death_start = max(pp_end, total - 20)
    else:
        pp_end = math.floor((fmt.powerplay_phases[-1].end + 1) * fmt.balls_per_over)
        death_start = math.floor(fmt.death_phase.start * fmt.balls_per_over)
    pp_end = min(total, pp_end)
    death_start = max(pp_end, min(total, death_start))
    return {'powerplay': (0, pp_end), 'middle': (pp_end, death_start), 'death': (death_start, total)}


def legal_ball_phase(fmt, completed):
    if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
        raise ValueError('Legal balls completed must be a nonnegative integer.')
    ranges = phase_ranges(fmt)
    for phase, (start, end) in ranges.items():
        if start <= completed < end:
            return phase
    if fmt.name == 'FC':
        return None
    raise ValueError('Delivery is outside the effective innings budget.')


def resolved_config(baseline, settings):
    result = deepcopy(baseline)
    result.pop('_stadium', None)
    result['blending'] = deepcopy(settings['blending'])
    return result


def apply_adjustments(weights, snapshot, bowling_type, fmt=None, completed=None, innings=1, tie_breaker=False):
    if snapshot.get('conditions_model_version') != 'stadium_v3':
        return weights
    settings = snapshot['format_settings']
    pitch = settings['pitches'][snapshot['pitch']]
    result = dict(weights)
    for outcome in RUNS:
        if outcome in result:
            result[outcome] *= pitch['scoring']
    if 'Wicket' in result:
        # Reuse the delivery engine's classification, including its default styles.
        from engine.ball_outcome import _PACE_TYPES, _SPIN_TYPES
        style = 'pace' if bowling_type in _PACE_TYPES else 'spin' if bowling_type in _SPIN_TYPES else None
        result['Wicket'] *= pitch['wicket'] * (pitch[style] if style else 1)
    if not tie_breaker and snapshot['format'] != 'FC':
        phase = settings['phases'][legal_ball_phase(fmt, completed)]
        chase = phase['chasing'] if innings == 2 else {'scoring': 1, 'wicket': 1}
        for outcome in RUNS:
            if outcome in result:
                result[outcome] *= phase['scoring'] * chase['scoring']
        for outcome in ('Four', 'Six'):
            if outcome in result:
                result[outcome] *= phase['boundary']
        if 'Wicket' in result:
            result['Wicket'] *= phase['wicket'] * chase['wicket']
    return result
