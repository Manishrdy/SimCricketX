"""Private public appeals and admin signup review, with no account on refusal."""
import json
import os

import click
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user
from sqlalchemy import select, or_
from sqlalchemy.exc import SQLAlchemyError

from auth.decorators import admin_required
from database import db
from database.models import IdentitySignupAppeal, IdentitySignupAttempt, IdentitySignupAction
from services import identity_signup as signup
from utils import email_service
from utils.identity_rules import InvalidEmail
from utils.turnstile import verify_turnstile
from utils.client_ip import get_client_ip

bp = Blueprint('identity_signup', __name__)


@bp.before_request
def guard():
    is_admin_page = request.path.startswith('/admin/')
    if not (signup.enabled() or current_app.config.get('IDENTITY_NETWORK_ENFORCE')) and not is_admin_page:
        abort(404)
    if is_admin_page:
        if session.get('impersonating_from'):
            abort(403)
        return admin_required(lambda: None)()


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@bp.route('/signup-review', methods=['GET', 'POST'])
def appeal():
    error = None
    if request.method == 'POST':
        try:
            ok, _ = verify_turnstile(request.form.get('cf-turnstile-response', ''), remote_ip=get_client_ip())
            if not ok or request.form.get('website'):
                raise ValueError('Human verification failed. Please try again.')
            verifier = current_app.extensions['identity_signup_pow']
            ok, _ = verifier(request.form.get('challenge_id', ''), request.form.get('challenge_counter', ''), request.form.get('challenge_digest', ''))
            if not ok:
                raise ValueError('Security challenge failed. Refresh and try again.')
            address, token = signup.request_mailbox(request.form.get('email', ''), request.form.get('message', ''))
            link = email_service._app_url().rstrip('/') + url_for('.verify', token=token)
            html = render_template('identity_signup_email.html', link=link)
            if not email_service.send_email(address, 'Verify your signup review request', html):
                raise ValueError('Verification email could not be sent. Please retry later.')
            return render_template('signup_review.html', view='sent')
        except InvalidEmail:
            error = 'Enter a valid email address.'
        except ValueError as exc:
            error = str(exc)
        except SQLAlchemyError:
            error = 'Review requests are temporarily unavailable. Please retry later.'
    return render_template('signup_review.html', view='form', error=error), 400 if error else 200


@bp.route('/signup-review/verify', methods=['GET', 'POST'])
def verify():
    token = request.values.get('token', '')
    if request.method == 'GET':
        return render_template('signup_review.html', view='verify', token=token)
    try:
        signup.verify_mailbox(token)
    except (ValueError, SQLAlchemyError):
        return render_template('signup_review.html', view='form', error='Verification link is invalid or expired. Request a new one.'), 400
    return redirect(url_for('.status'), code=303)


@bp.get('/signup-review/status')
def status():
    email = signup.mailbox_proof()
    if not email:
        return redirect(url_for('.appeal'))
    at = signup.now()
    row = db.session.execute(select(IdentitySignupAppeal).where(IdentitySignupAppeal.email == email,
        signup.visible_appeal(at))
        .order_by(IdentitySignupAppeal.id.desc()).limit(1)).scalar_one_or_none()
    state = row.status if row else 'unavailable'
    if row and row.expires_at and row.expires_at <= at and state in ('approved', 'pending'):
        state = 'expired'
    return render_template('signup_review.html', view='status', state=state,
        scopes=json.loads(row.scopes) if row and state == 'approved' else [], expires=row.expires_at if row else None)


def page_number():
    try:
        return max(1, min(100000, int(request.args.get('page', 1))))
    except ValueError:
        abort(400)


