"""Fixed legacy-email campaigns. Activation and deletion are explicit operator actions."""
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import os
import zipfile
import uuid

from flask import current_app, request, jsonify, redirect, url_for
from flask_login import current_user
from sqlalchemy import select, insert, update, delete, or_, tuple_, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from database import db
from database.models import (User, IdentityEmailCampaign, IdentityCampaignMember,
    IdentityCampaignAction, IdentityCampaignFile)
from services import identity_email_change as changes
from utils.identity_rules import validate_new_email, normalize_email, InvalidEmail

C = IdentityEmailCampaign.__table__
M = IdentityCampaignMember.__table__
A = IdentityCampaignAction.__table__
F = IdentityCampaignFile.__table__
U = User.__table__
ALLOWED = frozenset({'identity_campaign.member', 'identity_email.verify', 'force_verify_email',
    'force_verify_email_change', 'force_change_password', 'logout', 'static', 'auth_challenge',
    'identity_device_privacy', 'admin_stop_impersonation'})


def now():
    return datetime.utcnow()


def pending(subject, conn=None):
    executor = conn if conn is not None else db.session
    return executor.execute(select(M, C.c.deadline).join(C, M.c.campaign_id == C.c.id).where(
        M.c.subject == subject, M.c.status == 'pending')).mappings().first()


def audit(conn, campaign, action, reason, actor=None, member=None):
    conn.execute(insert(A).values(campaign_id=campaign, member_id=member, actor=actor,
        action=action, reason=reason, created_at=now()))


def inspect_candidates(conn):
    result = {'members': [], 'admins': [], 'collisions': []}
    canonicals = {}
    for u in conn.execute(select(U).order_by(U.c.stable_id)).mappings():
        try:
            canonical = normalize_email(u['id']).canonical
            canonicals.setdefault(canonical, []).append(u['stable_id'])
        except InvalidEmail:
            pass
        try:
            validate_new_email(u['id'])
        except InvalidEmail as exc:
            if not u['stable_id']:
                raise ValueError('Run identity foundation migration before activation.')
            result['admins' if u['is_admin'] else 'members'].append(
                {'subject': u['stable_id'], 'address': u['id'], 'reason': exc.code, 'prior_force_verify': bool(u['force_email_verify'])})
    result['collisions'] = [v for v in canonicals.values() if len(v) > 1]
    return result


def preview(actor):
    with db.engine.connect() as conn:
        changes.administrator(conn, actor)
        plan = inspect_candidates(conn)
    token = changes.signer().dumps({'campaign_actor': actor,
        'fingerprint': changes.digest(json.dumps(plan, sort_keys=True))})
    return plan, token


def activate(actor, token):
    from itsdangerous import BadSignature
    try:
        signed = changes.signer().loads(token, max_age=900)
    except (BadSignature, TypeError):
        raise ValueError('Preview expired; inspect the campaign again.') from None
    with changes.admission(exclusive=True), changes.transaction() as conn:
        changes.administrator(conn, actor)
        if conn.execute(select(M.c.id).where(M.c.status == 'pending')).first():
            raise ValueError('Resolve the current campaign before starting another.')
        plan = inspect_candidates(conn)
        if signed != {'campaign_actor': actor, 'fingerprint': changes.digest(json.dumps(plan, sort_keys=True))}:
            raise ValueError('Accounts changed; inspect a fresh preview.')
        if not plan['members']:
            raise ValueError('No affected non-admin accounts.')
        at, cid = now(), str(uuid.uuid4())
        conn.execute(insert(C).values(id=cid, started_at=at, deadline=at+timedelta(days=30),
            deletion_enabled=False, created_by=actor))
        for item in plan['members']:
            conn.execute(insert(M).values(id=str(uuid.uuid4()), campaign_id=cid, **item,
                status='pending', support='', response='', notices='{}'))
            conn.execute(update(U).where(U.c.stable_id == item['subject']).values(force_email_verify=True,
                email_verify_token=None, email_verify_token_expires=None))
        audit(conn, cid, 'activate', 'Fixed 30-day campaign activated after preview.', actor)
    db.session.expire_all()
    return cid


