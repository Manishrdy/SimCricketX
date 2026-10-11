"""Authenticated stadium catalogue, personal overrides and official administration."""
from datetime import datetime
from flask import request, render_template, jsonify, redirect, url_for, flash, session
from flask_login import login_required, current_user
from database import db
from database.models import Stadium, StadiumRevision, StadiumRevisionV3, StadiumAdoption
from sqlalchemy.exc import IntegrityError
from services.stadium_tuning import (create_personal, adopt, playable_stadium, capture_baselines, default_settings, validate_settings, official_baselines, propagate_official_revision)
from services.stadiums import (visible_stadiums, accessible_stadium, stadium_dict,
    current_profile, neutral_profile, validate_profile, BOUNDARIES, temperature_wear_multiplier)
from engine.ground_config import get_pitch_options
from engine.format_catalog import SUPPORTED_FORMATS


def register_stadium_routes(app):
    import os
    app.config.setdefault('STADIUM_V3_ENABLED', os.getenv('STADIUM_V3_ENABLED', 'true').lower() not in ('0', 'false', 'off'))
    app.config.setdefault('STADIUM_MATCHES_ENABLED', os.getenv('STADIUM_MATCHES_ENABLED', 'true').lower() not in ('0', 'false', 'off'))

    @app.context_processor
    def stadium_context():
        # Callable avoids a catalogue query on every unrelated page.
        def choices(existing_id=None, include_legacy=False):
            if not current_user.is_authenticated:
                return []
            return sorted([stadium_dict(s) for s in visible_stadiums(current_user.id).all()
                if include_legacy or (s.kind == 'custom' and s.current_profile_version == 3) or s.id == existing_id], key=lambda s: s['name'])
        from engine.stadium_tuning import phase_ranges
        from engine.format_config import get_any_format
        ranges = {fmt: phase_ranges(get_any_format(fmt)) for fmt in SUPPORTED_FORMATS}
        ranges['ListA40'] = phase_ranges(get_any_format('ListA', scheduled_overs=40))
        return {'stadium_choices': choices, 'stadium_phase_ranges': ranges, 'stadium_tuning_formats': SUPPORTED_FORMATS}

    def catalogue(rows=None):
        if rows is None:
            rows = [stadium_dict(s) for s in visible_stadiums(current_user.id).all()]
        q = request.args.get('q', '').casefold().strip()
        kind = request.args.get('kind', '')
        return sorted([s for s in rows if
            (not q or q in s['name'].casefold()) and
            (not kind or s['kind'] == kind)], key=lambda s: s['name'])

    @app.get('/stadiums')
    @login_required
    def stadiums():
        rows = [stadium_dict(s) for s in visible_stadiums(current_user.id).all()]
        added = {receipt.source_stadium_id for receipt in StadiumAdoption.query.join(
            Stadium, Stadium.id == StadiumAdoption.stadium_id).filter(
            StadiumAdoption.user_id == current_user.id, Stadium.status == 'active').all()}
        for row in rows:
            row['in_my_stadiums'] = row['id'] in added
        counts = {kind: sum(s['kind'] == kind for s in rows) for kind in ('official', 'custom')}
        return render_template('stadiums.html', stadiums=catalogue(rows),
            collection_counts=counts, total_stadiums=len(rows))

    @app.get('/api/stadiums')
    @login_required
    def stadiums_api():
        try:
            page = max(1, int(request.args.get('page', 1)))
        except ValueError:
            return jsonify(error='Invalid page.'), 400
        rows = catalogue()
        return jsonify(items=rows[(page-1)*25:page*25], total=len(rows), page=page)

    @app.post('/api/team/<int:team_id>/stadium')
    @login_required
    def link_team_stadium(team_id):
        from database.models import Team
        team = db.session.get(Team, team_id)
        if not team or team.user_id != current_user.id:
            return jsonify(error='Team not found.'), 404
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='Expected a stadium selection.'), 400
        try:
            stadium = playable_stadium(data.get('stadium_id'), current_user.id, team.stadium_id)
            team.stadium_id = stadium.id
            team.venue_mode = 'stadium'
            team.updated_at = datetime.utcnow()
            db.session.commit()
            return jsonify(ok=True, stadium_id=stadium.id, venue_mode='stadium')
        except ValueError as exc:
            db.session.rollback()
            return jsonify(error=str(exc)), 400

    @app.get('/api/stadiums/<stadium_id>')
    @login_required
    def stadium_api(stadium_id):
        try:
            return jsonify(stadium_dict(accessible_stadium(stadium_id, current_user.id, active=False)))
        except ValueError as exc:
            return jsonify(error=str(exc)), 404

    @app.route('/stadiums/new', methods=['GET', 'POST'])
    @app.route('/stadiums/<stadium_id>', methods=['GET', 'POST'])
    @login_required
    def stadium_detail(stadium_id=None):
        try:
            row = accessible_stadium(stadium_id, current_user.id, active=False) if stadium_id else None
            profile = current_profile(row) if row else {**neutral_profile(), 'format_settings': default_settings(capture_baselines(current_user.id)[0])}
            if request.args.get('copy') and not row:
                profile = current_profile(accessible_stadium(request.args['copy'], current_user.id))
                profile['format_settings'] = default_settings(capture_baselines(current_user.id)[0])
                profile['name'] += ' copy'
            editable = row is None or (row.status == 'active' and ((row.kind == 'custom' and row.owner_user_id == current_user.id) or (row.kind == 'official' and current_user.is_admin)))
            if editable and row and row.kind == 'official' and row.current_profile_version == 2:
                profile['format_settings'] = default_settings(official_baselines()[0])
            safe_profile = dict(profile)
            saved_draft_key = session.pop('saved_stadium_draft_key', None) if request.method == 'GET' else None
            draft_key = f'stadium-tuning:{current_user.id}:{row.id if row else "new"}:{row.current_revision if row else 0}'
            if request.method == 'POST':
                if not editable:
                    return 'This stadium is read-only.', 403
                allowed = {'csrf_token', 'revision', 'format_settings', 'name', 'outfield_speed', 'average_temperature_c', *BOUNDARIES,
                           *(f'pitch_{fmt}' for fmt in SUPPORTED_FORMATS)}
                if row and row.current_profile_version == 2 and row.kind != 'official' and 'format_settings' in request.form:
                    raise ValueError('Adopt stadium conditions before editing tuning.')
                if set(request.form) - allowed:
                    raise ValueError('Unsupported stadium form fields.')
                profile = {
                    'name': request.form.get('name', ''),
                    'pitch_defaults': {fmt: request.form.get(f'pitch_{fmt}', '') for fmt in SUPPORTED_FORMATS},
                    'dimensions_m': {key: float(request.form.get(key, 'nan')) for key in BOUNDARIES},
                    'outfield_speed': float(request.form.get('outfield_speed', 'nan')),
                    'average_temperature_c': float(request.form.get('average_temperature_c', 'nan')),
                }
                profile = validate_profile(profile)
                if not row or row.current_profile_version == 3 or row.kind == 'official':
                    import json
                    try:
                        settings = json.loads(request.form.get('format_settings', '{}'))
                    except (ValueError, TypeError) as exc:
                        raise ValueError('Invalid format settings.') from exc
                    if not settings:
                        settings = safe_profile['format_settings']
                    profile['format_settings'] = settings
                if row:
                    expected = int(request.form.get('revision', 0))
                    update_revision(row, expected, profile)
                else:
                    row = create_personal(current_user.id, profile)
                db.session.commit()
                db.session.expire_all()
                session['saved_stadium_draft_key'] = draft_key
                flash('Stadium saved. Existing matches keep their original conditions.', 'success')
                return redirect(url_for('stadium_detail', stadium_id=row.id))
            return render_template('stadium_detail.html', stadium=row, profile=profile, editable=editable, saved_draft_key=saved_draft_key, pitch_options={fmt: get_pitch_options(fmt) for fmt in SUPPORTED_FORMATS})
        except ValueError as exc:
            db.session.rollback()
            if request.method == 'GET':
                return str(exc), 404
            return render_template('stadium_detail.html', stadium=locals().get('row'),
                profile=locals().get('safe_profile', neutral_profile()), editable=locals().get('editable', False), error=str(exc),
                pitch_options={fmt: get_pitch_options(fmt) for fmt in SUPPORTED_FORMATS}), 400

    def update_revision(row, expected, profile):
        allowed = ((row.kind == 'custom' and row.owner_user_id == current_user.id)
                   or (row.kind == 'official' and current_user.is_admin))
        if not allowed or row.status != 'active':
            raise ValueError('You cannot edit this stadium.')
        promote = row.kind == 'official' and row.current_profile_version == 2
        if promote:
            from services.stadium_tuning import require_enabled
            require_enabled()
        changed = Stadium.query.filter_by(id=row.id, current_revision=expected, status='active').update(
            {'current_revision': expected + 1, 'current_profile_version': 3 if promote else row.current_profile_version, 'updated_at': datetime.utcnow()}, synchronize_session=False)
        if not changed:
            raise ValueError('This stadium changed. Reload before saving.')
        if row.current_profile_version == 3:
            previous = db.session.get(StadiumRevisionV3, (row.id, expected))
            simple = validate_profile({k: v for k, v in profile.items() if k != 'format_settings'})
            settings = validate_settings(profile['format_settings'], previous.baseline_configs)
            from copy import deepcopy
            provenance = deepcopy(previous.baseline_provenance)
            if row.kind == 'custom':
                modified = settings != previous.format_settings or any(
                    value != getattr(previous, key) for key, value in simple.items())
                for fmt in SUPPORTED_FORMATS:
                    provenance[fmt]['personal_override'] = modified or provenance[fmt].get('personal_override', expected != 1)
            db.session.add(StadiumRevisionV3(stadium_id=row.id, revision=expected+1, **simple,
                format_settings=settings, baseline_configs=previous.baseline_configs,
                baseline_provenance=provenance))
        elif promote:
            configs, provenance = official_baselines()
            simple = validate_profile({k: v for k, v in profile.items() if k != 'format_settings'})
            settings = validate_settings(profile.get('format_settings', default_settings(configs)), configs)
            profile = {**simple, 'format_settings': settings}
            db.session.add(StadiumRevisionV3(stadium_id=row.id, revision=expected+1, **simple,
                format_settings=settings, baseline_configs=configs, baseline_provenance=provenance))
        else:
            db.session.add(StadiumRevision(stadium_id=row.id, revision=expected+1, **validate_profile(profile)))
        if row.kind == 'official':
            db.session.flush()
            db.session.refresh(row)
            propagate_official_revision(row, profile)

    @app.post('/api/stadiums')
    @login_required
    def create_stadium_api():
        try:
            row = create_personal(current_user.id, request.get_json(silent=True))
            db.session.commit()
            return jsonify(stadium_dict(row)), 201
        except ValueError as exc:
            db.session.rollback()
            return jsonify(error=str(exc)), 400

    @app.patch('/api/stadiums/<stadium_id>')
    @login_required
    def edit_stadium_api(stadium_id):
        try:
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict) or set(data) - {'revision', 'profile'} or not isinstance(data.get('profile', {}), dict):
                raise ValueError('Expected a profile object.')
            row = accessible_stadium(stadium_id, current_user.id)
            profile = {**current_profile(row), **data.get('profile', {})}
            allowed_fields = {'name', 'pitch_defaults', 'outfield_speed', 'dimensions_m', 'average_temperature_c'}
            if row.current_profile_version == 3 or (row.kind == 'official' and current_user.is_admin):
                allowed_fields.add('format_settings')
            if set(profile) - allowed_fields:
                raise ValueError('Unsupported stadium profile fields.')
            expected = data.get('revision')
            if isinstance(expected, bool) or not isinstance(expected, int):
                raise ValueError('A valid revision is required.')
            update_revision(row, expected, profile)
            db.session.commit()
            db.session.expire_all()
            return jsonify(stadium_dict(row))
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            return jsonify(error=str(exc)), 400

    @app.post('/api/stadiums/<stadium_id>/retire')
    @login_required
    def retire_stadium_api(stadium_id):
        try:
            row = accessible_stadium(stadium_id, current_user.id, active=False)
            if row.kind != 'custom' or row.owner_user_id != current_user.id:
                return jsonify(error='Only your custom stadiums can be retired.'), 403
            row.status = 'retired'
            db.session.commit()
            return jsonify(ok=True)
        except ValueError as exc:
            return jsonify(error=str(exc)), 404

    @app.post('/api/stadiums/<stadium_id>/preview')
    @login_required
    def stadium_preview(stadium_id):
        try:
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict) or set(data) - {'match_format', 'scheduled_overs'}:
                raise ValueError('Preview accepts a match format and scheduled length.')
            fmt = data.get('match_format', 'T20')
            if fmt not in SUPPORTED_FORMATS:
                raise ValueError('Invalid match format.')
            row = accessible_stadium(stadium_id, current_user.id)
            p = current_profile(row)
            from engine.format_config import get_any_format
            from engine.format_catalog import FORMAT_CATALOG
            from engine.stadium_tuning import phase_ranges
            length = data.get('scheduled_overs')
            if length is not None and (isinstance(length, bool) or length not in FORMAT_CATALOG[fmt]['lengths']):
                raise ValueError('Invalid scheduled length.')
            rules = get_any_format(fmt, **({'scheduled_overs': length} if length is not None else {}))
            extra = {'schema_version': row.current_profile_version, 'phase_ranges': phase_ranges(rules)}
            if row.current_profile_version == 3:
                extra['format_settings'] = p['format_settings'][fmt]
            return jsonify(**extra, name=p['name'], revision=row.current_revision, match_format=fmt,
                pitch=p['pitch_defaults'][fmt], dimensions_m=p['dimensions_m'],
                outfield_speed=p['outfield_speed'], average_temperature_c=p['average_temperature_c'],
                temperature_wear_multiplier=temperature_wear_multiplier(p['average_temperature_c']))
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/stadiums/<stadium_id>/add')
    @login_required
    def add_stadium(stadium_id):
        try:
            source = accessible_stadium(stadium_id, current_user.id)
            row = adopt(source, current_user.id, int(request.form.get('revision', 0)))
            db.session.commit()
            flash('Stadium added to My stadiums.', 'success')
        except (ValueError, TypeError, IntegrityError) as exc:
            db.session.rollback()
            flash('Unable to add stadium. Refresh and try again.' if isinstance(exc, IntegrityError) else str(exc), 'error')
        return redirect(url_for('stadiums', kind='custom'))

    @app.post('/api/stadiums/<stadium_id>/adopt')
    @login_required
    def adopt_stadium_api(stadium_id):
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or set(data) != {'revision'}:
                raise ValueError('A source revision is required.')
            source = accessible_stadium(stadium_id, current_user.id)
            row = adopt(source, current_user.id, data['revision'])
            db.session.commit()
            return jsonify(stadium_dict(row))
        except IntegrityError:
            db.session.rollback()
            receipt = db.session.get(StadiumAdoption, (current_user.id, stadium_id, data['revision']))
            if receipt:
                try:
                    return jsonify(stadium_dict(accessible_stadium(receipt.stadium_id, current_user.id)))
                except ValueError as exc:
                    return jsonify(error=str(exc)), 409
            return jsonify(error='This stadium changed. Reload before adopting.'), 409
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            return jsonify(error=str(exc)), 400

    @app.get('/stadiums/conditions-guide')
    def stadium_conditions_guide():
        return render_template('stadium_conditions_guide.html')
