"""Serialized signup checks and scoped approvals. Never changes existing login access."""
from contextlib import contextmanager
from datetime import timedelta, timezone
import hashlib
import ipaddress
import json
import secrets
import uuid

from flask import current_app, has_request_context, request, session
from flask_login import current_user
from sqlalchemy import select, insert, update, delete, func, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from werkzeug.security import generate_password_hash

from database import db
from database.models import (User, IdentityDeviceSighting, IdentityEmailReservation,
    IdentitySignupSuccess, IdentitySignupAttempt, IdentitySignupAppeal,
    IdentitySignupAction, IdentityDeletionMarker, IdentityAppealDelivery)
from utils import identity_devices as devices
from utils.identity_rules import normalize_email, validate_new_email, InvalidEmail
from utils.identity_reservations import reserve_new_email, IdentityConflict
from utils.client_ip import get_client_ip

U = User.__table__
D = IdentityDeviceSighting.__table__
S = IdentitySignupSuccess.__table__
T = IdentitySignupAttempt.__table__
P = IdentitySignupAppeal.__table__
A = IdentitySignupAction.__table__
M = IdentityDeletionMarker.__table__
V = IdentityAppealDelivery.__table__
SCOPES = ('device_match', 'fingerprint_match', 'missing_signal', 'ip_cap', 'deleted_return')
SCOPE_LABELS = {'device_match': 'Previously used device', 'fingerprint_match': 'Matching browser details',
    'missing_signal': 'Unavailable browser checks', 'ip_cap': '24-hour network signup limit',
    'deleted_return': 'Return after account deletion'}
MESSAGES = {
    'plus_tag_not_allowed': 'Use an email address without a plus tag.',
    'disposable_domain': 'Use a permanent email address. You can request review if this classification is incorrect.',
    'ip_cap': 'A signup has already been completed on this network within the last 24 hours. Try again later or request review.',
    'missing_signal': 'Enable cookies and JavaScript, refresh this page, and retry. You can also request review.',
    'device_match': 'Registration needs admin review. No account was created.',
    'fingerprint_match': 'Registration needs admin review. No account was created.',
    'deleted_return': 'Registration needs admin review. No account was created.',
    'identity_unavailable': 'This address is unavailable. Try signing in or recovering your account, or request review.',
    'unavailable': 'Registration checks are temporarily unavailable. Please retry later.',
}


def now():
    return devices.now()


def enabled():
    return current_app.config.get('IDENTITY_SIGNUP_ENFORCE', False) is True


def key():
    value = current_app.config.get('IDENTITY_SIGNUP_KEY', '')
    if not isinstance(value, str) or len(value.encode()) < 32:
        raise ValueError('A dedicated IDENTITY_SIGNUP_KEY of at least 32 bytes is required')
    return value


def hashed(purpose, value):
    return devices.digest(key(), purpose, value)


def ip_hash():
    return hashed('signup-ip', ipaddress.ip_address(get_client_ip()).compressed)


def identity_hash(identity):
    return hashed('deleted-email', identity.canonical)


@contextmanager
def transaction():
    with db.engine.begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        # A different marker key must never silently make deletion markers invisible.
        if conn.execute(select(M.c.identity_hash).where(M.c.key_id != devices.key_id(key())).limit(1)).first():
            raise ValueError('Deletion marker key mismatch; operator recovery required')
        yield conn


def audit(conn, action, reason, appeal_id=None, actor=None, at=None):
    at = at or now()
    conn.execute(insert(A).values(appeal_id=appeal_id, actor_id=actor, action=action,
        reason=reason, created_at=at, expires_at=at+timedelta(days=180)))


def visible_appeal(at):
    # Treat an expired grant as closed at expiry even before scheduled cleanup.
    end = func.coalesce(P.c.closed_at, P.c.expires_at)
    return or_(end.is_(None), end > at-timedelta(days=180))


def cleanup(conn, at):
    conn.execute(delete(S).where(S.c.created_at <= at-timedelta(hours=24)))
    conn.execute(delete(V).where(V.c.created_at <= at-timedelta(hours=1)))
    conn.execute(delete(T).where(T.c.expires_at <= at))
    # Expired grants cannot remain open or prevent a fresh request.
    conn.execute(update(P).where(P.c.status.in_(['pending', 'approved']), P.c.expires_at <= at).values(
        status='expired', closed_at=P.c.expires_at, open_key=None, version=P.c.version+1))
    old = select(P.c.id).where(P.c.closed_at <= at-timedelta(days=180))
    conn.execute(delete(A).where(or_(A.c.expires_at <= at, A.c.appeal_id.in_(old))))
    conn.execute(delete(P).where(P.c.id.in_(old)))


