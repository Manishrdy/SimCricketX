"""Simple immutable venue conditions. No database reads during simulation."""
from copy import deepcopy
import random

MODEL_VERSION = 'stadium_v2'
SUPPORTED_MODELS = ('stadium_v1', MODEL_VERSION, 'stadium_v3')


def clamp(value, low, high):
    return max(low, min(high, value))


def resolve_conditions(profile, stadium_id, revision, fmt, ground_config,
                       is_day_night, overrides, seed, forecast='clear', baseline_configs=None, baseline_provenance=None):
    from services.stadiums import validate_profile, temperature_wear_multiplier
    from engine.weather import FORECAST_TIERS, generate_weather_script
    from engine import fc_weather
    settings = deepcopy(profile.get('format_settings'))
    profile = validate_profile({key: value for key, value in profile.items() if key != 'format_settings'})
    if settings is not None:
        from services.stadium_tuning import validate_settings
        from engine.stadium_tuning import resolved_config
        if baseline_configs is None:
            raise ValueError('Missing stadium baseline configurations.')
        validate_settings(settings, baseline_configs)
        ground_config = resolved_config(baseline_configs[fmt.name], settings[fmt.name])
    if not isinstance(overrides, dict) or set(overrides) - {'pitch'}:
        raise ValueError('Only a pitch override is supported for stadium conditions.')
    pitch = overrides.get('pitch') or profile['pitch_defaults'][fmt.name]
    if not isinstance(pitch, str) or pitch not in ground_config.get('pitch_profiles', {}):
        raise ValueError('Pitch is not available for this format.')
    if forecast not in FORECAST_TIERS:
        raise ValueError('Invalid weather forecast.')
    radius = sum(profile['dimensions_m'].values()) / 4
    scheduled_balls = fmt.overs * fmt.balls_per_over if fmt.name != 'FC' else None
    wear = temperature_wear_multiplier(profile['average_temperature_c'])
    snapshot = {
        'schema_version': 2, 'conditions_model_version': MODEL_VERSION,
        'stadium_id': stadium_id, 'revision': revision, 'name': profile['name'],
        'format': fmt.name, 'is_day_night': bool(is_day_night),
        'conditions_seed': seed, 'pitch': pitch, 'geometry_m': deepcopy(profile['dimensions_m']),
        'outfield_speed': profile['outfield_speed'],
        'average_temperature_c': profile['average_temperature_c'],
        'temperature_wear_multiplier': wear,
        'coefficients': {
            'Six': clamp((70 / radius) ** 2, .85, 1.15),
            'Four': clamp(70 / radius * profile['outfield_speed'], .9, 1.1),
            'Double': clamp(radius / 70, .95, 1.1),
            'Three': clamp((radius / 70) ** 2, .9, 1.15),
        },
        'scheduled_innings_balls': scheduled_balls,
        'wear_reference_balls': max(240, 2 * scheduled_balls) if scheduled_balls else None,
        'public_forecast': {'wear_rate_multiplier': wear},
        'overrides': deepcopy(overrides), 'forecast': forecast,
        'ground_config': deepcopy(ground_config),
    }
    if settings is not None:
        snapshot.update(schema_version=3, conditions_model_version='stadium_v3',
            format_settings=settings[fmt.name], baseline_provenance=deepcopy(baseline_provenance))
    rng = random.Random(seed)
    if fmt.name == 'FC':
        snapshot['fc_weather_script'] = fc_weather.generate_weather_script(
            forecast, fmt.days, fmt.overs_per_day, min_overs_last_hour=fmt.min_overs_last_hour,
            rng=rng, is_day_night=bool(is_day_night))
    else:
        snapshot['weather_script'] = generate_weather_script(forecast, fmt.overs, fmt.name, rng=rng)
    return snapshot


def apply_venue_weights(weights, snapshot, bowling_type, innings=1, legal_balls=0, super_over=False):
    if not snapshot:
        return weights
    version = snapshot.get('conditions_model_version')
    if version == 'stadium_v1':
        from engine.stadium_conditions_v1 import apply_venue_weights_v1
        return apply_venue_weights_v1(weights, snapshot, bowling_type, innings, legal_balls, super_over)
    if version not in (MODEL_VERSION, 'stadium_v3'):
        raise ValueError('Unsupported stadium conditions version.')
    result = dict(weights)
    for outcome, multiplier in snapshot['coefficients'].items():
        if outcome in result:
            result[outcome] *= multiplier
    return result


def uses_v1_dew(ground_config):
    return (ground_config or {}).get('_stadium', {}).get('conditions_model_version') == 'stadium_v1'


def public_match_data(data):
    """Browser-safe match setup: never expose actual weather or RNG state."""
    hidden = {'ground_config', 'weather_script', 'fc_weather_script', 'conditions_snapshot',
              'hundred_snapshot', 'fc_snapshot', 'super_over_snapshot', 'scenario_pack'}
    result = {key: value for key, value in data.items() if key not in hidden}
    snapshot = data.get('conditions_snapshot')
    if snapshot:
        common = ('schema_version', 'conditions_model_version', 'stadium_id', 'revision',
                  'name', 'format', 'is_day_night', 'pitch', 'geometry_m', 'public_forecast')
        extra = ('average_temperature_c', 'outfield_speed', 'temperature_wear_multiplier') if snapshot.get('conditions_model_version') in (MODEL_VERSION, 'stadium_v3') else ('city', 'country_code', 'match_date', 'environment', 'provenance')
        result['conditions_snapshot'] = {key: deepcopy(snapshot[key]) for key in (*common, *extra) if key in snapshot}
    return result
