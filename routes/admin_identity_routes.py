"""Private identity review screens; no enforcement or destructive account actions."""
from collections import deque
import json
import time

import click
from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select, func, or_
from sqlalchemy.exc import SQLAlchemyError

from auth.decorators import admin_required
from database import db
from database.models import User, IdentityReviewCase as Case, IdentityReviewEvidence as Evidence, IdentityReviewAction as Action
from services import identity_review as review, identity_hub as hub, identity_account_review as account_review
from utils.identity_scan import timestamp

bp = Blueprint('admin_identity', __name__, url_prefix='/admin/identity')


@bp.before_request
@admin_required
def guard():
    if session.get('impersonating_from'):
        abort(403)
    from services.identity_email_change import administrator
    with db.engine.connect() as conn:
        try:
            administrator(conn, current_user.stable_id)
        except ValueError:
            abort(403)


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
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
    from database.models import IdentityMerge
    merged = set(db.session.execute(select(IdentityMerge.source).where(IdentityMerge.status == 'completed', IdentityMerge.source.in_(ids))).scalars())
    return render_template('admin/identity.html', view=view, people=users(ids), merged=merged, states=review.STATES, observation_time=timestamp, **data)


def hub_render(view, **data):
    return render_template('admin/identity_hub.html', view=view, workflows=hub.WORKFLOWS, **data)


def hub_filters(**defaults):
    values = {**defaults, **request.args.to_dict()}
    try:
        return hub.filters(values, grants=request.endpoint in ('admin_identity.approvals', 'admin_identity.exemptions'))
    except ValueError as exc:
        abort(400, description=str(exc))


def pagination(f):
    args = {k: f[k] for k in ('workflow', 'action', 'q', 'from', 'to', 'status') if f.get(k)}
    return {'previous': url_for(request.endpoint, **(dict(stable_id=request.view_args['stable_id']) if request.view_args and 'stable_id' in request.view_args else {}), **args, page=f['page']-1) if f['page']>1 else None,
            'next': url_for(request.endpoint, **(dict(stable_id=request.view_args['stable_id']) if request.view_args and 'stable_id' in request.view_args else {}), **args, page=f['page']+1)}


@bp.get('')
def overview():
    data = hub.overview()
    recent, _ = hub.audit_rows(hub.filters({}))
    return hub_render('overview', recent=recent[:5], **data)


@bp.post('/account-review/run')
def account_review_run():
    if request.accept_mimetypes.best == 'application/x-ndjson':
        return Response(
            account_review_events(account_review.database_path(), current_user.stable_id,
                                  hub.configuration(), current_app.logger),
            mimetype='application/x-ndjson', headers={'X-Accel-Buffering': 'no'},
        )
    try:
        account_review.execute(current_user.stable_id, hub.configuration())
    except (account_review.ReviewBusy, account_review.ReviewUnavailable) as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('.account_review_report'), code=303)
    except Exception:  # A failed scan must leave the prior report accessible.
        current_app.logger.exception('Identity account review failed')
        flash('Identity review could not finish. The previous successful report is unchanged. Please retry.', 'error')
        return redirect(url_for('.account_review_report'), code=303)
    flash('Identity review completed. No accounts or review cases were changed.', 'success')
    return redirect(url_for('.account_review_report'), code=303)


def account_review_events(path, actor, configuration, logger):
    from gevent import sleep

    # Only the worker appends events. It can finish and publish even if the
    # browser disconnects, without retaining the Flask request context.
    events = deque()
    started = time.monotonic()

    def event(status, message, **counts):
        return json.dumps(dict(status=status, message=message,
                               elapsed_ms=round((time.monotonic() - started) * 1000), **counts)) + '\n'

    def progress(message, **counts):
        events.append(event('running', message, **counts))

    yield event('running', 'Starting identity review')
    try:
        job = account_review.start(path, actor, configuration, progress=progress)
        heartbeat = started + 5
        while not job.ready():
            while events:
                yield events.popleft()
            if time.monotonic() >= heartbeat:
                yield event('heartbeat', '')
                heartbeat = time.monotonic() + 5
            sleep(0.1)
        while events:
            yield events.popleft()
        job.get()
    except (account_review.ReviewBusy, account_review.ReviewUnavailable, TimeoutError) as exc:
        yield event('failed', str(exc) or 'Identity review timed out.')
    except Exception:
        logger.exception('Identity account review failed')
        yield event('failed', 'Identity review could not finish. The previous successful report is unchanged.')
    else:
        yield event('complete', 'Report saved')


@bp.get('/account-review')
def account_review_report():
    q = request.args.get('q', '').strip()[:254]
    finding = request.args.get('finding', '')
    view = request.args.get('view', 'findings')
    group_by = request.args.get('group_by', '')
    if group_by == 'none':
        group_by = ''
    page = page_number()
    try:
        report = account_review.read(q=q, finding=finding, view=view, group_by=group_by, page=page)
    except ValueError as exc:
        abort(400, description=str(exc))
    return render_template('admin/identity_account_review.html', report=report,
                           labels=account_review.LABELS, group_modes=account_review.GROUP_MODES,
                           observation_time=timestamp,
                           q=q, finding=finding, view=view, group_by=group_by, page=page)


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
    data = hub.account(stable_id)
    if data is None:
        abort(404)
    f = hub_filters()
    f['q'] = stable_id
    rows, more = hub.audit_rows(f)
    # Keep existing duplicate-review list and direct action links within the summary.
    cases = db.session.execute(base_query().where(or_(Case.left_id == stable_id, Case.right_id == stable_id))
                              .order_by(Case.updated_at.desc()).limit(10)).scalars().all()
    return hub_render('account', rows=rows, more=more, f=f, pages=pagination(f), cases=cases, **data)


@bp.get('/policy')
def policy():
    return hub_render('policy', config=hub.configuration())


@bp.get('/audit')
def audit():
    f = hub_filters()
    rows, more = hub.audit_rows(f)
    return hub_render('audit', rows=rows, more=more, f=f, pages=pagination(f))


@bp.get('/approvals')
def approvals():
    f = hub_filters(status='open')
    rows, more = hub.grants(f)
    return hub_render('approvals', rows=rows, more=more, f=f, pages=pagination(f))


@bp.get('/exemptions')
def exemptions():
    f = hub_filters(status='active')
    rows, more = hub.grants(f, exemptions=True)
    return hub_render('exemptions', rows=rows, more=more, f=f, pages=pagination(f))


def init_app(app):
    app.register_blueprint(bp)

    @app.cli.command('purge-identity-reviews')
    def purge():
        """Remove expired evidence/history and cases closed for 180 days."""
        with review.transaction() as conn:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            result = review.cleanup(conn, review.now())
            result['account_reports'] = account_review.cleanup(conn, review.now())
        click.echo(json.dumps(result))
