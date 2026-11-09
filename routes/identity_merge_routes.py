"""Admin-only reviewed merge workflow and explicit mailbox confirmation."""
import json
from flask import Blueprint, abort, render_template, request, redirect, url_for, session
from flask_login import current_user
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from auth.decorators import admin_required
from database import db
from services import identity_merge as merges

bp = Blueprint('identity_merge', __name__)


@bp.before_request
def protect():
    if request.path.startswith('/admin/'):
        if session.get('impersonating_from'):
            abort(403)
        return admin_required(lambda: None)()


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@bp.route('/admin/identity/merges', methods=['GET', 'POST'])
def overview():
    error = None
    if request.method == 'POST':
        try:
            if request.form.get('ownership') != 'yes':
                raise ValueError('Confirm established common ownership; signals alone are insufficient.')
            mid = merges.create(current_user.stable_id, int(request.form.get('case_key', '')),
                request.form.get('source', '').strip().lower(), request.form.get('survivor', '').strip().lower(),
                request.form.get('reason', ''), request.form.get('recovery', ''))
            return redirect(url_for('.detail', mid=mid), code=303)
        except ValueError as exc:
            error = str(exc)
        except (SQLAlchemyError, OSError):
            error = 'Request could not finish. Reload to check its status before retrying.'
    try:
        page = max(1, min(100000, int(request.args.get('page', 1))))
    except ValueError:
        abort(400)
    with db.engine.connect() as conn:
        rows = conn.execute(select(merges.M).order_by(merges.M.c.created_at.desc()).offset((page-1)*30).limit(31)).mappings().all()
    return render_template('admin/identity_merge.html', view='overview', rows=rows[:30], page=page, more=len(rows)>30,
        error=error, at=merges.base.now()), 400 if error else 200


@bp.route('/admin/identity/merges/<mid>', methods=['GET', 'POST'])
def detail(mid):
    error, plan, token = None, None, None
    if request.method == 'POST':
        try:
            action = request.form.get('action')
            if action == 'preview':
                plan, token = merges.preview(current_user.stable_id, mid)
            elif action == 'cancel':
                merges.cancel(current_user.stable_id, mid)
            elif action == 'apply':
                if request.form.get('confirmed') != 'yes':
                    raise ValueError('Confirm you reviewed ownership, private access and all conflicts/losses.')
                merges.execute(current_user.stable_id, mid, request.form.get('token'), request.form.get('confirmed_email'))
            else:
                raise ValueError('Unknown action.')
            if action != 'preview':
                return redirect(url_for('.detail', mid=mid), code=303)
        except ValueError as exc:
            error = str(exc)
        except (SQLAlchemyError, OSError, RuntimeError):
            error = 'Merge did not finish. Check the receipt and backup configuration before retrying.'
    with db.engine.connect() as conn:
        try:
            row = merges.get(conn, mid, pending=False)
        except ValueError:
            abort(404)
    return render_template('admin/identity_merge.html', view='detail', row=row, plan=plan, token=token, error=error,
        receipt=json.loads(row['receipt_json']) if row['receipt_json'] else None, at=merges.base.now()), 409 if error else 200


@bp.route('/identity/merge/verify/<mid>/<side>/<token>', methods=['GET', 'POST'])
def verify(mid, side, token):
    if side not in ('source', 'survivor') or len(token)>100:
        abort(404)
    error, done = None, False
    if request.method == 'POST':
        try:
            merges.verify(mid, side, token)
            done = True
        except ValueError as exc:
            error = str(exc)
        except SQLAlchemyError:
            error = 'Confirmation could not finish. Reload and retry.'
    return render_template('identity_merge_verify.html', done=done, error=error), 400 if error else 200


def init_app(app, limiter):
    app.register_blueprint(bp)
    for endpoint in ('identity_merge.overview', 'identity_merge.verify'):
        app.view_functions[endpoint] = limiter.limit('10 per hour', methods=['POST'])(app.view_functions[endpoint])

    @app.cli.command('purge-merge-requests')
    def purge():
        """Invalidate expired merge proofs; retain durable merge receipts."""
        import click
        with merges.base.transaction() as conn:
            merges.expire(conn)
        click.echo('Expired merge requests closed. Use purge-email-change-backups for 30-day operation backups.')
