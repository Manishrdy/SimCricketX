"""Convert current stadium revisions to v2 without changing historical data."""
import json
from sqlalchemy import inspect, text


def convert_v1_profile(profile):
    """Deterministic conversion of a saved v1 revision; never change match snapshots."""
    from services.stadiums import validate_profile, BOUNDARIES
    from engine.format_catalog import SUPPORTED_FORMATS
    from engine.ground_config import get_pitch_options
    pitches = {}
    for fmt in SUPPORTED_FORMATS:
        weights = profile['pitch_profile']['preparation_weights'][fmt]
        options = [p['key'] for p in get_pitch_options(fmt)]
        pitches[fmt] = max(options, key=lambda key: weights.get(key, 0))
    return validate_profile({
        'name': profile['name'], 'pitch_defaults': pitches,
        'outfield_speed': profile['outfield_profile']['speed_multiplier'],
        'dimensions_m': {key: profile['geometry_profile'][key]['typical'] for key in BOUNDARIES},
        'average_temperature_c': round(sum(profile['climate_profile']['months'][str(month)]['temperature_c']['typical']
                                         for month in range(1, 13)) / 12, 1),
    })


def convert_existing(conn):
    from database.models import StadiumRevision, Stadium
    tables = inspect(conn).get_table_names()
    if 'stadium_revisions' not in tables:
        return
    rows = conn.execute(text('''SELECT r.* FROM stadium_revisions r
        JOIN stadiums s ON s.id = r.stadium_id AND s.current_revision = r.revision
        WHERE NOT EXISTS (SELECT 1 FROM stadium_revisions_v2 v WHERE v.stadium_id = s.id)''')).mappings().all()
    for row in rows:
        profile = dict(row)
        for key in ('pitch_profile', 'geometry_profile', 'outfield_profile', 'climate_profile'):
            if isinstance(profile[key], str):
                profile[key] = json.loads(profile[key])
        simple = convert_v1_profile(profile)
        revision = row['revision'] + 1
        conn.execute(StadiumRevision.__table__.insert().values(
            stadium_id=row['stadium_id'], revision=revision, schema_version=2, **simple))
        conn.execute(Stadium.__table__.update().where(Stadium.id == row['stadium_id']).values(current_revision=revision))


def run_migration(db, app):
    from database.models import StadiumRevision
    with app.app_context():
        StadiumRevision.__table__.create(db.engine, checkfirst=True)
        with db.engine.begin() as conn:
            convert_existing(conn)