@bp.get('/admin/identity/signup')
def queue():
    at = signup.now()
    page = page_number()
    state = request.args.get('status', 'open')
    if state not in ('open', 'approved', 'rejected', 'revoked', 'used', 'expired', 'all'):
        abort(400)
    query = select(IdentitySignupAppeal).where(IdentitySignupAppeal.verified_at.is_not(None),
        signup.visible_appeal(at))
    if state == 'expired':
        query = query.where(or_(IdentitySignupAppeal.status == 'expired',
            (IdentitySignupAppeal.status == 'approved') & (IdentitySignupAppeal.expires_at <= at)))
    elif state != 'all':
        query = query.where(IdentitySignupAppeal.status == state)
        if state == 'approved':
            query = query.where(IdentitySignupAppeal.expires_at > at)
    rows = db.session.execute(query.order_by(IdentitySignupAppeal.id.desc()).offset((page-1)*30).limit(31)).scalars().all()
    return render_template('admin/identity_signup.html', view='queue', rows=rows[:30], more=len(rows)>30, page=page, state=state, at=at)


@bp.get('/admin/identity/signup/attempts')
def attempts():
    at = signup.now()
    page = page_number()
    rows = db.session.execute(select(IdentitySignupAttempt).where(IdentitySignupAttempt.expires_at > at)
        .order_by(IdentitySignupAttempt.last_seen.desc(), IdentitySignupAttempt.id.desc()).offset((page-1)*30).limit(31)).scalars().all()
    return render_template('admin/identity_signup.html', view='attempts', rows=rows[:30], more=len(rows)>30, page=page)


@bp.route('/admin/identity/signup/<int:appeal_id>', methods=['GET', 'POST'])
def detail(appeal_id):
    at = signup.now()
    row = db.session.execute(select(IdentitySignupAppeal).where(IdentitySignupAppeal.id == appeal_id,
        IdentitySignupAppeal.verified_at.is_not(None),
        signup.visible_appeal(at))).scalar_one_or_none()
    if not row:
        abort(404)
    error = None
    if request.method == 'POST':
        try:
            if request.form.get('confirmed') != 'yes':
                raise ValueError('Confirm that you reviewed ownership and the requested exceptions.')
            signup.decide(appeal_id, current_user.stable_id, int(request.form.get('version', '')),
                request.form.get('action'), request.form.get('reason'), request.form.getlist('scopes'))
            flash('Signup review updated.', 'success')
            return redirect(url_for('.detail', appeal_id=appeal_id), code=303)
        except ValueError as exc:
            error = str(exc)
        except SQLAlchemyError:
            error = 'Could not save. Reload and retry.'
    history = db.session.execute(select(IdentitySignupAction).where(IdentitySignupAction.appeal_id == appeal_id,
        IdentitySignupAction.expires_at > at).order_by(IdentitySignupAction.id.desc()).limit(100)).scalars().all()
    evidence = db.session.execute(select(IdentitySignupAttempt).where(IdentitySignupAttempt.identity_hash == row.identity_hash,
        IdentitySignupAttempt.expires_at > at)).scalars().all()
    return render_template('admin/identity_signup.html', view='detail', row=row, history=history,
        evidence=[(e, json.loads(e.evidence)) for e in evidence], scopes=signup.SCOPES, error=error, at=at), 409 if error else 200


def init_app(app, limiter, pow_verifier):
    app.config.setdefault('IDENTITY_SIGNUP_ENFORCE', os.environ.get('IDENTITY_SIGNUP_ENFORCE', '').lower() == 'true')
    app.config.setdefault('IDENTITY_SIGNUP_KEY', os.environ.get('IDENTITY_SIGNUP_KEY', ''))
    with app.app_context():
        if signup.enabled():
            signup.key()
            if not signup.devices.keys():
                raise ValueError('Device collection must be configured before signup enforcement')
    app.extensions['identity_signup_pow'] = pow_verifier
    app.register_blueprint(bp)
    for endpoint, limit in (('appeal', '5 per hour'), ('verify', '20 per minute')):
        name = 'identity_signup.' + endpoint
        app.view_functions[name] = limiter.limit(limit, methods=['POST'])(app.view_functions[name])

    @app.context_processor
    def context():
        return {'identity_signup_enforced': signup.enabled(),
                'identity_signup_ready': bool(app.config.get('IDENTITY_SIGNUP_KEY')),
                'signup_scope_labels': signup.SCOPE_LABELS}

    @app.cli.command('purge-signup-evidence')
    def purge():
        with signup.transaction() as conn:
            signup.cleanup(conn, signup.now())
        click.echo('Expired signup evidence, requests and history removed. Deletion markers retained.')
