"""Campaign review and private remediation support. No startup activation/jobs."""
import click
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError
from auth.decorators import admin_required
from database import db
from services import identity_campaign as campaign

bp = Blueprint('identity_campaign', __name__)


@bp.before_request
def protect():
    if request.path.startswith('/admin/'):
        if session.get('impersonating_from'):
            abort(403)
        denial = admin_required(lambda: None)()
        if denial is not None:
            return denial
        with db.engine.connect() as conn:
            try:
                campaign.changes.administrator(conn, current_user.stable_id)
            except ValueError:
                abort(403)


@bp.after_request
def private(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@bp.route('/admin/identity/email-campaign', methods=['GET', 'POST'])
def overview():
    error, plan, token = None, None, None
    if request.method == 'POST':
        try:
            action = request.form.get('action')
            actor = current_user.stable_id
            if action == 'preview':
                plan, token = campaign.preview(actor)
            elif action == 'activate':
                if request.form.get('confirmed') != 'ACTIVATE':
                    raise ValueError('Type ACTIVATE to start the fixed 30-day clock.')
                campaign.activate(actor, request.form.get('token'))
                flash('Campaign activated. Expiry deletion remains disabled until explicitly enabled.', 'success')
            elif action in ('enable', 'pause'):
                if action == 'enable' and request.form.get('confirmed') != 'DELETE AT EXPIRY':
                    raise ValueError('Type DELETE AT EXPIRY to enable automatic expiry processing.')
                campaign.enable_deletion(actor, request.form.get('campaign'), action == 'enable')
            else:
                raise ValueError('Unknown action.')
            if action != 'preview':
                return redirect(url_for('.overview'), code=303)
        except ValueError as exc:
            error = str(exc)
        except (SQLAlchemyError, OSError):
            error = 'Operation failed safely. Check configuration and retry.'
    try:
        page = max(1, min(100000, int(request.args.get('page', 1))))
    except ValueError:
        abort(400)
    with db.engine.connect() as conn:
        campaigns = conn.execute(select(campaign.C).order_by(campaign.C.c.started_at.desc()).limit(20)).mappings().all()
        rows = conn.execute(select(campaign.M, campaign.C.c.deadline).join(campaign.C,
            campaign.M.c.campaign_id == campaign.C.c.id).order_by(campaign.C.c.started_at.desc(), campaign.M.c.id)
            .offset((page-1)*30).limit(31)).mappings().all()
    return render_template('admin/identity_campaign.html', view='overview', campaigns=campaigns,
        rows=rows[:30], more=len(rows)>30, page=page, plan=plan, token=token, error=error), 409 if error else 200


@bp.route('/admin/identity/email-campaign/<member_id>', methods=['GET', 'POST'])
def detail(member_id):
    error = None
    if request.method == 'POST':
        try:
            campaign.administer(current_user.stable_id, member_id, request.form.get('action'),
                request.form.get('reason'), request.form.get('days'))
            return redirect(url_for('.detail', member_id=member_id), code=303)
        except ValueError as exc:
            error = str(exc)
    with db.engine.connect() as conn:
        row = conn.execute(select(campaign.M, campaign.C.c.deadline).join(campaign.C,
            campaign.M.c.campaign_id == campaign.C.c.id).where(campaign.M.c.id == member_id)).mappings().first()
        if not row:
            abort(404)
        actions = conn.execute(select(campaign.A).where(campaign.A.c.member_id == member_id)
            .order_by(campaign.A.c.id.desc()).limit(100)).mappings().all()
        files = conn.execute(select(campaign.F).where(campaign.F.c.member_id == member_id)).mappings().all()
    return render_template('admin/identity_campaign.html', view='detail', row=row, actions=actions,
        files=files, error=error), 400 if error else 200


@bp.route('/identity/email-campaign/support', methods=['GET', 'POST'])
@login_required
def member():
    row = campaign.pending(current_user.stable_id)
    if not row:
        abort(404)
    error = None
    if request.method == 'POST':
        try:
            campaign.member_action(current_user.stable_id, request.form.get('message'))
            return redirect(url_for('.member'), code=303)
        except ValueError as exc:
            error = str(exc)
    return render_template('identity_campaign_support.html', row=row, error=error), 400 if error else 200


def init_app(app, limiter):
    app.register_blueprint(bp)
    # After admission, before other remediation/profile hooks.
    app.before_request_funcs.setdefault(None, []).insert(1, campaign.guard)
    for endpoint in ('identity_campaign.member', 'identity_campaign.overview', 'identity_campaign.detail'):
        app.view_functions[endpoint] = limiter.limit('20 per hour', methods=['POST'])(app.view_functions[endpoint])

    @app.cli.command('identity-campaign-tick')
    def tick():
        """Send due reminders, process explicitly enabled expiry, retry cleanup."""
        campaign.send_notices()
        click.echo(f'Deleted {campaign.run_due()} eligible accounts.')

    @app.cli.command('purge-identity-campaign')
    def purge():
        """Purge closed campaign records after 180 days; cleanup must finish first."""
        click.echo(f'Purged {campaign.purge()} closed campaign records and {campaign.purge_artifact_backups()} expired artifact backups.')
