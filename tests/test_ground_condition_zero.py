"""Accepted zero factors survive storage, merging and rendering in each format."""
import json
import re

import pytest
from engine.ground_config import get_defaults, get_effective_config


@pytest.mark.parametrize('fmt', ['T20', 'ListA', 'FC'])
def test_zero_factors_save_and_reload(authenticated_client, regular_user, fmt):
    config = get_defaults(fmt, mutable=True)
    profile = config['pitch_profiles']['Hard']
    profile['run_factor'] = 0
    config['blending'] = {'pitch_weight': 0, 'skill_weight': 1}
    if fmt == 'T20':
        profile['wicket_factors']['Fast'] = 0
        zero_paths = [
            ('phase_boosts', 'powerplay', 'boundary_multiplier'),
            ('phase_boosts', 'death_overs', 'boundary_boost_batting_pitch'),
            ('phase_boosts', 'death_overs', 'boundary_boost_bowling_pitch'),
            ('phase_boosts', 'death_overs', 'wicket_boost'),
            ('phase_boosts', 'second_innings_death', 'scoring_boost'),
            ('phase_boosts', 'second_innings_death', 'wicket_boost'),
            ('pitch_profiles', 'Hard', 'wicket_factors', 'Fast'),
        ]
    elif fmt == 'ListA':
        zero_paths = [
            ('pitch_profiles', 'Hard', 'wicket_mult'),
            ('pitch_profiles', 'Hard', 'dot_single', 'Dot'),
            ('pitch_profiles', 'Hard', 'dot_single', 'Single'),
        ]
    else:
        zero_paths = [
            ('pitch_profiles', 'Hard', 'wicket_factors_start', 'Fast'),
            ('pitch_profiles', 'Hard', 'wicket_factors_end', 'Fast'),
            ('rough_targeting', 'Hard'),
        ]
    zero_paths += [('pitch_profiles', 'Hard', 'run_factor'), ('blending', 'pitch_weight')]
    for keys in zero_paths:
        target = config
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = 0
    response = authenticated_client.post('/ground-conditions/save', json={**config, 'match_format': fmt})
    assert response.status_code == 200, response.get_json()
    response = authenticated_client.get('/ground-conditions', query_string={'format': fmt})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    rendered = json.loads(re.search(r'<script[^>]*id="gc-config-data"[^>]*>(.*?)</script>', html, re.S).group(1))
    for saved in [rendered, get_effective_config(regular_user.id, fmt)]:
        for keys in zero_paths:
            value = saved
            for key in keys:
                value = value[key]
            assert value == 0, keys
    # The visible run-factor control can represent the accepted value too.
    control_class = {'T20': 'gc-rf-range', 'FC': 'gc-fc-factor-input'}.get(fmt)
    if control_class:
        inputs = re.findall(r'<input[^>]*class="' + control_class + r'"[^>]*>', html)
        assert inputs
        assert all('min="0"' in field for field in inputs)
