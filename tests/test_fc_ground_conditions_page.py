"""FC editor retirement and preservation of detailed historical tuning."""
from copy import deepcopy
from database import db
from database.models import UserGroundConfig, StadiumRevisionV3
from scripts.dev_test_accounts import seed, logged_in_client, ACCOUNTS
from services.stadiums import neutral_profile
from engine.ground_config import get_defaults


def test_fc_editor_moves_to_stadiums(app):
    seed(app)
    client = logged_in_client(app, 'user1')
    response = client.get('/ground-conditions?format=FC')
    assert response.status_code == 302 and response.location.endswith('/stadiums')
    assert client.post('/ground-conditions/save', json={'match_format':'FC'}).status_code == 410


def test_fc_advanced_tuning_preserved_without_phase_controls(app):
    seed(app)
    client = logged_in_client(app, 'user1')
    config = get_defaults('FC', mutable=True)
    config['ball_condition']['pink_ball']['swing_overs'] = 17
    config['ball_condition']['pink_ball']['under_lights_wicket_factors']['Fast'] = 1.19
    db.session.add(UserGroundConfig(user_id=ACCOUNTS['user1']['email'], match_format='FC', config_json=config))
    db.session.commit()
    response = client.post('/api/stadiums', json=neutral_profile())
    assert response.status_code == 201
    revision = db.session.get(StadiumRevisionV3, (response.json['id'],1))
    assert revision.baseline_configs['FC'] == config
    assert 'phases' not in response.json['format_settings']['FC']
    assert revision.baseline_configs['T20'] == get_defaults('T20')
