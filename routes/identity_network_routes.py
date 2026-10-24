"""Community network appeals and admin-only network policy review."""
import json
import os

import click
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError

from auth.decorators import admin_required
from database import db
from database.models import (User, IdentityNetworkAppeal as Appeal, IdentityNetworkAction as Action,
    IdentityNetworkEvent as Event)
from services import identity_network as network
from services.identity_signup import key
from utils import network_classifier as classifier

bp = Blueprint('identity_network', __name__)


@bp.before_request
def protect_admin():
    if request.path.startswith('/admin/'):
        if session.get('impersonating_from'):
            abort(403)
        return admin_required(lambda: None)()


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


def page_number():
    try:
        return max(1, min(100000, int(request.args.get('page', 1))))
    except ValueError:
        abort(400)


@bp.get('/privacy/network-checks')
def privacy():
    return render_template('network_privacy.html')


@bp.route('/community/network-appeal', methods=['GET', 'POST'])
@login_required
def appeal():
    if not network.enabled():
        abort(404)
    if session.get('impersonating_from'):
        abort(403)
    decision = network.evaluate()
    if decision['state'] not in ('allowed', 'restricted'):
        abort(403)
    error = None
    if request.method == 'POST':
        if decision['state'] != 'restricted':
            error = 'Your current network is not restricted. An existing request or exception is shown below.'
        else:
            try:
                network.submit_appeal(current_user.stable_id, request.form.get('message'), decision['categories'])
                flash('Your private network appeal is ready for admin review.', 'success')
                return redirect(url_for('.appeal'), code=303)
            except ValueError as exc:
                error = str(exc)
            except SQLAlchemyError:
                error = 'Could not save your request. Please retry.'
    at = network.now()
    row = db.session.execute(select(Appeal).where(Appeal.user_stable_id == current_user.stable_id,
        network.visible(at)).order_by(Appeal.id.desc()).limit(1)).scalar_one_or_none()
    state = 'expired' if row and row.status == 'approved' and row.expires_at <= at else (row.status if row else None)
    return render_template('community/network_appeal.html', appeal=row, state=state, error=error,
        can_submit=decision['state'] == 'restricted' and state not in ('open', 'approved')), 409 if error else 200


@bp.get('/admin/identity/network')
def overview():
    at = network.now()
    page = page_number()
    status = request.args.get('status', 'open')
    if status not in ('open', 'approved', 'rejected', 'revoked', 'expired', 'all'):
        abort(400)
    query = select(Appeal).where(network.visible(at))
    if status == 'expired':
        query = query.where((Appeal.status == 'expired') | ((Appeal.status == 'approved') & (Appeal.expires_at <= at)))
    elif status != 'all':
        query = query.where(Appeal.status == status)
        if status == 'approved':
            query = query.where(Appeal.expires_at > at)
    rows = db.session.execute(query.order_by(Appeal.id.desc()).offset((page-1)*30).limit(31)).scalars().all()
    people = {u.stable_id: u for u in db.session.execute(select(User).where(User.stable_id.in_([r.user_stable_id for r in rows]))).scalars()}
    budget = db.session.execute(select(classifier.B).where(classifier.B.c.day == at.date().isoformat())).mappings().first()
    feed = db.session.execute(select(classifier.F.c.imported_at).where(classifier.F.c.id == 1)).scalar_one_or_none()
    counts = dict(db.session.execute(select(Event.state, func.count()).where(Event.expires_at > at).group_by(Event.state)).all())
    return render_template('admin/identity_network.html', view='overview', rows=rows[:30], people=people,
        more=len(rows)>30, page=page, status=status, at=at, budget=budget, feed=feed,
        feed_fresh=bool(feed and at-network.timedelta(hours=48) < feed <= at), counts=counts,
        configured=bool(current_app.config.get('PROXYCHECK_API_KEY')))


@bp.route('/admin/identity/network/<int:appeal_id>', methods=['GET', 'POST'])
def detail(appeal_id):
    at = network.now()
    row = db.session.execute(select(Appeal).where(Appeal.id == appeal_id, network.visible(at))).scalar_one_or_none()
    if not row:
        abort(404)
    error = None
    if request.method == 'POST':
        try:
            if request.form.get('confirmed') != 'yes':
                raise ValueError('Confirm you reviewed the seven-day network-only exception.')
            network.decide(appeal_id, current_user.stable_id, int(request.form.get('version', '')),
                request.form.get('action'), request.form.get('reason'))
            flash('Network review updated.', 'success')
            return redirect(url_for('.detail', appeal_id=appeal_id), code=303)
        except ValueError as exc:
            error = str(exc)
        except SQLAlchemyError:
            error = 'Could not save. Reload and retry.'
    person = db.session.execute(select(User).where(User.stable_id == row.user_stable_id)).scalar_one_or_none()
    history = db.session.execute(select(Action).where(Action.appeal_id == appeal_id, Action.expires_at > at).order_by(Action.id)).scalars().all()
    return render_template('admin/identity_network.html', view='detail', appeal=row, person=person,
        history=history, categories=json.loads(row.categories), error=error, at=at), 409 if error else 200


def init_app(app, limiter):
    app.config.setdefault('IDENTITY_NETWORK_ENFORCE', os.environ.get('IDENTITY_NETWORK_ENFORCE', '').lower() == 'true')
    app.config.setdefault('PROXYCHECK_API_KEY', os.environ.get('PROXYCHECK_API_KEY', ''))
    with app.app_context():
        if network.enabled():
            key()  # Separate HMAC purpose; reuse the existing stable identity key.
            if not app.config.get('PROXYCHECK_API_KEY'):
                raise ValueError('PROXYCHECK_API_KEY required before network enforcement activation')
    app.register_blueprint(bp)
    app.view_functions['identity_network.appeal'] = limiter.limit('5 per hour', methods=['POST'])(app.view_functions['identity_network.appeal'])

    @app.context_processor
    def context():
        decision = request.environ.get('scx.network.decision', {})
        return {'network_restricted': decision.get('state') == 'restricted', 'identity_network_enabled': network.enabled()}

    @app.after_request
    def restricted_cache(response):
        if request.environ.get('scx.network.decision', {}).get('state') == 'restricted':
            response.headers['Cache-Control'] = 'private, no-store'
        return response

    @app.cli.command('refresh-private-relay-feed')
    def refresh():
        """Fetch only Apple's fixed HTTPS feed; atomic replacement, no scheduling."""
        try:
            content = classifier._download(classifier.RELAY_URL, limit=5_000_000, seconds=15)
            count = classifier.import_relay_csv(content)
        except Exception:
            raise click.ClickException('Relay refresh failed; previous feed preserved. Check connectivity and feed format.') from None
        click.echo(f'Imported {count} Private Relay networks. Schedule daily refresh; evidence expires after 48 hours.')

    @app.cli.command('purge-network-evidence')
    def purge():
        with network.transaction() as conn:
            network.cleanup(conn, network.now())
        click.echo('Expired network cache, events and closed appeals removed; active exceptions preserved.')
