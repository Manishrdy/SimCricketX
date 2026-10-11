"""Accepted zero factors survive storage, merging and rendering in each format."""
import pytest
from engine.ground_config import get_defaults, get_effective_config


@pytest.mark.parametrize('fmt', ['T20', 'ListA', 'FC'])
def test_zero_factors_preserved_in_stadium_baseline(app, fmt):
    from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS
    from database import db
    from database.models import UserGroundConfig, StadiumRevisionV3
    from services.stadiums import neutral_profile
    seed(app)
    client = logged_in_client(app, 'user1')
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
    # Historical configurations stay readable even though the editor has retired.
    db.session.add(UserGroundConfig(user_id=ACCOUNTS['user1']['email'], match_format=fmt, config_json=config))
    db.session.commit()
    response = client.post('/api/stadiums', json=neutral_profile())
    assert response.status_code == 201
    baseline = db.session.get(StadiumRevisionV3, (response.json['id'], 1)).baseline_configs[fmt]
    for saved in [baseline, get_effective_config(ACCOUNTS['user1']['email'], fmt)]:
        for keys in zero_paths:
            value = saved
            for key in keys:
                value = value[key]
            assert value == 0, keys
    assert client.post('/ground-conditions/reset', json={'match_format':fmt}).status_code == 410
    assert db.session.get(StadiumRevisionV3, (response.json['id'],1)).baseline_configs[fmt] == baseline