class Refused(ValueError):
    def __init__(self, reasons, evidence=None):
        self.reasons = reasons
        self.evidence = evidence or {}
        super().__init__(reasons[0])


def record_attempt(identity, refusal):
    at = now()
    with transaction() as conn:
        cleanup(conn, at)
        for reason in refusal.reasons:
            evidence = refusal.evidence.get(reason, [])
            # Evidence expiration follows the earliest underlying observation.
            expiry = min([at+timedelta(days=180)] + [e['until'] for e in evidence])
            payload = json.dumps([{'account': e['account']} for e in evidence])
            stmt = sqlite_insert(T).values(identity_hash=identity_hash(identity), email=identity.address,
                reason=reason, evidence=payload, first_seen=at, last_seen=at, expires_at=expiry, count=1)
            conn.execute(stmt.on_conflict_do_update(index_elements=['identity_hash', 'reason'],
                set_={'last_seen': at, 'expires_at': expiry, 'evidence': payload, 'count': T.c.count+1}))


def mailbox_proof():
    proof = session.get('identity_signup_proof', {})
    if not isinstance(proof, dict) or not isinstance(proof.get('until'), (int, float)):
        return None
    if proof['until'] <= now().replace(tzinfo=timezone.utc).timestamp():
        return None
    return proof.get('email')


def active_approval(conn, identity, at):
    if mailbox_proof() != identity.address:
        return None
    return conn.execute(select(P).where(P.c.open_key == identity_hash(identity),
        P.c.email == identity.address, P.c.status == 'approved', P.c.expires_at > at,
        P.c.verified_at.is_not(None))).mappings().first()


def admin_creation(conn):
    # Only the authenticated admin route may use this narrowly scoped bypass.
    if request.endpoint != 'admin_create_user' or not current_user.is_authenticated or session.get('impersonating_from'):
        return False
    return bool(conn.execute(select(U.c.id).where(U.c.id == current_user.id,
        U.c.is_admin.is_(True), U.c.is_banned.is_(False))).first())


def evidence(conn, token, fingerprint, at):
    reasons = {}
    for k in devices.keys():
        query = select(D).join(U, U.c.stable_id == D.c.user_stable_id).where(
            D.c.key_id == devices.key_id(k), D.c.last_seen > at-timedelta(days=180),
            D.c.last_seen <= at, D.c.first_seen <= D.c.last_seen)
        for reason, condition in (
            ('device_match', (D.c.device_hash == devices.digest(k, 'device', token)) & D.c.cookie_confirmed.is_(True)),
            ('fingerprint_match', (D.c.fingerprint_hash == devices.digest(k, 'fingerprint-v1', fingerprint)) & (D.c.fingerprint_status == 'ok')),
        ):
            if reason == 'fingerprint_match' and not fingerprint:
                continue
            rows = conn.execute(query.where(condition)).mappings().all()
            if rows:
                reasons.setdefault(reason, []).extend({'account': r['user_stable_id'],
                    'until': r['last_seen']+timedelta(days=180)} for r in rows)
    return reasons


