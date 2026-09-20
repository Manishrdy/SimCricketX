"""Owner-managed tour pages and mutations."""
import secrets
import json
from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from database import db
from database.models import Tour, Team, TeamProfile, Match
from sqlalchemy.orm import selectinload
from engine.tour_engine import (FORMATS, create_tour, ordered_series, reorder_series,
                               tour_summary, series_result, fixture_block_reason, team_format_availability)


def register_tour_routes(app, limiter, cleanup_match, delete_match_json):
    def owned(tour_id):
        tour = db.session.get(Tour, tour_id)
        if not tour or tour.user_id != current_user.id:
            abort(404)
        return tour

    @app.route('/tournaments/tours/create', methods=['GET', 'POST'])
    @login_required
    @limiter.limit('10 per minute')
    def create_tour_route():
        if request.method == 'POST':
            try:
                snapshot = {
                    'name': request.form.get('name', '').strip(),
                    'host': request.form.get('host_team_id', ''),
                    'visitor': request.form.get('visiting_team_id', ''),
                    'scheduled_overs': request.form.get('scheduled_overs', '50'),
                    'counts': {f: request.form.get(f'count_{f}', '0') for f in FORMATS},
                    'order': request.form.getlist('format_order'),
                }
                if 'scheduled_overs' not in request.form:
                    snapshot.pop('scheduled_overs')
                confirmed = json.loads(request.form.get('schedule_confirmation') or 'null')
                if isinstance(confirmed, dict) and isinstance(confirmed.get('counts'), dict):
                    for fmt in FORMATS:
                        confirmed['counts'].setdefault(fmt, '0')
                if (request.form.get('confirm_schedule') != 'yes'
                        or confirmed != snapshot):
                    raise ValueError('Review and confirm the current teams, match counts, and series order before creating your tour.')
                tour = create_tour(request.form.get('name'), current_user.id,
                                   request.form.get('host_team_id'), request.form.get('visiting_team_id'),
                                   {f: request.form.get(f'count_{f}', '0') for f in FORMATS},
                                   request.form.getlist('format_order'),
                                   request.form.get('creation_token') or None,
                                   scheduled_overs=request.form.get('scheduled_overs'))
                return redirect(url_for('tour_dashboard', tour_id=tour.id))
            except (ValueError, TypeError) as exc:
                db.session.rollback()
                flash(str(exc), 'error')
            except Exception:
                db.session.rollback()
                app.logger.exception('Tour creation failed')
                flash('Could not create the tour. Please retry.', 'error')
        teams = (Team.query.options(selectinload(Team.profiles).selectinload(TeamProfile.players))
                 .filter(Team.user_id == current_user.id, Team.is_placeholder.isnot(True))
                 .order_by(Team.name).all())
        availability = {str(t.id): {'name': t.name, 'formats': team_format_availability(t)} for t in teams}
        return render_template('tournaments/tour_create.html', teams=teams, formats=FORMATS,
                               team_availability=availability,
                               creation_token=request.form.get('creation_token') or secrets.token_hex(24))

    @app.route('/tournaments/tours/<int:tour_id>')
    @login_required
    def tour_dashboard(tour_id):
        tour = owned(tour_id)
        series = ordered_series(tour)
        fmt = request.args.get('format') or None
        if fmt and fmt not in {s.format_type for s in series}:
            abort(404)
        next_fixture = next((f for s in series for f in sorted(s.fixtures, key=lambda f: f.round_number)
                             if f.status == 'Scheduled' and not fixture_block_reason(f)), None)
        pending = next((f for s in series for f in sorted(s.fixtures, key=lambda f: f.round_number)
                        if f.status == 'Scheduled'), None)
        return render_template('tournaments/tour_dashboard.html', tour=tour,
                               blocked_reason=fixture_block_reason(pending) if pending and not next_fixture else None,
                               rows=[series_result(s, tour) for s in series],
                               stats=tour_summary(tour, fmt), selected_format=fmt,
                               next_fixture=next_fixture)

    @app.route('/tournaments/tours/<int:tour_id>/rename', methods=['POST'])
    @login_required
    @limiter.limit('10 per minute')
    def rename_tour(tour_id):
        tour = owned(tour_id)
        name = request.form.get('name', '').strip()
        if not name or len(name) > 100:
            flash('Enter a tour name of 1–100 characters.', 'error')
        else:
            tour.name = name
            db.session.commit()
        return redirect(url_for('tour_dashboard', tour_id=tour.id))

    @app.route('/tournaments/tours/<int:tour_id>/reorder', methods=['POST'])
    @login_required
    @limiter.limit('10 per minute')
    def reorder_tour(tour_id):
        tour = owned(tour_id)
        try:
            reorder_series(tour, [int(i) for i in request.form.getlist('series_ids')])
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
        return redirect(url_for('tour_dashboard', tour_id=tour_id))

    @app.route('/tournaments/tours/<int:tour_id>/delete', methods=['POST'])
    @login_required
    @limiter.limit('5 per minute')
    def delete_tour(tour_id):
        tour = owned(tour_id)
        try:
            matches = Match.query.filter(Match.tournament_id.in_([s.id for s in tour.series])).all()
            for fixture in [f for s in tour.series for f in s.fixtures]:
                fixture.match_id = None
                fixture.match = None
            db.session.flush()
            for match in matches:
                cleanup_match(match, rebuild_player_cache=False, delete_json=False)
                db.session.delete(match)
            db.session.delete(tour)
            db.session.commit()
            for match in matches:
                delete_match_json(match)
            flash('Tour deleted successfully.', 'success')
        except Exception:
            db.session.rollback()
            app.logger.exception('Tour deletion failed')
            flash('Could not delete the tour. Please retry.', 'error')
        return redirect(url_for('tournaments'))