def corrected(conn, subject):
    # Called in the SAME transaction as verified identity repointing.
    rows = conn.execute(select(M).where(M.c.subject == subject, M.c.status == 'pending')).mappings().all()
    for row in rows:
        conn.execute(update(M).where(M.c.id == row['id']).values(status='corrected', closed_at=now(),
            address=None, support='', response='', error=None))
        audit(conn, row['campaign_id'], 'corrected', 'Verified replacement committed.', member=row['id'])


def guard():
    if not current_user.is_authenticated:
        return
    row = pending(current_user.stable_id)
    if not row:
        return
    # A support exception never bypasses a ban, even with network policy off.
    if current_user.is_banned and (current_user.banned_until is None or current_user.banned_until > now()):
        if request.endpoint not in {'logout', 'static', 'admin_stop_impersonation'}:
            return jsonify(error='Account suspended.', code='banned'), 403
    if request.endpoint in ALLOWED:
        return
    if request.is_json or request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
        return jsonify(error='Verify a replacement email before continuing.', code='email_correction_required',
            correction_url=url_for('force_verify_email')), 403
    return redirect(url_for('force_verify_email'))


def member_action(subject, message):
    message = (message or '').strip()
    if not 10 <= len(message) <= 2000:
        raise ValueError('Explain the issue in 10–2000 characters. Do not include passwords.')
    with changes.transaction() as conn:
        row = pending(subject, conn)
        u = conn.execute(select(U).where(U.c.stable_id == subject)).mappings().first()
        if not row or not u or (u['is_banned'] and (not u['banned_until'] or u['banned_until'] > now())):
            raise ValueError('This account cannot open correction support.')
        conn.execute(update(M).where(M.c.id == row['id']).values(support=message))
        audit(conn, row['campaign_id'], 'support', 'Member updated private support request.', subject, row['id'])


def administer(actor, member, action, reason, days=None):
    reason = (reason or '').strip()
    if not 10 <= len(reason) <= 2000:
        raise ValueError('Provide a reason or reply in 10–2000 characters.')
    with changes.transaction() as conn:
        changes.administrator(conn, actor)
        row = conn.execute(select(M).where(M.c.id == member)).mappings().first()
        if not row or row['status'] != 'pending':
            raise ValueError('This campaign entry is closed.')
        values = {}
        if action == 'hold':
            try:
                days = int(days)
            except (ValueError, TypeError):
                raise ValueError('Hold must be 1–30 days.') from None
            if not 1 <= days <= 30:
                raise ValueError('Hold must be 1–30 days.')
            values['hold_until'] = now()+timedelta(days=days)
        elif action == 'release':
            values['hold_until'] = None
        elif action == 'reply':
            values['response'] = reason
        else:
            raise ValueError('Unknown action.')
        conn.execute(update(M).where(M.c.id == member).values(**values))
        audit(conn, row['campaign_id'], action, reason, actor, member)


def enable_deletion(actor, campaign, enabled):
    with changes.transaction() as conn:
        changes.administrator(conn, actor)
        if not conn.execute(select(C.c.id).where(C.c.id == campaign)).first():
            raise ValueError('Campaign not found.')
        if enabled:
            from services.identity_signup import key
            if current_app.config.get('IDENTITY_SIGNUP_ENFORCE') is not True:
                raise ValueError('Enable signup identity enforcement before expiry deletion so return approvals are enforced.')
            key()  # Markers are mandatory even when signup enforcement is off.
            changes.directory()
        conn.execute(update(C).where(C.c.id == campaign).values(deletion_enabled=enabled))
        audit(conn, campaign, 'enable_deletion' if enabled else 'pause_deletion', 'Operator changed expiry processing.', actor)