def create_user(email, password, display_name):
    """Return the accepted stored address; all policy writes commit together."""
    try:
        identity = validate_new_email(email)
    except InvalidEmail as exc:
        try:
            record_attempt(normalize_email(email), Refused([exc.code]))
        except InvalidEmail:
            pass
        raise
    if not devices.keys():
        raise ValueError('Device collection must be configured before signup enforcement')
    token, confirmed = devices.device_token()
    fp, status = devices.fingerprint(request.form.get('device_fingerprint'))
    network = ip_hash()
    password_hash = generate_password_hash(password)
    at = now()
    try:
        with transaction() as conn:
            cleanup(conn, at)
            approval = active_approval(conn, identity, at)
            scopes = set(json.loads(approval['scopes'])) if approval else set()
            marker = conn.execute(select(M).where(M.c.identity_hash == identity_hash(identity))).mappings().first()
            reasons = []
            if marker:
                if marker['reason'] != 'deleted' or not approval or approval['marker_generation'] != marker['generation']:
                    reasons.append('deleted_return')
                elif 'deleted_return' not in scopes:
                    reasons.append('deleted_return')
            admin = admin_creation(conn)
            matches = {} if admin else evidence(conn, token, fp, at)
            if not admin:
                if not confirmed or status != 'ok':
                    reasons.append('missing_signal')
                reasons.extend(matches)
                if conn.execute(select(S.c.id).where(S.c.ip_hash == network, S.c.created_at > at-timedelta(hours=24)).limit(1)).first():
                    reasons.append('ip_cap')
            # Tombstone checks above already validate generation and eligibility.
            denied = [r for r in reasons if r == 'deleted_return' or r not in scopes]
            if denied:
                raise Refused(denied, matches)
            if marker:
                # The canonical reservation outlives deletion. Only an approved
                # return releases it; reserve_new_email still checks all live users.
                conn.execute(delete(IdentityEmailReservation).where(IdentityEmailReservation.__table__.c.canonical_email == identity.canonical))
            reserve_new_email(conn, identity.address)
            stable_id = str(uuid.uuid4())
            conn.execute(insert(U).values(id=identity.address, stable_id=stable_id, password_hash=password_hash,
                display_name=display_name, canonical_email=identity.canonical,
                email_normalization_version=identity.version, created_at=at,
                email_verified=False, ip_address=get_client_ip()))
            if not admin:
                conn.execute(insert(S).values(ip_hash=network, created_at=at))
                for k in devices.keys():
                    conn.execute(insert(D).values(user_stable_id=stable_id, key_id=devices.key_id(k),
                        device_hash=devices.digest(k, 'device', token),
                        fingerprint_hash=devices.digest(k, 'fingerprint-v1', fp) if fp else '',
                        fingerprint_status=status, cookie_confirmed=confirmed, first_seen=at, last_seen=at))
            if approval:
                conn.execute(update(P).where(P.c.id == approval['id']).values(status='used', open_key=None,
                    closed_at=at, version=P.c.version+1))
                conn.execute(update(A).where(A.c.appeal_id == approval['id']).values(expires_at=at+timedelta(days=180)))
                audit(conn, 'used', 'Approval consumed by successful registration.', approval['id'], at=at)
            return identity.address
    except IdentityConflict:
        refusal = Refused(['identity_unavailable'])
        record_attempt(identity, refusal)
        raise refusal from None
    except Refused as refusal:
        record_attempt(identity, refusal)
        raise


def mark_deleted(orm_session, user):
    """Participates in the existing ORM deletion transaction; no independent commit."""
    if not enabled():
        return
    try:
        marker_hash = identity_hash(normalize_email(user.id))
    except InvalidEmail:
        # Structurally invalid legacy addresses cannot register, but their
        # deletion must remain possible. Do not retain the invalid raw address.
        marker_hash = hashed('deleted-invalid-email', user.id.strip().lower())
    conn = orm_session.connection()
    if conn.execute(select(M.c.identity_hash).where(M.c.key_id != devices.key_id(key())).limit(1)).first():
        raise ValueError('Deletion marker key mismatch')
    values = dict(identity_hash=marker_hash, key_id=devices.key_id(key()),
        reason='banned' if user.is_banned else 'deleted', generation=str(uuid.uuid4()), created_at=now())
    conn.execute(sqlite_insert(M).values(**values).on_conflict_do_update(index_elements=['identity_hash'], set_=values))
    # A new deletion invalidates any unused permission tied to earlier history.
    conn.execute(update(P).where(P.c.open_key == marker_hash).values(
        status='revoked', closed_at=now(), open_key=None, version=P.c.version+1))
    audit(conn, 'deletion_marker', 'Recorded '+values['reason']+' identity marker.',
          actor=current_user.stable_id if has_request_context() and current_user.is_authenticated else None)


def request_mailbox(email, message):
    """Issue a one-hour single-use verification link; rate limit survives restart."""
    identity = normalize_email(email)  # Disposable classification itself may be appealed.
    message = (message or '').strip()
    if not 1 <= len(message) <= 2000:
        raise ValueError('Explain your request in 1–2000 characters.')
    at = now()
    raw = secrets.token_urlsafe(32)
    with transaction() as conn:
        cleanup(conn, at)
        network = ip_hash()
        count = conn.execute(select(func.count()).select_from(V).where(V.c.ip_hash == network,
            V.c.created_at > at-timedelta(hours=1))).scalar_one()
        if count >= 5:
            raise ValueError('Too many requests from this network. Try again in an hour.')
        row = conn.execute(select(P).where(P.c.open_key == identity_hash(identity))).mappings().first()
        if not row:
            conn.execute(insert(P).values(email=identity.address, identity_hash=identity_hash(identity),
                open_key=identity_hash(identity), message=message, status='pending', created_at=at,
                expires_at=at+timedelta(hours=1)))
        # Do not overwrite a verified request or rotate somebody else's token.
        conn.execute(insert(V).values(email=identity.address, message=message, token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            ip_hash=network, created_at=at, used=False))
    return identity.address, raw


