"""Stadium identity, access, immutable revisions and simple profile validation."""
from copy import deepcopy
import math
from uuid import uuid4, uuid5, NAMESPACE_URL

from database import db
from database.models import Stadium, StadiumRevision
from engine.format_catalog import SUPPORTED_FORMATS
from engine.ground_config import get_pitch_options

PROFILE_FIELDS = ('name', 'pitch_defaults', 'outfield_speed', 'dimensions_m', 'average_temperature_c')
BOUNDARIES = ('straight_a', 'straight_b', 'square_a', 'square_b')


def neutral_profile(name='Custom stadium', pitch='Hard'):
    return {
        'name': name,
        'pitch_defaults': {fmt: pitch if pitch in [p['key'] for p in get_pitch_options(fmt)] else 'Hard'
                           for fmt in SUPPORTED_FORMATS},
        'outfield_speed': 1.0,
        'dimensions_m': {key: 70.0 for key in BOUNDARIES},
        'average_temperature_c': 22.0,
    }


def number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} must be a finite number between {low} and {high}.')
    return value


def temperature_wear_multiplier(temperature):
    return max(.85, min(1.15, 1 + .01 * (temperature - 22)))



def validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError('Stadium profile must be an object.')
    unknown = set(profile) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError('Unsupported stadium fields: ' + ', '.join(sorted(unknown)) + '.')
    try:
        name = profile['name']
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('Stadium name must contain 1–100 characters.')
        pitches = profile['pitch_defaults']
        if not isinstance(pitches, dict) or set(pitches) != set(SUPPORTED_FORMATS):
            raise ValueError('A default pitch is required for every format.')
        for fmt, pitch in pitches.items():
            if not isinstance(pitch, str) or pitch not in [p['key'] for p in get_pitch_options(fmt)]:
                raise ValueError(f'Invalid default pitch for {fmt}.')
        dimensions = profile['dimensions_m']
        if not isinstance(dimensions, dict) or set(dimensions) != set(BOUNDARIES):
            raise ValueError('Four fixed boundary distances are required.')
        for key in BOUNDARIES:
            number(dimensions[key], 40, 100, key)
        number(profile['outfield_speed'], .9, 1.1, 'Outfield speed')
        number(profile['average_temperature_c'], -20, 55, 'Average temperature')
    except (KeyError, TypeError) as exc:
        raise ValueError('Incomplete or invalid stadium profile.') from exc
    result = deepcopy(profile)
    result['name'] = name.strip()
    return result


def visible_stadiums(user_id, active=True):
    query = Stadium.query.filter(db.or_(Stadium.kind == 'official', Stadium.owner_user_id == user_id))
    if active:
        query = query.filter_by(status='active')
    return query


def accessible_stadium(stadium_id, user_id, active=True):
    row = visible_stadiums(user_id, active).filter_by(id=str(stadium_id)).first()
    if row is None:
        raise ValueError('Choose an accessible, active stadium.')
    return row


def current_profile(stadium):
    from services.stadium_tuning import revision_record
    if stadium.current_profile_version not in (2, 3):
        raise ValueError('Unsupported stadium profile version.')
    row = revision_record(stadium) if stadium.current_profile_version == 3 else db.session.get(StadiumRevision, (stadium.id, stadium.current_revision))
    if row is None:
        raise ValueError('Stadium profile is unavailable.')
    fields = (*PROFILE_FIELDS, 'format_settings') if stadium.current_profile_version == 3 else PROFILE_FIELDS
    return {key: deepcopy(getattr(row, key)) for key in fields}


def stadium_dict(stadium):
    return {'id': stadium.id, 'kind': stadium.kind, 'status': stadium.status,
            'revision': stadium.current_revision, 'schema_version': stadium.current_profile_version, **current_profile(stadium)}


def create_custom(user_id, profile):
    profile = validate_profile(profile)
    ident = str(uuid4())
    stadium = Stadium(id=ident, slug='custom-' + ident, owner_user_id=user_id, kind='custom')
    db.session.add(stadium)
    db.session.flush()
    db.session.add(StadiumRevision(stadium_id=ident, revision=1, **profile))
    return stadium


def team_venue_error(team, user_id):
    if team.venue_mode == 'legacy':
        return None
    try:
        accessible_stadium(team.stadium_id, user_id)
    except ValueError as exc:
        return str(exc)
    return None


def seed_catalogue():
    """Seed reviewed identity data and explicitly labelled estimated profiles."""
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / 'config/stadium_catalogue.json'
    if not path.exists():
        return
    for entry in json.loads(path.read_text()):
        ident = str(uuid5(NAMESPACE_URL, 'simcricketx:stadium:' + entry['slug']))
        if db.session.get(Stadium, ident):
            continue
        profile = validate_profile(entry['profile'])
        db.session.add(Stadium(id=ident, slug=entry['slug'], kind='official'))
        db.session.flush()
        db.session.add(StadiumRevision(stadium_id=ident, revision=1, **profile))
    db.session.commit()


def team_stadium_name(team):
    if team.venue_mode != 'stadium' or not team.stadium_id:
        return team.home_ground
    row = db.session.get(Stadium, team.stadium_id)
    if row is None:
        return team.home_ground
    return current_profile(row)['name']
