"""Network-only restriction, account-bound exceptions and private Community appeals."""
from contextlib import contextmanager
from datetime import timedelta
import json

from flask import current_app, g, jsonify, redirect, request, session, url_for
from flask_login import current_user
from sqlalchemy import select, insert, update, delete, or_, func
from sqlalchemy.exc import SQLAlchemyError

from database import db
from database.models import User, ActiveSession, IdentityNetworkAppeal, IdentityNetworkAction, IdentityNetworkEvent
from utils import network_classifier as classifier
from utils.client_ip import get_client_ip

U = User.__table__
S = ActiveSession.__table__
P = IdentityNetworkAppeal.__table__
A = IdentityNetworkAction.__table__
E = IdentityNetworkEvent.__table__

# Every endpoint is named explicitly; a future /community/* route is not
# automatically allowed. Existing permissions still run inside these endpoints.
COMMUNITY = frozenset({
    'community_index', 'community_new', 'community_post', 'community_edit', 'community_notifications_page',
    'community_api_create', 'community_api_update', 'community_api_delete', 'community_api_vote',
    'community_api_search', 'community_api_mention_suggest', 'community_api_similar', 'community_api_comment',
    'community_api_edit_comment', 'community_api_delete_comment', 'community_api_report',
    'community_api_notifications', 'community_api_notifications_read', 'community_api_upload',
    'community_api_delete_image', 'community_media', 'identity_network.appeal',
})
SUPPORT = frozenset({
    'static', 'logout', 'login', 'auth_challenge', 'force_change_password', 'set_display_name',
    'verify_email', 'verify_email_pending', 'resend_verification', 'forgot_password', 'reset_password',
    'force_verify_email', 'force_verify_email_send', 'force_verify_email_change',
    'identity_device_privacy', 'identity_device_collect', 'identity_network.privacy',
    'admin_stop_impersonation',
})
ALLOWED = COMMUNITY | SUPPORT
# Returning from impersonation must remain possible when the target is banned
# or requires remediation; the existing route validates the original admin.
PUBLIC = frozenset({'static', 'login', 'logout', 'auth_challenge', 'register', 'admin_stop_impersonation',
    'verify_email', 'verify_email_pending', 'resend_verification', 'forgot_password', 'reset_password',
    'identity_device_privacy', 'identity_network.privacy',
    'identity_signup.appeal', 'identity_signup.verify', 'identity_signup.status'})


def enabled():
    return current_app.config.get('IDENTITY_NETWORK_ENFORCE') is True


def now():
    return classifier.now()


def visible(at):
    end = func.coalesce(P.c.closed_at, P.c.expires_at)
    return or_(end.is_(None), end > at-timedelta(days=180))


@contextmanager
def transaction():
    with db.engine.begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        yield conn


def cleanup(conn, at):
    classifier.cleanup(conn, at)
    conn.execute(update(P).where(P.c.status == 'approved', P.c.expires_at <= at).values(
        status='expired', closed_at=P.c.expires_at, open_key=None, version=P.c.version+1))
    old = select(P.c.id).where(P.c.closed_at <= at-timedelta(days=180))
    conn.execute(delete(A).where(or_(A.c.appeal_id.in_(old), A.c.expires_at <= at)))
    conn.execute(delete(P).where(P.c.id.in_(old)))
    conn.execute(delete(E).where(E.c.expires_at <= at))


def audit(conn, appeal_id, actor, action, reason, at, expiry=None):
    conn.execute(insert(A).values(appeal_id=appeal_id, actor_id=actor, action=action, reason=reason,
        created_at=at, expires_at=expiry or at+timedelta(days=180)))


def account_block(row, at):
    if row['is_banned'] and (row['banned_until'] is None or row['banned_until'] > at):
        return 'banned'
    if row['force_email_verify']:
        return 'force_verify'
    if not row['email_verified']:
        return 'unverified'
    if row['force_password_reset']:
        return 'force_password'
    return None


def evaluate(*, force=False):
    """Revalidate auth independently of classification; never trust cookie verdicts."""
    cache_key = 'scx.network.decision'
    if not force and cache_key in request.environ:
        return request.environ[cache_key]
    result = {'state': 'allowed', 'categories': (), 'classification': None}
    if not enabled():
        return result
    if not current_user.is_authenticated:
        return {**result, 'state': 'auth_required'}
    at = now()
    try:
        with db.engine.connect() as conn:
            row = conn.execute(select(U).where(U.c.id == current_user.id)).mappings().first()
            active = conn.execute(select(S).where(S.c.user_id == current_user.id,
                S.c.session_token == session.get('session_token', ''))).mappings().first()
            if not row or not active or active['last_active'] <= at-timedelta(minutes=current_app.config.get('SESSION_INACTIVITY_MINUTES', 120)):
                return {**result, 'state': 'auth_required'}
            block = account_block(row, at)
            if block:
                return {**result, 'state': block}
            if row['is_admin'] and not session.get('impersonating_from'):
                return result
            approved = conn.execute(select(P.c.id).where(P.c.user_stable_id == row['stable_id'],
                P.c.status == 'approved', P.c.expires_at > at)).first()
            if approved:
                return result
    except SQLAlchemyError:
        return {**result, 'state': 'auth_unavailable'}
    classification = classifier.classify(get_client_ip())
    result.update(state='restricted' if classification.state == 'flagged' else 'allowed',
                  categories=classification.categories, classification=classification)
    request.environ[cache_key] = result
    # Only audit changed classifications, not every asset/poll. This signed
    # cookie marker is a logging optimization and is never an access decision.
    marker = [row['stable_id'], classification.state, list(classification.categories)]
    if session.get('identity_network_observed') != marker:
        try:
            with db.engine.begin() as conn:
                conn.execute(insert(E).values(user_stable_id=row['stable_id'], state=classification.state,
                    categories=json.dumps(classification.categories), created_at=at, expires_at=at+timedelta(days=180)))
            session['identity_network_observed'] = marker
        except SQLAlchemyError:
            current_app.logger.warning('Network transition audit unavailable')
    return result