def verify_mailbox(raw):
    if not isinstance(raw, str) or not 20 <= len(raw) <= 100:
        raise ValueError('Verification link is invalid or expired.')
    at = now()
    with transaction() as conn:
        cleanup(conn, at)
        row = conn.execute(select(V).where(V.c.token_hash == hashlib.sha256(raw.encode()).hexdigest(),
            V.c.used.is_(False), V.c.created_at > at-timedelta(hours=1))).mappings().first()
        if not row:
            raise ValueError('Verification link is invalid or expired.')
        conn.execute(update(V).where(V.c.id == row['id']).values(used=True))
        ih = identity_hash(normalize_email(row['email']))
        appeal = conn.execute(select(P).where(P.c.open_key == ih)).mappings().first()
        if not appeal:
            result = conn.execute(insert(P).values(email=row['email'], identity_hash=ih, open_key=ih,
                status='open', message=row['message'], created_at=at, verified_at=at))
            audit(conn, 'verified', 'Applicant verified their mailbox.', result.inserted_primary_key[0], at=at)
        elif appeal['email'] == row['email'] and appeal['status'] == 'pending':
            conn.execute(update(P).where(P.c.id == appeal['id']).values(status='open', message=row['message'],
                verified_at=at, expires_at=None, version=P.c.version+1))
            audit(conn, 'verified', 'Applicant verified their mailbox.', appeal['id'], at=at)
    session['identity_signup_proof'] = {'email': row['email'], 'until': (at+timedelta(minutes=30)).replace(tzinfo=timezone.utc).timestamp()}
    return row['email']


def decide(appeal_id, actor, version, action, reason, scopes):
    reason = (reason or '').strip()
    scopes = sorted(set(scopes))
    if not 1 <= len(reason) <= 2000 or action not in ('approve', 'reject', 'revoke'):
        raise ValueError('Choose an action and provide a reason (up to 2000 characters).')
    if not set(scopes).issubset(SCOPES) or (action == 'approve' and not scopes):
        raise ValueError('Select the specific exceptions to approve.')
    at = now()
    with transaction() as conn:
        cleanup(conn, at)
        admin = conn.execute(select(U).where(U.c.stable_id == actor, U.c.is_admin.is_(True), U.c.is_banned.is_(False))).first()
        if not admin:
            raise ValueError('An active admin is required.')
        row = conn.execute(select(P).where(P.c.id == appeal_id)).mappings().first()
        if not row or row['version'] != version or row['status'] not in ('open', 'approved'):
            raise ValueError('Request changed or is no longer actionable. Reload it.')
        if (action == 'revoke') != (row['status'] == 'approved'):
            raise ValueError('Only unused approvals may be revoked; only open requests may be decided.')
        marker = conn.execute(select(M).where(M.c.identity_hash == row['identity_hash'])).mappings().first()
        if action == 'approve' and 'deleted_return' in scopes and (not marker or marker['reason'] != 'deleted'):
            raise ValueError('Only a deleted identity is eligible for a return approval.')
        status = {'approve': 'approved', 'reject': 'rejected', 'revoke': 'revoked'}[action]
        conn.execute(update(P).where(P.c.id == appeal_id).values(status=status, version=version+1,
            scopes=json.dumps(scopes if action == 'approve' else []),
            marker_generation=marker['generation'] if marker else None,
            expires_at=at+timedelta(days=7) if action == 'approve' else None,
            closed_at=None if action == 'approve' else at,
            open_key=row['identity_hash'] if action == 'approve' else None))
        expiry = at+timedelta(days=187 if action == 'approve' else 180)
        conn.execute(update(A).where(A.c.appeal_id == appeal_id).values(expires_at=expiry))
        audit(conn, action, reason, appeal_id, actor, at)
        conn.execute(update(A).where(A.c.appeal_id == appeal_id).values(expires_at=expiry))