def send_notices():
    from markupsafe import escape
    from utils.email_service import send_email
    with db.engine.connect() as conn:
        rows = conn.execute(select(M, C.c.started_at, C.c.deadline).join(C, M.c.campaign_id == C.c.id)
            .where(M.c.status == 'pending')).mappings().all()
    for row in rows:
        if now() >= row['deadline']:
            continue
        for label, due in [('activation', row['started_at']), ('seven_days', row['deadline']-timedelta(days=7)),
                           ('one_day', row['deadline']-timedelta(days=1))]:
            # Serialize reminder attempts and deletion/correction. No claim of exactly-once SMTP.
            with changes.transaction() as conn:
                live = conn.execute(select(M).where(M.c.id == row['id'])).mappings().first()
                if not live or live['status'] != 'pending':
                    break
                notices = json.loads(live['notices'])
                if due > now() or notices.get(label) == 'sent':
                    continue
                try:
                    ok = send_email(live['address'], 'Action required: correct your SimCricketX email',
                        '<p>Sign in and verify a replacement email by '+str(escape(str(row['deadline'])))+
                        ' UTC. Normal app access is restricted. Uncorrected accounts are due for deletion. '
                        'Private correction support is available after sign-in. A support request does not pause deletion.</p>')
                except Exception:
                    ok = False
                notices[label] = 'sent' if ok else 'failed'
                conn.execute(update(M).where(M.c.id == row['id']).values(notices=json.dumps(notices)))


def roots():
    from services.community_media import media_root
    return {'matches': Path(current_app.config['IDENTITY_EMAIL_MATCH_DIR']),
            'archives': Path(current_app.config['ARCHIVES_FOLDER']), 'community': Path(media_root())}


def safe_path(area, name):
    root = roots()[area].resolve()
    path = root / name
    if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Unsafe artifact path; operator review required.')
    return path


def file_record(area, name, match_key=None):
    path = safe_path(area, name)
    if not path.exists():
        return None
    return {'area': area, 'name': name, 'match_key': match_key,
            'content_hash': hashlib.sha256(path.read_bytes()).hexdigest()}


def artifacts(conn, user, private_posts):
    tables = db.metadata.tables
    owned = set(conn.execute(select(tables['matches'].c.id).where(tables['matches'].c.user_id == user['id'])).scalars())
    files = []
    for path in roots()['matches'].glob('*.json*'):
        safe_path('matches', path.name)
        try:
            data = json.loads(path.read_bytes())
            mid = data.get('match_id')
        except (ValueError, AttributeError):
            raise ValueError('Unreadable match artifact; operator review required.') from None
        owner = conn.execute(select(tables['matches'].c.user_id).where(tables['matches'].c.id == mid)).scalar_one_or_none() if mid else None
        if mid in owned or (owner is None and data.get('created_by') == user['id']):
            files.append(file_record('matches', path.name, str(mid) if mid else None))
    from utils.match_ownership import owns_archive
    for path in roots()['archives'].glob('*.zip'):
        if owns_archive(path, user['id'], conn=conn):
            files.append(file_record('archives', path.name))
    images = tables['community_images']
    rows = conn.execute(select(images).where(or_(images.c.uploader_id == user['id'], images.c.post_id.in_(private_posts)))).mappings()
    for row in rows:
        for name in (row['path'], row['thumb_path']):
            files.append(file_record('community', name))
    return [f for f in files if f]


# User-linked rows that are owned data. Other references are explicitly preserved/anonymized.
DELETE_USER_LINKS = frozenset({'teams', 'matches', 'tours', 'tournaments', 'user_players', 'leagues',
    'stadium_adoptions', 'user_banner_dismissals', 'login_history', 'user_ground_configs',
    'support_prompt_events', 'auth_event_log', 'community_votes', 'community_notifications',
    'community_mentions', 'community_reports', 'active_sessions'})


