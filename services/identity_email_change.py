"""Verified identity repointing. Never call the legacy immediate email writer.

Current ownership links move; historical actor/address snapshots do not. SQLite
serializes the plan, backup and updates; an inter-process admission lock excludes
HTTP/socket work while the operation commits. No external match files are edited.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timedelta
from pathlib import Path
import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import uuid

from flask import current_app, g, jsonify, request
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import select, update, delete, insert, inspect

from database import db
from database.models import User, IdentityEmailChange, ActiveSession, IdentityEmailReservation
from utils.identity_rules import validate_new_email, normalize_email, InvalidEmail

U = User.__table__
C = IdentityEmailChange.__table__
# Explicit coverage: adding a user FK requires a conscious classification.
OWNERSHIP = frozenset({
    ('stadiums', 'owner_user_id'), ('stadium_adoptions', 'user_id'),
    ('teams', 'user_id'), ('user_players', 'user_id'), ('leagues', 'user_id'),
    ('matches', 'user_id'), ('tours', 'user_id'), ('tournaments', 'user_id'),
    ('announcement_banner', 'updated_by'), ('user_banner_dismissals', 'user_id'),
    ('login_history', 'user_id'), ('user_ground_configs', 'user_id'),
    ('support_prompt_events', 'user_id'), ('auth_event_log', 'user_id'),
    ('community_posts', 'author_id'), ('community_comments', 'author_id'),
    ('community_votes', 'user_id'), ('community_images', 'uploader_id'),
    ('community_notifications', 'user_id'), ('community_mentions', 'user_id'),
    ('community_mentions', 'author_id'), ('community_reports', 'reporter_id'),
})
NON_FK_CURRENT = frozenset({('fixture_replays', 'user_id')})
HISTORICAL = frozenset({('failed_login_attempts', 'email'), ('auth_event_log', 'email'),
    ('exception_log', 'user_email'), ('blocked_ips', 'blocked_by'),
    ('auction_audit_logs', 'actor_label')})
REVOKE = frozenset({('active_sessions', 'user_id')})

# Non-FK address/actor columns intentionally retained, including historical
# appeal identities and stable UUID actors. Never silently rewrite snapshots.
NON_FK_PRESERVED = HISTORICAL | frozenset({
    ('identity_review_actions', 'actor_id'), ('identity_signup_actions', 'actor_id'),
    ('identity_network_actions', 'actor_id'), ('identity_signup_attempts', 'email'),
    ('identity_signup_appeals', 'email'), ('identity_appeal_deliveries', 'email'),
    ('identity_email_changes', 'old_email'), ('identity_email_changes', 'new_email'),
    ('identity_email_changes', 'actor_id'),
})
NON_FK_MANAGED = frozenset({('users', 'canonical_email'), ('users', 'pending_email'),
    ('identity_email_reservations', 'canonical_email')})


def now():
    return datetime.utcnow()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='identity-email-preview-v1')


def directory():
    path = Path(current_app.config['IDENTITY_EMAIL_BACKUP_DIR'])
    if path.is_symlink():
        raise ValueError('Operation backup directory cannot be a symlink.')
    path = path.resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('Operation backup directory must be private (mode 0700).')
    return path


@contextmanager
def admission(exclusive=False):
    # A database-specific lock shared by every worker on this SQLite host.
    filename = str(Path(db.engine.url.database).resolve())
    path = Path(current_app.instance_path) / ('identity-' + digest(filename)[:20] + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Account activity is in progress. Retry the email change shortly.') from None
        yield
    finally:
        os.close(fd)


def enter_request():
    if request.endpoint and request.endpoint.startswith('identity_email.'):
        return None
    guard = admission()
    try:
        guard.__enter__()
    except ValueError:
        return jsonify(error='An identity update is in progress. Please retry.'), 409
    g.identity_admission = guard


def leave_request(error=None):
    guard = g.pop('identity_admission', None)
    if guard:
        guard.__exit__(None, None, None)


@contextmanager
def transaction():
    with db.engine.begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        yield conn


def administrator(conn, actor):
    row = conn.execute(select(U).where(U.c.stable_id == actor)).mappings().first()
    if not row or not row['is_admin'] or row['is_banned']:
        raise ValueError('An active administrator is required.')


def coverage(conn):
    actual = set()
    inspector = inspect(conn)
    for name in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(name):
            if fk['referred_table'] == 'users' and fk['referred_columns'] == ['id']:
                actual.update((name, col) for col in fk['constrained_columns'])
    candidates = set()
    names = {'user_id', 'email', 'user_email', 'actor_label', 'blocked_by', 'owner_id', 'author_id', 'actor_id'}
    for table in db.metadata.tables.values():
        for column in table.c:
            if not column.foreign_keys and (column.name in names or column.name.endswith('_email')):
                candidates.add((table.name, column.name))
    if candidates != NON_FK_CURRENT | NON_FK_PRESERVED | NON_FK_MANAGED:
        raise ValueError('Non-FK account reference coverage changed; operator review is required.')
    if actual != OWNERSHIP | REVOKE:
        raise ValueError('Account reference coverage changed; operator review is required.')


def available(conn, address, old):
    identity = validate_new_email(address)
    if identity.address == old:
        raise ValueError('Choose a different email address.')
    for email in conn.execute(select(U.c.id)).scalars():
        if email == old:
            continue
        try:
            if normalize_email(email).canonical == identity.canonical:
                raise ValueError('The requested email requires identity review.')
        except InvalidEmail:
            continue
    previous = None
    try:
        previous = normalize_email(old).canonical
    except InvalidEmail:
        pass
    R = IdentityEmailReservation.__table__
    if identity.canonical != previous and conn.execute(select(R).where(R.c.canonical_email == identity.canonical)).first():
        raise ValueError('The requested email is reserved and requires identity review.')
    from services import identity_signup as signup
    if conn.execute(select(signup.M).limit(1)).first():
        key = signup.key()  # Fail closed for missing/rotated marker keys, even with signup off.
        if conn.execute(select(signup.M).where(signup.M.c.key_id != signup.devices.key_id(key)).limit(1)).first():
            raise ValueError('Identity marker key needs operator recovery.')
        if conn.execute(select(signup.M).where(signup.M.c.identity_hash == signup.identity_hash(identity))).first():
            raise ValueError('The requested email requires identity review.')
    return identity


def get_change(conn, change_id, pending=True):
    row = conn.execute(select(C).where(C.c.id == change_id)).mappings().first()
    if not row:
        raise ValueError('Email-change request not found.')
    if pending and (row['status'] != 'pending' or row['expires_at'] <= now()):
        raise ValueError('This request is closed or expired. Start a new request.')
    return row


def target(conn, change):
    row = conn.execute(select(U).where(U.c.stable_id == change['user_stable_id'])).mappings().first()
    if not row or row['id'] != change['old_email'] or row['is_admin']:
        raise ValueError('Account identity or role changed. Start a new request.')
    return row


def create(actor, old, new, reason, recovery='', *, self_service=False):
    reason, recovery = (reason or '').strip(), (recovery or '').strip()
    if not 10 <= len(reason) <= 2000 or (recovery and not 20 <= len(recovery) <= 2000):
        raise ValueError('Provide a reason (10–2000 characters) and, for recovery, ownership evidence (20–2000 characters).')
    change_id = str(uuid.uuid4())
    at = now()
    tokens = {'old': secrets.token_urlsafe(32), 'new': secrets.token_urlsafe(32)}
    with transaction() as conn:
        if not self_service:
            administrator(conn, actor)
        user = conn.execute(select(U).where(U.c.id == old)).mappings().first()
        if not user or user['is_admin'] or not user['stable_id']:
            raise ValueError('Select a non-admin account with a stable identity.')
        if self_service and (user['stable_id'] != actor or not user['force_email_verify']):
            raise ValueError('Mandatory correction is not required for this account.')
        identity = available(conn, new, old)
        conn.execute(update(C).where(C.c.status == 'pending', C.c.expires_at <= at).values(
            status='expired', open_key=None, old_token_hash=None, new_token_hash=None))
        # Retrying mandatory correction replaces only that member's own request.
        if self_service:
            conn.execute(update(C).where(C.c.open_key == user['stable_id'], C.c.self_service.is_(True)).values(
                status='cancelled', open_key=None, old_token_hash=None, new_token_hash=None))
        if conn.execute(select(C.c.id).where(C.c.open_key == user['stable_id'])).first():
            raise ValueError('An open email-change request already exists for this account.')
        conn.execute(insert(C).values(id=change_id, user_stable_id=user['stable_id'], open_key=user['stable_id'],
            old_email=old, new_email=identity.address, actor_id=actor, reason=reason, recovery_evidence=recovery, self_service=self_service,
            created_at=at, expires_at=at+timedelta(hours=24), old_token_hash=None if recovery else digest(tokens['old']),
            new_token_hash=digest(tokens['new']), status='pending', version=1))
    deliver_proofs(change_id, tokens)
    return change_id


def deliver_proofs(change_id, tokens):
    from markupsafe import escape
    from utils import email_service as mail
    with db.engine.connect() as conn:
        row = get_change(conn, change_id)
    outcomes = []
    for side in ('old', 'new'):
        if side == 'old' and row['recovery_evidence']:
            continue
        url = mail._app_url().rstrip('/') + '/identity/email-change/verify/' + change_id + '/' + side + '/' + tokens[side]
        try:
            ok = mail.send_email(row[side+'_email'], 'Confirm your SimCricketX email change',
                '<p>An email change was requested for your account. Only confirm if you requested this change.</p>'
                + '<p><a href="' + str(escape(url)) + '">Review and confirm</a></p><p>This link expires in 24 hours.</p>')
        except Exception:
            ok = False
        outcomes.append(bool(ok))
    with transaction() as conn:
        conn.execute(update(C).where(C.c.id == change_id).values(proof_delivery='sent' if all(outcomes) else 'failed'))


def verify(change_id, side, token):
    if side not in ('old', 'new') or not isinstance(token, str) or len(token) > 100:
        raise ValueError('Invalid verification link.')
    with transaction() as conn:
        row = get_change(conn, change_id)
        target(conn, row)
        expected = row[side+'_token_hash']
        if not expected or not secrets.compare_digest(expected, digest(token)):
            raise ValueError('Invalid or already-used verification link.')
        conn.execute(update(C).where(C.c.id == change_id).values(**{
            side+'_verified_at': now(), side+'_token_hash': None, 'version': row['version']+1}))


def cancel(actor, change_id):
    with transaction() as conn:
        administrator(conn, actor)
        row = get_change(conn, change_id)
        conn.execute(update(C).where(C.c.id == change_id).values(status='cancelled', open_key=None,
            old_token_hash=None, new_token_hash=None, version=row['version']+1))


def artifacts(conn, email):
    """Only completed, DB-owned archives can survive without rewriting snapshots."""
    from database.models import Match
    from app import MATCH_INSTANCES, MATCH_INSTANCES_LOCK
    with MATCH_INSTANCES_LOCK:
        if any(m.data.get('created_by') == email and m.data.get('current_state') != 'completed'
               for m in MATCH_INSTANCES.values()):
            raise ValueError('Finish active matches before changing this email.')
    root = Path(current_app.config['IDENTITY_EMAIL_MATCH_DIR'])
    result = []
    for path in sorted(root.glob('*.json')):
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
        except (OSError, ValueError):
            raise ValueError('A match file cannot be inspected. Operator review is required.') from None
        if not isinstance(data, dict):
            raise ValueError('A match file has an unsupported format.')
        if data.get('created_by') != email:
            continue
        archived = conn.execute(select(Match.id).where(Match.id == data.get('match_id'), Match.user_id == email)).first()
        if data.get('current_state') != 'completed' or not archived:
            raise ValueError('Finish and archive saved matches before changing this email.')
        result.append({'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest()})
    return result


def plan(conn, row):
    user = target(conn, row)
    available(conn, row['new_email'], row['old_email'])
    if not row['new_verified_at'] or not (row['old_verified_at'] or row['recovery_evidence']):
        raise ValueError('Required mailbox verification is incomplete.')
    coverage(conn)
    counts, fingerprints = {}, {}
    for table_name, column in sorted(OWNERSHIP | NON_FK_CURRENT | REVOKE):
        table = db.metadata.tables[table_name]
        records = conn.execute(select(table).where(table.c[column] == row['old_email'])).mappings().all()
        name = table_name + '.' + column
        counts[name] = len(records)
        # Session activity alone should not invalidate an otherwise unchanged preview.
        values = [dict(record) for record in records]
        if table_name == 'active_sessions':
            values = [{'id': record['id'], 'session_token': record['session_token']} for record in records]
        fingerprints[name] = digest(json.dumps(sorted(values, key=lambda r: json.dumps(r, sort_keys=True, default=str)), sort_keys=True, default=str))
    state = {k: v for k, v in dict(user).items() if k not in ('last_login',)}
    result = {'request': row['id'], 'version': row['version'], 'old': row['old_email'], 'new': row['new_email'],
        'counts': counts, 'artifacts': artifacts(conn, row['old_email'])}
    result['fingerprint'] = digest(json.dumps({'plan': result, 'user': state, 'rows': fingerprints, 'reason': row['reason'], 'recovery': row['recovery_evidence'], 'self_service': row['self_service']}, sort_keys=True, default=str))
    return result


def preview(actor, change_id):
    with db.engine.connect() as conn:
        row = get_change(conn, change_id)
        authorize_change(conn, actor, row)
        result = plan(conn, row)
    return result, signer().dumps({'actor': actor, 'id': change_id, 'fingerprint': result['fingerprint']})


def backup_locked(conn, change_id):
    """Separate read connection sees the committed state under our reserved lock.

    BEGIN IMMEDIATE excludes writers before the backup begins and until commit.
    Do not use SQLite backup() on the connection holding a write transaction.
    """
    source = db.engine.url.database
    if not source or source == ':memory:':
        raise ValueError('Operation backups require a file-backed SQLite database.')
    filename = 'email-' + change_id + '-' + uuid.uuid4().hex + '.sqlite3'
    path = directory() / filename
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(path)) as dest:
            src.backup(dest)
            if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Operation backup verification failed.')
        with path.open('rb') as f:
            os.fsync(f.fileno())
        return filename
    except Exception:
        path.unlink(missing_ok=True)
        raise


def execute(actor, change_id, token, confirmed_email):
    try:
        signed = signer().loads(token, max_age=900)
    except (BadSignature, TypeError):
        raise ValueError('Preview expired or invalid. Create a new preview.') from None
    if signed.get('actor') != actor or signed.get('id') != change_id:
        raise ValueError('Preview belongs to another administrator or request.')
    with admission(exclusive=True), transaction() as conn:
        row = get_change(conn, change_id, pending=False)
        authorize_change(conn, actor, row)
        if confirmed_email != row['new_email']:
            raise ValueError('Type the exact new email to confirm.')
        if row['status'] == 'completed':
            receipt = json.loads(row['receipt_json'])
            if receipt['fingerprint'] != signed.get('fingerprint') or receipt['actor'] != actor:
                raise ValueError('This request was completed using a different preview.')
            return receipt
        row = get_change(conn, change_id)
        result = plan(conn, row)
        if result['fingerprint'] != signed.get('fingerprint'):
            raise ValueError('Account data changed. Review a fresh preview.')
        backup = backup_locked(conn, change_id)
        identity = available(conn, row['new_email'], row['old_email'])
        conn.exec_driver_sql('PRAGMA defer_foreign_keys = ON')
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        conn.execute(sqlite_insert(IdentityEmailReservation.__table__).values(canonical_email=identity.canonical).on_conflict_do_nothing())
        try:
            previous = normalize_email(row['old_email']).canonical
        except InvalidEmail:
            previous = None
        if previous:
            conn.execute(sqlite_insert(IdentityEmailReservation.__table__).values(canonical_email=previous).on_conflict_do_nothing())
        for table_name, column in sorted(OWNERSHIP | NON_FK_CURRENT):
            table = db.metadata.tables[table_name]
            conn.execute(update(table).where(table.c[column] == row['old_email']).values(**{column: identity.address}))
        conn.execute(delete(ActiveSession).where(ActiveSession.user_id == row['old_email']))
        values = dict(id=identity.address, canonical_email=identity.canonical, email_normalization_version=identity.version,
            email_normalization_error=None, email_verified=True, force_email_verify=False,
            pending_email=None, pending_email_token=None, pending_email_token_expires=None,
            email_verify_token=None, email_verify_token_expires=None, reset_token=None, reset_token_expires=None,
            pw_change_otp_hash=None, pw_change_otp_expires=None, pw_change_pending_hash=None)
        conn.execute(update(U).where(U.c.stable_id == row['user_stable_id']).values(**values))
        result.update(actor=actor, backup=backup, completed_at=now().isoformat(), stable_id=row['user_stable_id'],
                      reason=row['reason'], recovery_evidence=row['recovery_evidence'])
        conn.execute(update(C).where(C.c.id == change_id).values(status='completed', open_key=None,
            completed_at=now(), old_token_hash=None, new_token_hash=None, version=row['version']+1,
            receipt_json=json.dumps(result)))
    db.session.expire_all()
    try:
        notify_completion(change_id)
    except Exception:
        current_app.logger.warning('Email change committed; completion notices need retry.')
    return result


def notify_completion(change_id):
    from markupsafe import escape
    from utils import email_service as mail
    with db.engine.connect() as conn:
        row = get_change(conn, change_id, pending=False)
    if row['status'] != 'completed':
        return
    for side in ('old', 'new'):
        if row[side+'_notice_sent']:
            continue
        try:
            ok = mail.send_email(row[side+'_email'], 'Your SimCricketX email was changed',
                '<p>Your account email changed to '+str(escape(row['new_email']))+'. All sessions were signed out. '
                'If you did not request this change, contact support.</p>')
        except Exception:
            ok = False
        if ok:
            with transaction() as conn:
                conn.execute(update(C).where(C.c.id == change_id).values(**{side+'_notice_sent': True}))


def purge_backups():
    cutoff = now().timestamp() - timedelta(days=30).total_seconds()
    count = 0
    for path in directory().glob('email-*.sqlite3'):
        if not path.is_symlink() and path.stat().st_mtime <= cutoff:
            path.unlink()
            count += 1
    with transaction() as conn:
        conn.execute(update(C).where(C.c.status == 'pending', C.c.expires_at <= now()).values(
            status='expired', open_key=None, old_token_hash=None, new_token_hash=None))
    return count


def socket_admission(function):
    from functools import wraps
    @wraps(function)
    def wrapped(*args, **kwargs):
        from flask_socketio import emit
        from flask_login import current_user
        from flask import session

        def refuse(code, message):
            if request.event.get('message') != 'connect':
                emit('ws_error', {'code': code, 'message': message})
            return False

        guard = admission()
        try:
            guard.__enter__()
        except ValueError:
            return refuse('identity_update', 'An identity update is in progress. Retry shortly.')
        try:
            # Socket cookies may outlive HTTP sessions, including when the
            # network policy is disabled. Never advance a revoked session.
            if current_user.is_authenticated:
                with db.engine.connect() as conn:
                    active = conn.execute(select(ActiveSession.id).where(
                        ActiveSession.user_id == current_user.id,
                        ActiveSession.session_token == session.get('session_token', ''))).first()
                if not active:
                    return refuse('auth_required', 'Sign in again to continue.')
            return function(*args, **kwargs)
        finally:
            guard.__exit__(None, None, None)
    return wrapped


def authorize_change(conn, actor, row):
    if row['self_service'] and actor == row['user_stable_id']:
        # Completed receipts allow the exact signed retry; pending requests
        # require the original mandatory-remediation flag to remain set.
        if row['status'] != 'completed':
            user = target(conn, row)
            if not user['force_email_verify']:
                raise ValueError('Mandatory correction is no longer required.')
        return
    administrator(conn, actor)


def complete_mandatory(change_id):
    with db.engine.connect() as conn:
        row = get_change(conn, change_id)
        if not row['self_service']:
            return False
    _, token = preview(row['user_stable_id'], change_id)
    execute(row['user_stable_id'], change_id, token, row['new_email'])
    return True
