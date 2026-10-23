"""Private identity review screens; no enforcement or destructive account actions."""
import json

import click
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select, func, or_
from sqlalchemy.exc import SQLAlchemyError

from auth.decorators import admin_required
from database import db
from database.models import User, IdentityReviewCase as Case, IdentityReviewEvidence as Evidence, IdentityReviewAction as Action
from services import identity_review as review
from utils.identity_devices import keys
from utils.identity_scan import timestamp

bp = Blueprint('admin_identity', __name__, url_prefix='/admin/identity')


@bp.before_request
@admin_required
def guard():
    if session.get('impersonating_from'):
        abort(403)


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


def serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='identity-review-preview-v1')


def users(ids):
    return {u.stable_id: u for u in db.session.execute(select(User).where(User.stable_id.in_(ids))).scalars()}


def page_number():
    try:
        return max(1, min(100000, int(request.args.get('page', 1))))
    except ValueError:
        abort(400)


def base_query():
    return select(Case).where(review.visible(review.now()))


def render(view, **data):
    ids = set()
    cases = list(data.get('cases', [])) + ([data['case']] if 'case' in data else [])
    for case in cases:
        ids.update((case.left_id, case.right_id, case.assignee))
    ids.update(entry.actor_id for entry in data.get('history', []))
    return render_template('admin/identity.html', view=view, people=users(ids), states=review.STATES, observation_time=timestamp, **data)


@bp.get('')
def overview():
    counts = dict(db.session.execute(select(Case.status, func.count()).where(review.visible(review.now())).group_by(Case.status)).all())
    recent = db.session.execute(base_query().order_by(Case.updated_at.desc(), Case.id.desc()).limit(5)).scalars().all()
    return render('overview', counts=counts, cases=recent)


@bp.get('/duplicates')
def duplicates():
    query = base_query()
    status = request.args.get('status', '')
    q = request.args.get('q', '').strip()[:120]
    reason = request.args.get('reason', '')
    if status:
        if status not in review.STATES:
            abort(400)
        query = query.where(Case.status == status)
    if q:
        ids = select(User.stable_id).where(User.id.contains(q, autoescape=True))
        query = query.where(or_(Case.left_id.in_(ids), Case.right_id.in_(ids)))
    if reason:
        if reason not in ('canonical_email', 'confirmed_device', 'fingerprint'):
            abort(400)
        ids = select(Evidence.case_id).where(Evidence.reason == reason, Evidence.expires_at > review.now())
        query = query.where(Case.id.in_(ids))
    page = page_number()
    cases = db.session.execute(query.order_by(Case.updated_at.desc(), Case.id.desc()).offset((page-1)*30).limit(31)).scalars().all()
    return render('queue', cases=cases[:30], more=len(cases)>30, page=page, status=status, q=q, reason=reason)


@bp.post('/scan/preview')
def scan_preview():
    try:
        report, digest = review.preview()
    except (ValueError, SQLAlchemyError) as exc:
        return render('error', error=str(exc) if isinstance(exc, ValueError) else 'Scan unavailable. Try again later.'), 409
    token = serializer().dumps({'actor': current_user.stable_id, 'digest': digest})
    return render('preview', report=report, token=token)


@bp.post('/scan/import')
def scan_import():
    if request.form.get('confirmed') != 'yes':
        return render('error', error='Confirm that you reviewed the preview before importing.'), 400
    try:
        token = serializer().loads(request.form.get('token', ''), max_age=900)
        if token['actor'] != current_user.stable_id:
            abort(403)
        count = review.import_scan(current_user.stable_id, token['digest'])
    except (BadSignature, KeyError, TypeError):
        return render('error', error='Preview expired or invalid. Run a new preview.'), 409
    except ValueError as exc:
        return render('error', error=str(exc)), 409
    except SQLAlchemyError:
        return render('error', error='Import could not be saved. Preview again and retry.'), 409
    db.session.expire_all()
    flash(f'Imported evidence for {count} cases. Closed cases were preserved.', 'success')
    return redirect(url_for('.duplicates'), code=303)


@bp.route('/duplicates/<int:case_id>', methods=['GET', 'POST'])
def detail(case_id):
    case = db.session.execute(base_query().where(Case.id == case_id)).scalar_one_or_none()
    if not case:
        abort(404)
    if request.method == 'POST':
        try:
            version = int(request.form.get('version', ''))
            review.change(case_id, current_user.stable_id, version, request.form.get('action'),
                          request.form.get('reason'), request.form.get('assignee', ''))
        except (ValueError, SQLAlchemyError) as exc:
            return render('error', error=str(exc) if isinstance(exc, ValueError) else 'Could not save. Reload the case and retry.'), 409
        db.session.expire_all()
        flash('Review updated. Account access has not changed.', 'success')
        return redirect(url_for('.detail', case_id=case_id), code=303)
    at = review.now()
    evidence = db.session.execute(select(Evidence).where(Evidence.case_id == case_id, Evidence.expires_at > at).order_by(Evidence.id)).scalars().all()
    history = db.session.execute(select(Action).where(Action.case_id == case_id, Action.expires_at > at).order_by(Action.id.desc()).limit(100)).scalars().all()
    admins = db.session.execute(select(User).where(User.is_admin.is_(True), User.is_banned.is_(False)).order_by(User.id)).scalars().all()
    return render('detail', case=case, evidence=[(e, json.loads(e.payload)) for e in evidence], history=history,
                  transitions=review.TRANSITIONS[case.status], admins=admins)


@bp.get('/accounts/<stable_id>')
def account(stable_id):
    user = db.session.execute(select(User).where(User.stable_id == stable_id)).scalar_one_or_none()
    if not user:
        abort(404)
    page = page_number()
    cases = db.session.execute(base_query().where(or_(Case.left_id == stable_id, Case.right_id == stable_id))
                              .order_by(Case.id.desc()).offset((page-1)*30).limit(31)).scalars().all()
    return render('account', account=user, cases=cases[:30], page=page, more=len(cases)>30)


@bp.get('/policy')
def policy():
    try:
        enabled = bool(keys())
        configuration = 'Enabled' if enabled else 'Disabled — dedicated key not configured'
    except ValueError:
        configuration = 'Disabled by invalid key configuration; operator action required'
    return render('policy', configuration=configuration)


@bp.get('/audit')
def audit():
    page = page_number()
    query = select(Action).join(Case, Action.case_id == Case.id).where(review.visible(review.now()), Action.expires_at > review.now())
    action = request.args.get('action', '')
    if action:
        if action not in (*review.STATES, 'scan_import', 'assign', 'note'):
            abort(400)
        query = query.where(Action.action == action)
    rows = db.session.execute(query.order_by(Action.id.desc()).offset((page-1)*30).limit(31)).scalars().all()
    return render('audit', history=rows[:30], page=page, more=len(rows)>30, action=action)


def init_app(app):
    app.register_blueprint(bp)

    @app.cli.command('purge-identity-reviews')
    def purge():
        """Remove expired evidence/history and cases closed for 180 days."""
        with review.transaction() as conn:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            result = review.cleanup(conn, review.now())
        click.echo(json.dumps(result))