def delete_graph(conn, user, private_posts):
    """Delete owned roots and dependent rows; nullable shared references are detached.

    Deferrable transaction permits arbitrary delete order. Unknown user references
    fail closed through Feature 8's complete schema inventory.
    """
    changes.coverage(conn)
    tables = db.metadata.tables
    targets = {}
    def add(name, predicate):
        table = tables[name]
        keys = tuple(c.name for c in table.primary_key)
        rows = conn.execute(select(*(table.c[k] for k in keys)).where(predicate)).all()
        old = targets.setdefault(name, set())
        size = len(old); old.update(tuple(r) for r in rows)
        return len(old) != size
    for name, col in changes.OWNERSHIP | changes.REVOKE:
        if name in DELETE_USER_LINKS:
            add(name, tables[name].c[col] == user['id'])
    add('users', U.c.stable_id == user['stable_id'])
    add('stadiums', tables['stadiums'].c.owner_user_id == user['id'])
    add('community_posts', tables['community_posts'].c.id.in_(private_posts))
    comments = tables['community_comments']
    private_replies = list(conn.execute(select(comments.c.id).where(comments.c.author_id == user['id'], comments.c.post_id.in_(
        select(tables['community_posts'].c.id).where(tables['community_posts'].c.visibility == 'private')))).scalars())
    # Preserve other members' replies in someone else's private thread.
    conn.execute(update(comments).where(comments.c.parent_id.in_(private_replies)).values(parent_id=None))
    add('community_comments', comments.c.id.in_(private_replies))
    images = tables['community_images']
    add('community_images', or_(images.c.uploader_id == user['id'], images.c.post_id.in_(private_posts)))
    add('fixture_replays', tables['fixture_replays'].c.user_id == user['id'])
    # Follow only declared dependent relationships; nullable references survive.
    changed = True
    while changed:
        changed = False
        for table in tables.values():
            for col in table.c:
                for fk in col.foreign_keys:
                    parent = fk.column.table
                    if parent.name not in targets or not targets[parent.name]:
                        continue
                    pk = tuple(parent.primary_key)
                    parent_filter = tuple_(*pk).in_(targets[parent.name])
                    values = select(fk.column).where(parent_filter)
                    if fk.ondelete == 'CASCADE' or (not col.nullable and fk.ondelete != 'SET NULL'):
                        changed = add(table.name, col.in_(values)) or changed
    for name, column in changes.OWNERSHIP:
        if name in {'teams', 'matches', 'tours', 'tournaments', 'leagues', 'user_players'} and name in targets and targets[name]:
            table = tables[name]
            if conn.execute(select(table.c[column]).where(tuple_(*table.primary_key).in_(targets[name]), table.c[column].is_not(None), table.c[column] != user['id'])).first():
                raise ValueError('Shared owned data requires operator review before deletion.')
    conn.exec_driver_sql('PRAGMA defer_foreign_keys = ON')
    # Null remaining inbound references BEFORE removing parent rows.
    for table in tables.values():
        for col in table.c:
            for fk in col.foreign_keys:
                parent = fk.column.table
                if parent.name in targets and targets[parent.name] and col.nullable:
                    values = select(fk.column).where(tuple_(*parent.primary_key).in_(targets[parent.name]))
                    conn.execute(update(table).where(col.in_(values)).values({col.name: None}))
    for name, keys in targets.items():
        if keys:
            conn.execute(delete(tables[name]).where(tuple_(*tables[name].primary_key).in_(keys)))
    # Public text survives; account-linked metadata does not.
    posts = tables['community_posts']
    conn.execute(update(posts).where(posts.c.author_id == user['id']).values(author_id=None, page_url=None, app_version=None, user_agent=None))
    for name, column in [('community_posts','edited_by'), ('community_posts','deleted_by'), ('community_comments','edited_by')]:
        table = tables[name]
        if column in table.c:
            conn.execute(update(table).where(table.c[column] == user['id']).values({column: None}))
    for name, column in changes.HISTORICAL:
        table = tables[name]
        # These rows are diagnostic snapshots, not campaign receipts.
        conn.execute(delete(table).where(table.c[column] == user['id']))
    votes = tables['community_votes']
    up = select(func.count()).where(votes.c.post_id == posts.c.id, votes.c.value == 1).scalar_subquery()
    down = select(func.count()).where(votes.c.post_id == posts.c.id, votes.c.value == -1).scalar_subquery()
    count = select(func.count()).where(comments.c.post_id == posts.c.id, comments.c.deleted_at.is_(None)).scalar_subquery()
    conn.execute(update(posts).values(vote_count=up, downvote_count=down, score=up-down, comment_count=count))
    return {name: len(keys) for name, keys in targets.items() if keys}


