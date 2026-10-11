"""Frozen stadium_v1 modifiers for existing saved matches only."""

def clamp(value, low, high):
    return max(low, min(high, value))


def apply_venue_weights_v1(weights, snapshot, bowling_type, innings=1, legal_balls=0, super_over=False):
    if not snapshot:
        return weights
    if snapshot.get('conditions_model_version') != 'stadium_v1':
        raise ValueError('Unsupported stadium conditions version.')
    result = dict(weights)
    modifiers = dict(snapshot['coefficients'])
    bowling = str(bowling_type).lower()
    surface = snapshot['surface']
    modifiers['Wicket'] = surface['spin_residual'] if any(k in bowling for k in ('spin', 'break', 'orthodox', 'chinaman')) else surface['seam_residual']
    dew = snapshot['dew']
    # Carry late-match dew into tie breakers without starting another timeline.
    progress = ((innings - 1) * (snapshot['scheduled_innings_balls'] or 0) + legal_balls)
    if super_over:
        progress = 2 * (snapshot['scheduled_innings_balls'] or 0)
    intensity = dew['intensity'] * clamp((progress - dew['start_ball']) / max(1, dew['peak_ball'] - dew['start_ball']), 0, 1)
    modifiers['Wicket'] *= 1 - .075 * intensity
    modifiers['Four'] *= 1 + .05 * intensity
    modifiers['Extras'] = 1 + .2 * intensity
    for outcome, modifier in modifiers.items():
        if outcome in result:
            result[outcome] *= clamp(modifier, .8, 1.2)
    return result