def guard():
    g.network_restricted = False
    if not enabled() or request.endpoint in PUBLIC or not current_user.is_authenticated:
        return None
    decision = evaluate()
    state = decision['state']
    json_request = request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.best == 'application/json'
    if state == 'restricted':
        g.network_restricted = True
        if request.endpoint in ALLOWED:
            return None
        if json_request or request.method not in ('GET', 'HEAD'):
            return jsonify(error='Network access is restricted to Community. Turn off your VPN/proxy or request review.',
                           code='network_restricted', review_url=url_for('identity_network.appeal')), 403
        return redirect(url_for('community_index'), code=303)
    if state == 'allowed':
        return None
    if state in ('force_verify', 'unverified'):
        allowed = {'force_verify_email', 'force_verify_email_send', 'force_verify_email_change'} if state == 'force_verify' else set()
        if request.endpoint in allowed:
            return None
        if not json_request:
            return redirect(url_for('force_verify_email' if state == 'force_verify' else 'verify_email_pending'))
    if state == 'force_password':
        if request.endpoint == 'force_change_password':
            return None
        if not json_request:
            return redirect(url_for('force_change_password'))
    if state == 'auth_required' and not json_request:
        return redirect(url_for('login'))
    return jsonify(error='Your account or session does not permit this action.', code=state), 503 if state == 'auth_unavailable' else (401 if state == 'auth_required' else 403)


def socket_error():
    """Socket events do not execute Flask before_request hooks."""
    if not enabled():
        return None
    state = evaluate(force=True)['state']
    if state != 'allowed':
        return {'message': 'Network access is restricted to Community.' if state == 'restricted' else 'Account or session restriction.',
                'code': 'network_restricted' if state == 'restricted' else state, 'status': 403}
    return None


def signup_block():
    """Public registration only; invoked before any usable account is written."""
    if not enabled():
        return False
    return classifier.classify(get_client_ip()).state == 'flagged'


def submit_appeal(user_id, message, categories):
    message = (message or '').strip()
    if not 10 <= len(message) <= 2000:
        raise ValueError('Explain your request in 10–2000 characters. Do not include passwords.')
    at = now()
    with transaction() as conn:
        cleanup(conn, at)
        user = conn.execute(select(U).where(U.c.stable_id == user_id)).mappings().first()
        if not user or account_block(user, at):
            raise ValueError('Verify your account and resolve other account restrictions first.')
        existing = conn.execute(select(P.c.id).where(P.c.open_key == user_id)).scalar_one_or_none()
        if existing:
            return existing
        # Persisted cap applies even if the in-memory HTTP limiter is restarted.
        count = conn.execute(select(func.count()).select_from(P).where(P.c.user_stable_id == user_id,
            P.c.created_at > at-timedelta(hours=1))).scalar_one()
        if count >= 5:
            raise ValueError('Too many review requests. Try again in an hour.')
        result = conn.execute(insert(P).values(user_stable_id=user_id, open_key=user_id, status='open',
            message=message, categories=json.dumps(categories), created_at=at))
        appeal_id = result.inserted_primary_key[0]
        audit(conn, appeal_id, user_id, 'submitted', 'Private network appeal submitted.', at)
        return appeal_id


def decide(appeal_id, actor_id, version, action, reason):
    reason = (reason or '').strip()
    if not 1 <= len(reason) <= 2000 or action not in ('approve', 'reject', 'revoke'):
        raise ValueError('Choose a decision and provide its reason (up to 2000 characters).')
    at = now()
    with transaction() as conn:
        cleanup(conn, at)
        admin = conn.execute(select(U).where(U.c.stable_id == actor_id, U.c.is_admin.is_(True))).mappings().first()
        if not admin or account_block(admin, at):
            raise ValueError('An unrestricted verified admin is required.')
        appeal = conn.execute(select(P).where(P.c.id == appeal_id, visible(at))).mappings().first()
        if not appeal or appeal['version'] != version or appeal['status'] not in ('open', 'approved'):
            raise ValueError('This request changed or is no longer actionable. Reload it.')
        if (action == 'revoke') != (appeal['status'] == 'approved'):
            raise ValueError('Only unused active exceptions may be revoked; only open requests may be approved or rejected.')
        user = conn.execute(select(U).where(U.c.stable_id == appeal['user_stable_id'])).mappings().first()
        if action == 'approve' and (not user or account_block(user, at)):
            raise ValueError('An exception cannot bypass other account restrictions.')
        status = {'approve': 'approved', 'reject': 'rejected', 'revoke': 'revoked'}[action]
        expiry = at+timedelta(days=7) if action == 'approve' else None
        conn.execute(update(P).where(P.c.id == appeal_id).values(status=status, expires_at=expiry,
            open_key=appeal['user_stable_id'] if expiry else None, closed_at=None if expiry else at, version=version+1))
        history_expiry = (expiry or at)+timedelta(days=180)
        conn.execute(update(A).where(A.c.appeal_id == appeal_id).values(expires_at=history_expiry))
        audit(conn, appeal_id, actor_id, action, reason, at, history_expiry)