def marker(conn, user):
    from services import identity_signup as signup
    try:
        hashed = signup.identity_hash(normalize_email(user['id']))
    except InvalidEmail:
        hashed = signup.hashed('deleted-invalid-email', user['id'].strip().lower())
    key_id = signup.devices.key_id(signup.key())
    if conn.execute(select(signup.M.c.identity_hash).where(signup.M.c.key_id != key_id)).first():
        raise ValueError('Deletion marker key mismatch.')
    prior = conn.execute(select(signup.M).where(signup.M.c.identity_hash == hashed)).mappings().first()
    reason = prior['reason'] if prior and prior['reason'] in ('banned', 'merged') else ('banned' if user['is_banned'] else 'deleted')
    values = dict(identity_hash=hashed, key_id=key_id, reason=reason, generation=str(uuid.uuid4()), created_at=now())
    conn.execute(sqlite_insert(signup.M).values(**values).on_conflict_do_update(index_elements=['identity_hash'], set_=values))
    conn.execute(update(signup.P).where(signup.P.c.open_key == hashed).values(status='revoked',
        closed_at=now(), open_key=None, version=signup.P.c.version+1))


def delete_member(member):
    with changes.admission(exclusive=True), changes.transaction() as conn:
        row = conn.execute(select(M, C.c.deadline, C.c.deletion_enabled).join(C, M.c.campaign_id == C.c.id)
            .where(M.c.id == member)).mappings().first()
        if not row or row['status'] != 'pending' or not row['deletion_enabled'] or now() < row['deadline'] or (row['hold_until'] and row['hold_until'] > now()):
            return False
        if current_app.config.get('IDENTITY_SIGNUP_ENFORCE') is not True:
            raise ValueError('Signup enforcement must remain enabled during campaign deletion.')
        user = conn.execute(select(U).where(U.c.stable_id == row['subject'])).mappings().first()
        if not user or user['is_admin']:
            if user:
                conn.execute(update(U).where(U.c.stable_id == user['stable_id']).values(force_email_verify=row['prior_force_verify']))
            conn.execute(update(M).where(M.c.id == member).values(status='excluded', closed_at=now(), address=None, support='', response=''))
            audit(conn, row['campaign_id'], 'excluded', 'Account missing or now an administrator.', member=member)
            return False
        # Only verified replacement through the engine closes enrollment. Never
        # infer correction from a changed blocklist or a cleared boolean alone.
        if user['id'] != row['address']:
            raise ValueError('Untracked identity change requires operator review.')
        posts = db.metadata.tables['community_posts']
        private_posts = list(conn.execute(select(posts.c.id).where(posts.c.author_id == user['id'], posts.c.visibility == 'private')).scalars())
        files = artifacts(conn, user, private_posts)
        changes.coverage(conn)
        backup = changes.backup_locked(conn, 'campaign-'+member)
        file_backup = backup_artifacts(files, member)
        marker(conn, user)
        # Mask public authorship metadata before graph detaches the author FK.
        conn.execute(update(posts).where(posts.c.author_id == user['id']).values(page_url=None, app_version=None, user_agent=None))
        # Pending mailbox proofs cannot survive removal of their owner.
        conn.execute(delete(changes.C).where(changes.C.c.user_stable_id == user['stable_id'], changes.C.c.status != 'completed'))
        counts = delete_graph(conn, user, private_posts)
        for f in files:
            conn.execute(insert(F).values(member_id=member, **f))
        conn.execute(update(M).where(M.c.id == member).values(status='deleted', closed_at=now(),
            address=None, support='', response='', error=None,
            receipt=json.dumps({'backup': backup, 'artifact_backup': file_backup, 'counts': counts, 'file_count': len(files)})))
        audit(conn, row['campaign_id'], 'deleted', 'Expiry eligibility rechecked; backup verified; account deleted.', member=member)
    db.session.expire_all()
    # Purge this process's caches; other workers still enforce session and DB checks.
    from app import MATCH_INSTANCES, MATCH_INSTANCES_LOCK
    with MATCH_INSTANCES_LOCK:
        for key, match in list(MATCH_INSTANCES.items()):
            if match.data.get('created_by') == user['id']:
                MATCH_INSTANCES.pop(key, None)
    cleanup_files()
    return True


