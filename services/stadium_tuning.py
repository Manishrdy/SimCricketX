"""Private stadium baselines, validated simple adjustments, and adoption."""
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from database import db
from database.models import Stadium, StadiumRevisionV3, StadiumAdoption, UserGroundConfig
from engine.format_catalog import SUPPORTED_FORMATS
from engine.ground_config import get_defaults, _deep_merge, _normalise_stored

PITCH_FACTORS = ('scoring', 'wicket', 'pace', 'spin')
PHASE_FACTORS = ('scoring', 'boundary', 'wicket')
PHASES = ('powerplay', 'middle', 'death')


def capture_baselines(user_id):
    # Adoption must fail on storage errors, rather than silently capture factory tuning.
    stored = {row.match_format: row for row in UserGroundConfig.query.filter_by(user_id=user_id).all()}
    configs, provenance = {}, {}
    captured_at = datetime.utcnow().isoformat() + 'Z'
    for fmt in SUPPORTED_FORMATS:
        defaults = get_defaults(fmt, mutable=True)
        row = stored.get(fmt)
        configs[fmt] = _deep_merge(defaults, _normalise_stored(row.config_json, fmt)) if row else defaults
        configs[fmt].pop('_stadium', None)
        provenance[fmt] = {'source': 'user' if row and row.config_json else 'factory',
                           'capture_version': 3, 'captured_at': captured_at}
    return configs, provenance


def default_settings(configs):
    return {fmt: {
        'blending': deepcopy(configs[fmt]['blending']),
        'pitches': {pitch: dict.fromkeys(PITCH_FACTORS, 1.0)
                    for pitch in configs[fmt]['pitch_profiles']},
        **({'phases': {phase: {**dict.fromkeys(PHASE_FACTORS, 1.0),
                    'chasing': {'scoring': 1.0, 'wicket': 1.0}} for phase in PHASES}}
           if fmt != 'FC' else {}),
    } for fmt in SUPPORTED_FORMATS}


def validate_settings(settings, configs):
    from services.stadiums import number
    def keys(value, expected, label):
        if not isinstance(value, dict) or set(value) != set(expected):
            raise ValueError(f'Invalid {label} fields.')
    def factors(value, expected, label):
        keys(value, expected, label)
        for key in expected:
            number(value[key], 0, 3, f'{label} {key}')
    keys(settings, SUPPORTED_FORMATS, 'format settings')
    for fmt in SUPPORTED_FORMATS:
        block = settings[fmt]
        keys(block, ('blending', 'pitches') if fmt == 'FC' else ('blending', 'pitches', 'phases'), fmt)
        keys(block['blending'], ('pitch_weight', 'skill_weight'), 'blending')
        for key, value in block['blending'].items():
            number(value, 0, 1, key)
        if abs(sum(block['blending'].values()) - 1) > 1e-8:
            raise ValueError('Pitch and skill weights must sum to 1.')
        keys(block['pitches'], configs[fmt]['pitch_profiles'], f'{fmt} pitches')
        for pitch, adjustments in block['pitches'].items():
            factors(adjustments, PITCH_FACTORS, f'{fmt} {pitch}')
        if fmt != 'FC':
            keys(block['phases'], PHASES, f'{fmt} phases')
            for phase, adjustments in block['phases'].items():
                keys(adjustments, (*PHASE_FACTORS, 'chasing'), phase)
                factors({key: adjustments[key] for key in PHASE_FACTORS}, PHASE_FACTORS, phase)
                factors(adjustments['chasing'], ('scoring', 'wicket'), 'chasing')
    return deepcopy(settings)


def revision_record(stadium):
    return db.session.get(StadiumRevisionV3, (stadium.id, stadium.current_revision))


def require_enabled():
    from flask import current_app
    if not current_app.config.get('STADIUM_V3_ENABLED', True):
        raise ValueError('New stadium tuning is temporarily disabled.')


def create_personal(user_id, profile, configs=None, provenance=None):
    from services.stadiums import validate_profile
    require_enabled()
    if not isinstance(profile, dict):
        raise ValueError('Stadium profile must be an object.')
    simple = validate_profile({key: value for key, value in profile.items() if key != 'format_settings'})
    if configs is None:
        configs, provenance = capture_baselines(user_id)
    settings = validate_settings(profile.get('format_settings', default_settings(configs)), configs)
    ident = str(uuid4())
    row = Stadium(id=ident, slug='custom-' + ident, kind='custom', owner_user_id=user_id,
                  current_profile_version=3)
    db.session.add(row)
    db.session.flush()
    db.session.add(StadiumRevisionV3(stadium_id=ident, revision=1, **simple,
        format_settings=settings, baseline_configs=deepcopy(configs), baseline_provenance=deepcopy(provenance)))
    db.session.flush()
    return row


