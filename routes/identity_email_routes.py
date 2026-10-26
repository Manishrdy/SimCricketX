"""Private admin email changes and mailbox-only confirmation."""
import json
import os
from pathlib import Path

import click
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from auth.decorators import admin_required
from database import db
from services import identity_email_change as changes

bp = Blueprint('identity_email', __name__)


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


@bp.route('/admin/identity/email-changes', methods=['GET', 'POST'])
def overview():
    error = None
    if request.method == 'POST':
        try:
            recovery = request.form.get('recovery_evidence', '') if request.form.get('recovery') == 'yes' else ''
            if request.form.get('recovery') == 'yes' and not recovery.strip():
                raise ValueError('Document ownership evidence for the recovery exception.')
            change_id = changes.create(current_user.stable_id, request.form.get('old_email', '').strip().lower(),
                request.form.get('new_email', ''), request.form.get('reason', ''), recovery)
            return redirect(url_for('.detail', change_id=change_id), code=303)
        except ValueError as exc:
            error = str(exc)
        except SQLAlchemyError:
            error = 'Could not create this request. Reload and retry.'
    try:
        page = max(1, min(100000, int(request.args.get('page', 1))))
    except ValueError:
        abort(400)
    rows = db.session.execute(select(changes.C).order_by(changes.C.c.created_at.desc()).offset((page-1)*30).limit(31)).mappings().all()
    return render_template('admin/identity_email.html', rows=rows[:30], page=page, more=len(rows)>30,
        error=error, view='overview', old_email=request.args.get('email', ''), at=changes.now()), 400 if error else 200


@bp.route('/admin/identity/email-changes/<change_id>', methods=['GET', 'POST'])
def detail(change_id):
    error, plan, token = None, None, None
    if request.method == 'POST':
        try:
            action = request.form.get('action')
            if action == 'cancel':
                changes.cancel(current_user.stable_id, change_id)
            elif action == 'preview':
                plan, token = changes.preview(current_user.stable_id, change_id)
            elif action == 'apply':
                if request.form.get('confirmed') != 'yes':
                    raise ValueError('Confirm that you reviewed this impact preview.')
                changes.execute(current_user.stable_id, change_id, request.form.get('token'), request.form.get('confirmed_email'))
                flash('Email changed. Sessions and outstanding security tokens were revoked.', 'success')
            elif action == 'notify':
                changes.notify_completion(change_id)
            else:
                raise ValueError('Unknown action.')
            if action != 'preview':
                return redirect(url_for('.detail', change_id=change_id), code=303)
        except ValueError as exc:
            error = str(exc)
        except (SQLAlchemyError, OSError, RuntimeError):
            error = 'Operation failed safely. Check backup configuration and reload before retrying.'
    with db.engine.connect() as conn:
        try:
            row = changes.get_change(conn, change_id, pending=False)
        except ValueError:
            abort(404)
    receipt = json.loads(row['receipt_json']) if row['receipt_json'] else None
    return render_template('admin/identity_email.html', view='detail', row=row, plan=plan, token=token,
        receipt=receipt, error=error, at=changes.now()), 409 if error else 200


@bp.route('/identity/email-change/verify/<change_id>/<side>/<token>', methods=['GET', 'POST'])
def verify(change_id, side, token):
    # GET deliberately neither consumes nor validates a token against stored data.
    if side not in ('old', 'new') or len(token) > 100:
        abort(404)
    error, done, completed = None, False, False
    if request.method == 'POST':
        try:
            changes.verify(change_id, side, token)
            done = True
            completed = changes.complete_mandatory(change_id)
        except ValueError as exc:
            error = str(exc)
        except (SQLAlchemyError, OSError):
            error = 'Mailbox confirmation or account update could not finish. Contact support or retry mandatory correction.'
    return render_template('identity_email_verify.html', done=done, completed=completed, error=error), 400 if error else 200


def init_app(app, limiter):
    app.config.setdefault('IDENTITY_EMAIL_BACKUP_DIR', os.environ.get('IDENTITY_EMAIL_BACKUP_DIR', str(Path(app.instance_path)/'identity-email-backups')))
    app.config.setdefault('IDENTITY_EMAIL_MATCH_DIR', str(Path(app.root_path)/'data'/'matches'))
    app.register_blueprint(bp)
    # Admission must precede every hook that could write or cache an identity.
    app.before_request_funcs.setdefault(None, []).insert(0, changes.enter_request)
    app.teardown_request(changes.leave_request)
    for endpoint in ('identity_email.overview', 'identity_email.verify'):
        app.view_functions[endpoint] = limiter.limit('10 per hour', methods=['POST'])(app.view_functions[endpoint])

    @app.cli.command('purge-email-change-backups')
    def purge():
        """Remove only this feature's operation backups older than 30 days."""
        click.echo(f'Removed {changes.purge_backups()} expired operation backups.')

    @app.cli.command('retry-email-change-notices')
    def retry():
        """Retry unsent completion notices; delivery is at least once."""
        with db.engine.connect() as conn:
            ids = conn.execute(select(changes.C.c.id).where(changes.C.c.status == 'completed',
                (~changes.C.c.old_notice_sent) | (~changes.C.c.new_notice_sent))).scalars().all()
        for change_id in ids:
            changes.notify_completion(change_id)
        click.echo(f'Checked {len(ids)} completed changes.')