def cleanup_files():
    with db.engine.connect() as conn:
        files = conn.execute(select(F).where(F.c.cleaned_at.is_(None))).mappings().all()
    for f in files:
        try:
            path = safe_path(f['area'], f['name'])
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != f['content_hash']:
                    raise ValueError('Artifact content changed; operator review required.')
                path.unlink()
            values = {'cleaned_at': now(), 'error': None}
        except (OSError, ValueError):
            values = {'error': 'Artifact cleanup needs operator review or retry.'}
        with changes.transaction() as conn:
            conn.execute(update(F).where(F.c.id == f['id']).values(**values))


def run_due():
    with db.engine.connect() as conn:
        ids = conn.execute(select(M.c.id).where(M.c.status == 'pending')).scalars().all()
    count = 0
    for mid in ids:
        try:
            count += bool(delete_member(mid))
        except Exception as exc:
            # One failed candidate must not skip others. Preserve a non-sensitive
            # retry indication rather than storing arbitrary exception payloads.
            current_app.logger.warning('Campaign deletion deferred (%s).', type(exc).__name__)
            with changes.transaction() as conn:
                conn.execute(update(M).where(M.c.id == mid, M.c.status == 'pending').values(error='Deletion deferred; inspect backup, references and artifacts, then retry.'))
    cleanup_files()
    return count


def purge():
    cutoff = now()-timedelta(days=180)
    with changes.transaction() as conn:
        ids = list(conn.execute(select(M.c.id).where(M.c.closed_at <= cutoff,
            ~M.c.id.in_(select(F.c.member_id).where(F.c.cleaned_at.is_(None))))).scalars())
        conn.execute(delete(A).where(A.c.member_id.in_(ids)))
        conn.execute(delete(F).where(F.c.member_id.in_(ids)))
        conn.execute(delete(M).where(M.c.id.in_(ids)))
        empty = list(conn.execute(select(C.c.id).where(~C.c.id.in_(select(M.c.campaign_id)))).scalars())
        conn.execute(delete(A).where(A.c.campaign_id.in_(empty)))
        conn.execute(delete(C).where(C.c.id.in_(empty)))
    return len(ids)


def backup_artifacts(files, member):
    """Private recovery copy of the exact files selected under admission lock."""
    if not files:
        return None
    name = 'email-campaign-' + member + '-' + uuid.uuid4().hex + '.zip'
    path = changes.directory() / name
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                seen = set()
                for f in files:
                    entry = f['area'] + '/' + f['name']
                    if entry in seen:
                        continue
                    seen.add(entry)
                    raw = safe_path(f['area'], f['name']).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != f['content_hash']:
                        raise ValueError('Artifact changed before backup.')
                    archive.writestr(entry, raw)
            stream.flush()
            os.fsync(stream.fileno())
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError('Artifact backup verification failed.')
        return name
    except Exception:
        path.unlink(missing_ok=True)
        raise


def purge_artifact_backups():
    from datetime import timezone
    cutoff = now().replace(tzinfo=timezone.utc).timestamp()-timedelta(days=30).total_seconds()
    count = 0
    for path in changes.directory().glob('email-campaign-*.zip'):
        if not path.is_symlink() and path.stat().st_mtime <= cutoff:
            path.unlink()
            count += 1
    return count