def adopt(stadium, user_id, expected):
    from services.stadiums import current_profile
    require_enabled()
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise ValueError('A valid source revision is required.')
    # Look up the receipt before checking current revision, so retries survive later edits.
    receipt = db.session.get(StadiumAdoption, (user_id, stadium.id, expected))
    if receipt:
        result = db.session.get(Stadium, receipt.stadium_id)
        if result.status != 'active':
            raise ValueError('The adopted stadium is retired.')
        return result
    if stadium.current_revision != expected:
        raise ValueError('This stadium changed. Reload before adopting.')
    if stadium.kind == 'official':
        previous_copy = StadiumAdoption.query.filter_by(user_id=user_id, source_stadium_id=stadium.id).order_by(StadiumAdoption.source_revision.desc()).first()
        if previous_copy:
            result = db.session.get(Stadium, previous_copy.stadium_id)
            if result and result.status == 'active':
                return result
    if stadium.current_profile_version == 3 and stadium.owner_user_id == user_id:
        return stadium
    configs, provenance = capture_baselines(user_id)
    profile = current_profile(stadium)
    settings = validate_settings(default_settings(configs), configs)
    if stadium.kind == 'official':
        # Lock the source revision while recording the copy/receipt.
        changed = Stadium.query.filter_by(id=stadium.id, current_revision=expected, status='active').update(
            {'updated_at': Stadium.updated_at}, synchronize_session=False)
        if not changed:
            raise ValueError('This stadium changed. Reload before adopting.')
        for fmt in SUPPORTED_FORMATS:
            provenance[fmt].update(official_source_id=stadium.id, official_source_revision=expected, personal_override=False)
        row = create_personal(user_id, profile, configs, provenance)
    else:
        if stadium.owner_user_id != user_id:
            raise ValueError('Choose an accessible, active stadium.')
        changed = Stadium.query.filter_by(id=stadium.id, current_revision=expected,
            current_profile_version=2, status='active').update(
            {'current_revision': expected + 1, 'current_profile_version': 3,
             'updated_at': datetime.utcnow()}, synchronize_session=False)
        if not changed:
            retry = db.session.get(StadiumAdoption, (user_id, stadium.id, expected))
            if retry:
                db.session.expire_all()
                return db.session.get(Stadium, retry.stadium_id)
            raise ValueError('This stadium changed. Reload before adopting.')
        db.session.add(StadiumRevisionV3(stadium_id=stadium.id, revision=expected+1, **profile,
            format_settings=settings, baseline_configs=configs, baseline_provenance=provenance))
        row = stadium
    db.session.add(StadiumAdoption(user_id=user_id, source_stadium_id=stadium.id,
        source_revision=expected, stadium_id=row.id))
    db.session.flush()
    db.session.expire_all()
    return row


def playable_stadium(stadium_id, user_id, existing_id=None):
    from services.stadiums import accessible_stadium
    row = accessible_stadium(stadium_id, user_id)
    if (row.kind == 'official' or row.current_profile_version != 3) and row.id != existing_id:
        raise ValueError('Add or adopt this stadium in My stadiums before selecting it.')
    return row


def official_baselines():
    """Official tuning uses factory baselines, independent of the admin's preferences."""
    configs = {fmt: get_defaults(fmt, mutable=True) for fmt in SUPPORTED_FORMATS}
    provenance = {fmt: {'source': 'factory', 'capture_version': 3,
        'captured_at': datetime.utcnow().isoformat() + 'Z'} for fmt in SUPPORTED_FORMATS}
    return configs, provenance


def propagate_official_revision(source, profile):
    """Append immutable revisions to unedited personal copies in the same transaction."""
    from services.stadiums import validate_profile
    ids = {receipt.stadium_id for receipt in StadiumAdoption.query.filter_by(source_stadium_id=source.id).all()}
    simple = validate_profile({k: v for k, v in profile.items() if k != 'format_settings'})
    for ident in sorted(ids):
        copy = db.session.get(Stadium, ident)
        if not copy or copy.kind != 'custom' or copy.status != 'active' or copy.current_profile_version != 3:
            continue
        previous = revision_record(copy)
        marker = previous.baseline_provenance.get('T20', {})
        # Older adopted copies had no marker. Only their initial revision is unedited.
        if marker.get('personal_override', copy.current_revision != 1):
            continue
        expected = copy.current_revision
        changed = Stadium.query.filter_by(id=ident, current_revision=expected, status='active').update(
            {'current_revision': expected + 1, 'updated_at': datetime.utcnow()}, synchronize_session=False)
        if not changed:
            raise ValueError('A personal stadium changed. Reload before saving.')
        provenance = deepcopy(previous.baseline_provenance)
        for fmt in SUPPORTED_FORMATS:
            provenance[fmt].update(official_source_id=source.id,
                official_source_revision=source.current_revision, personal_override=False)
        settings = validate_settings(profile.get('format_settings', previous.format_settings), previous.baseline_configs)
        db.session.add(StadiumRevisionV3(stadium_id=ident, revision=expected + 1, **simple,
            format_settings=settings, baseline_configs=deepcopy(previous.baseline_configs), baseline_provenance=provenance))
