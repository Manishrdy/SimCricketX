"""Ownership-reviewed merges. Previews are read-only; execution rechecks under lock."""
from datetime import timedelta
import json
import hashlib
from pathlib import Path
import secrets
import uuid

from flask import current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import select, insert, update, delete, inspect, or_, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database import db
from database.models import IdentityMerge, IdentityReviewCase, IdentityReviewAction
from services import identity_email_change as base, identity_signup as signup
from utils.identity_rules import validate_new_email, normalize_email

M = IdentityMerge.__table__
R = IdentityReviewCase.__table__
U = base.U
# All FK references to stable identities require an explicit merge policy.
STABLE = {'identity_device_sightings', 'identity_network_appeals',
          'identity_network_events', 'identity_archive_owners'}
# Conflicting rows use the survivor's value; all discarded source keys are previewed.
KEEP = {'user_players': ('master_player_id',),
        'user_ground_configs': ('match_format',),
        'stadium_adoptions': ('source_stadium_id', 'source_revision'),
        'community_votes': ('post_id',), 'user_banner_dismissals': ('banner_version',)}
UNIQUE = {**{name: {frozenset(('user_id', *keys))} for name, keys in KEEP.items()},
          'teams': {frozenset(('user_id', 'short_code'))},
          'leagues': {frozenset(('user_id', 'name'))},
          'tours': {frozenset(('user_id', 'creation_token'))},
          'tournaments': {frozenset(('user_id', 'creation_token'))}}


def table(name):
    return db.metadata.tables[name]


def signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='identity-merge-v1')


def get(conn, mid, pending=True):
    row = conn.execute(select(M).where(M.c.id == mid)).mappings().first()
    if not row:
        raise ValueError('Merge request not found.')
    if pending and (row['status'] != 'pending' or row['expires_at'] <= base.now()):
        raise ValueError('Merge request is closed or expired.')
    return row


def accounts(conn, row):
    from services.identity_campaign import pending
    users = []
    for side in ('source', 'survivor'):
        user = conn.execute(select(U).where(U.c.stable_id == row[side])).mappings().first()
        if not user or user['id'] != row[side+'_address'] or user['is_admin']:
            raise ValueError('Choose two distinct non-admin accounts with unchanged identities.')
        if user['is_banned'] or user['force_email_verify'] or user['force_password_reset'] or pending(user['stable_id'], conn):
            raise ValueError('Resolve bans, mandatory correction and password remediation before merging.')
        if user['lockout_until'] and user['lockout_until'] > base.now():
            raise ValueError('Resolve account lockout before merging.')
        users.append(user)
    if users[0]['stable_id'] == users[1]['stable_id']:
        raise ValueError('Choose two distinct accounts.')
    validate_new_email(users[1]['id'])
    if not users[1]['email_verified']:
        raise ValueError('The survivor must have a verified acceptable email.')
    case = conn.execute(select(R).where(R.c.id == row['case_key'])).mappings().first()
    if not case or {case['left_id'], case['right_id']} != {row['source'], row['survivor']} or case['status'] not in ('open', 'investigating'):
        raise ValueError('An open or investigating duplicate case for this pair is required.')
    # Reserve both aliases, including when public signup enforcement is off.
    if not signup.enabled():
        raise ValueError('Enable signup identity enforcement before merging, so retired identities cannot register again.')
    key_id = signup.devices.key_id(signup.key())
    if conn.execute(select(signup.M).where(signup.M.c.key_id != key_id)).first():
        raise ValueError('Identity marker key needs operator recovery.')
    for user in users:
        identity = normalize_email(user['id'])
        if conn.execute(select(signup.M).where(signup.M.c.identity_hash == signup.identity_hash(identity))).first():
            raise ValueError('Existing deletion/ban/merge marker requires separate review.')
    return users, case


def create(actor, case_key, source_address, survivor_address, reason, recovery=''):
    reason, recovery = reason.strip(), recovery.strip()
    if not 20 <= len(reason) <= 2000 or (recovery and not 20 <= len(recovery) <= 2000):
        raise ValueError('Document common ownership (20–2000 characters) and any source recovery evidence (20–2000 characters).')
    mid, at = str(uuid.uuid4()), base.now()
    tokens = {s: secrets.token_urlsafe(32) for s in ('source', 'survivor')}
    with base.transaction() as conn:
        base.administrator(conn, actor)
        users = [conn.execute(select(U).where(U.c.id == address)).mappings().first()
                 for address in (source_address, survivor_address)]
        if not all(users):
            raise ValueError('Select two existing accounts.')
        row = dict(id=mid, case_key=case_key, source=users[0]['stable_id'], survivor=users[1]['stable_id'],
                   source_address=source_address, survivor_address=survivor_address, administrator=actor,
                   reason=reason, recovery=recovery, created_at=at, expires_at=at+timedelta(hours=24), status='pending',
                   source_proof=None if recovery else base.digest(tokens['source']), survivor_proof=base.digest(tokens['survivor']))
        accounts(conn, row)
        expire(conn)
        ids = [u['stable_id'] for u in users]
        if conn.execute(select(M.c.id).where(M.c.status == 'pending', or_(M.c.source.in_(ids), M.c.survivor.in_(ids)))).first():
            raise ValueError('An account already has an open merge request.')
        conn.execute(insert(M).values(**row))
    deliver(mid, tokens)
    return mid


def deliver(mid, tokens):
    from utils import email_service as mail
    from markupsafe import escape
    with db.engine.connect() as conn:
        row = get(conn, mid)
    success = []
    for side in ('source', 'survivor'):
        if side == 'source' and row['recovery']:
            continue
        url = mail._app_url().rstrip('/') + '/identity/merge/verify/'+mid+'/'+side+'/'+tokens[side]
        try:
            success.append(bool(mail.send_email(row[side+'_address'], 'Confirm your SimCricketX account merge',
                '<p>Confirm only if you requested combining your accounts. The source login will be retired; '
                'private history will become accessible through the surviving account. This confirms ownership only; '
                'an administrator must review and apply the merge.</p><p><a href="'+str(escape(url))+'">Review confirmation</a></p>')))
        except Exception:
            success.append(False)
    with base.transaction() as conn:
        conn.execute(update(M).where(M.c.id == mid).values(delivery='sent' if all(success) else 'failed'))


def verify(mid, side, token):
    if side not in ('source', 'survivor') or not token:
        raise ValueError('Invalid mailbox confirmation.')
    with base.transaction() as conn:
        row = get(conn, mid)
        accounts(conn, row)
        if not row[side+'_proof'] or not secrets.compare_digest(row[side+'_proof'], base.digest(token)):
            raise ValueError('Invalid or already used mailbox confirmation.')
        conn.execute(update(M).where(M.c.id == mid).values(**{side+'_proof': None, side+'_verified': base.now()}))


def expire(conn):
    conn.execute(update(M).where(M.c.status == 'pending', M.c.expires_at <= base.now()).values(
        status='expired', source_proof=None, survivor_proof=None))


def cancel(actor, mid):
    with base.transaction() as conn:
        base.administrator(conn, actor)
        get(conn, mid)
        conn.execute(update(M).where(M.c.id == mid).values(status='cancelled', source_proof=None, survivor_proof=None))


def coverage(conn):
    base.coverage(conn)
    inspector = inspect(conn)
    actual, dependents = set(), set()
    for name in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(name):
            if fk['referred_table'] in KEEP:
                dependents.add((name, tuple(fk['constrained_columns']), fk['referred_table'], tuple(fk['referred_columns'])))
            if fk['referred_table'] == 'users' and fk['referred_columns'] == ['stable_id']:
                actual.update((name, c) for c in fk['constrained_columns'])
    if dependents != {(n, ('user_player_id',), 'user_players', ('id',)) for n in ('players', 'auction_players')}:
        raise ValueError('Discarded-row dependency coverage changed; operator review is required.')
    if actual != {(n, 'user_stable_id') for n in STABLE}:
        raise ValueError('Stable account reference coverage changed; operator review is required.')
    stable_names = {'user_stable_id', 'subject', 'left_id', 'right_id', 'assignee', 'source', 'survivor', 'administrator'}
    stable_non_fk = {(t.name, c.name) for t in db.metadata.tables.values() for c in t.c
                     if c.name in stable_names and not c.foreign_keys}
    expected = {('identity_review_cases', n) for n in ('left_id', 'right_id', 'assignee')} | {
        ('identity_email_changes', 'user_stable_id'), ('identity_campaign_members', 'subject')} | {
        ('identity_merges', n) for n in ('source', 'survivor', 'administrator')}
    # These source columns describe event/provider origins, not account identities.
    expected |= {(n, 'source') for n in ('exception_log', 'identity_network_cache', 'match_diagnostic_events', 'support_prompt_events')}
    if stable_non_fk != expected:
        raise ValueError('Stable non-FK account reference coverage changed; operator review is required.')
    # Catch new owner-dependent uniqueness rules rather than silently losing data.
    for name, col in base.OWNERSHIP | base.NON_FK_CURRENT:
        found = set()
        constraints = inspector.get_unique_constraints(name) + [i for i in inspector.get_indexes(name) if i['unique']]
        pk = inspector.get_pk_constraint(name)['constrained_columns']
        constraints.append({'column_names': pk})
        for constraint in constraints:
            cols = constraint['column_names']
            if col in cols:
                found.add(frozenset(cols))
        if found != UNIQUE.get(name, set()):
            raise ValueError('Merge uniqueness coverage changed for '+name+'.')


def primary(row, t):
    return {c.name: row[c.name] for c in t.primary_key}


def where_key(t, key):
    return [t.c[k] == v for k, v in key.items()]


def rows(conn, t, column, value):
    return [dict(r) for r in conn.execute(select(t).where(t.c[column] == value)).mappings()]


def plan(conn, row):
    users, case = accounts(conn, row)
    if not row['survivor_verified'] or not (row['source_verified'] or row['recovery']):
        raise ValueError('Complete the required mailbox proofs first.')
    coverage(conn)
    source, survivor = users
    addresses = [u['id'] for u in users]
    seasons, leagues = table('seasons'), table('leagues')
    if conn.execute(select(seasons.c.id).join(leagues, seasons.c.league_id == leagues.c.id).where(
            leagues.c.user_id.in_(addresses), seasons.c.status.in_(['auction_live', 'auction_paused']))).first():
        raise ValueError('Finish active or paused auctions before merging.')
    auctions = table('auctions')
    if conn.execute(select(auctions.c.id).join(seasons, auctions.c.season_id == seasons.c.id).join(leagues,
            seasons.c.league_id == leagues.c.id).where(leagues.c.user_id.in_(addresses), auctions.c.started_at.is_not(None),
            auctions.c.ended_at.is_(None))).first():
        raise ValueError('Finish active auctions before merging.')
    artifacts = {side: match_artifacts(conn, u['id']) for side, u in zip(('source', 'survivor'), users)}
    archives = {side: archive_artifacts(conn, u) for side, u in zip(('source', 'survivor'), users)}
    state, counts, losses, renames, provenance = {}, {}, [], [], {}
    for name, column in sorted(base.OWNERSHIP | base.NON_FK_CURRENT | base.REVOKE):
        t = table(name)
        left, right = rows(conn, t, column, source['id']), rows(conn, t, column, survivor['id'])
        label = name+'.'+column
        state[label] = [left, right]
        counts[label] = len(left)
        provenance[label] = [primary(r, t) for r in left]
        if name in KEEP:
            keys = KEEP[name]
            # SQLite UNIQUE permits multiple NULLs: custom players have no master ID.
            existing = {tuple(r[k] for k in keys): r for r in right if all(r[k] is not None for k in keys)}
            for item in left:
                if tuple(item[k] for k in keys) in existing:
                    losses.append({'table': name, 'key': primary(item, t), 'survivor_key': primary(existing[tuple(item[k] for k in keys)], t), 'reason': 'Keep survivor value'})
        fields = {'teams': ('name', 'short_code'), 'leagues': ('name',), 'tours': ('name', 'creation_token'),
                  'tournaments': ('name', 'creation_token')}.get(name, ())
        for field in fields:
            occupied = {r[field] for r in right if r[field] is not None}
            reserved = occupied | {r[field] for r in left if r[field] is not None}
            for item in sorted(left, key=lambda r: str(primary(r, t))):
                value = item[field]
                if value is not None and value in occupied:
                    n = 1
                    length = t.c[field].type.length or 200
                    while True:
                        suffix = ('-M' if field == 'short_code' else '-merged-')+str(n)
                        replacement = str(value)[:max(0, length-len(suffix))]+suffix
                        if replacement not in reserved:
                            break
                        n += 1
                    reserved.add(replacement)
                    renames.append({'table': name, 'key': primary(item, t), 'field': field, 'old': value, 'new': replacement})
                occupied.add(value)
    # Include stable security/evidence and outstanding workflows in stale-plan binding.
    for name in STABLE:
        t = table(name)
        state[name] = [rows(conn, t, 'user_stable_id', u['stable_id']) for u in users]
        counts[name+'.user_stable_id'] = len(state[name][0])
        provenance[name+'.user_stable_id'] = [primary(r, t) for r in state[name][0]]
    for name, column in [('identity_email_changes', 'user_stable_id'), ('identity_campaign_members', 'subject')]:
        state[name] = [rows(conn, table(name), column, u['stable_id']) for u in users]
    for name in ('identity_signup_appeals', 'identity_appeal_deliveries'):
        state[name] = [rows(conn, table(name), 'email', u['id']) for u in users]
    relinks, dependent_state = pool_relinks(conn, losses, addresses)
    state['pool_dependents'] = dependent_state
    result = dict(version=2, relinks=relinks, source=row['source_address'], survivor=row['survivor_address'], counts=counts,
                  losses=losses, renames=renames, provenance=provenance, artifacts=artifacts, archives=archives,
                  source_stable=row['source'], survivor_stable=row['survivor'])
    # Stable sorting makes insertion/query order irrelevant; no passwords appear in preview/receipt.
    fingerprint = json.dumps({'result': result, 'state': state, 'users': [dict(u) for u in users],
                              'case': dict(case), 'request': dict(row)}, sort_keys=True, default=str)
    result['fingerprint'] = base.digest(fingerprint)
    return result


def preview(actor, mid):
    with db.engine.connect() as conn:
        base.administrator(conn, actor)
        result = plan(conn, get(conn, mid))
    return result, signer().dumps({'actor': actor, 'id': mid, 'fingerprint': result['fingerprint']})


def audit(conn, row, actor):
    at = base.now()
    conn.execute(update(R).where(R.c.id == row['case_key']).values(status='resolved', closed_at=at, updated_at=at, version=R.c.version+1))
    conn.execute(insert(IdentityReviewAction.__table__).values(case_id=row['case_key'], actor_id=actor, action='resolved',
        reason='Ownership-reviewed merge completed: '+row['id'], created_at=at, expires_at=at+timedelta(days=180)))


def execute(actor, mid, token, confirmed_email):
    try:
        signed = signer().loads(token, max_age=900)
    except (BadSignature, TypeError):
        raise ValueError('Preview is invalid or expired. Review a fresh preview.') from None
    if signed.get('actor') != actor or signed.get('id') != mid:
        raise ValueError('Preview belongs to a different actor or request.')
    with base.admission(exclusive=True), base.transaction() as conn:
        base.administrator(conn, actor)
        row = get(conn, mid, pending=False)
        if confirmed_email != row['survivor_address']:
            raise ValueError('Type the exact survivor email.')
        if row['status'] == 'completed':
            receipt = json.loads(row['receipt_json'])
            if receipt['fingerprint'] != signed.get('fingerprint') or receipt['actor'] != actor:
                raise ValueError('Merge was completed using a different preview.')
            return receipt
        result = plan(conn, get(conn, mid))
        if result['fingerprint'] != signed.get('fingerprint'):
            raise ValueError('Account data changed. Review a fresh preview.')
        backup = base.backup_locked(conn, 'merge-'+mid)
        source, survivor = row['source_address'], row['survivor_address']
        conn.exec_driver_sql('PRAGMA defer_foreign_keys = ON')
        for item in result['relinks']:
            t = table(item['table'])
            conn.execute(update(t).where(*where_key(t, item['key'])).values(user_player_id=item['survivor_id']))
        for item in result['losses']:
            t = table(item['table'])
            conn.execute(delete(t).where(*where_key(t, item['key'])))
        for item in result['renames']:
            t = table(item['table'])
            conn.execute(update(t).where(*where_key(t, item['key'])).values(**{item['field']: item['new']}))
        for name, column in sorted(base.OWNERSHIP | base.NON_FK_CURRENT):
            t = table(name)
            conn.execute(update(t).where(t.c[column] == source).values(**{column: survivor}))
        # Original source row keys above preserve provenance after current ownership moves.
        transfer_stable(conn, row, result)
        for side in ('source', 'survivor'):
            for item in result['archives'][side]:
                t = table('identity_archive_owners')
                conn.execute(sqlite_insert(t).values(filename=item['filename'], user_stable_id=row['survivor'], content_hash=item['sha256'])
                             .on_conflict_do_update(index_elements=['filename'], set_=dict(user_stable_id=row['survivor'], content_hash=item['sha256'])))
        revoke(conn, row)
        # Carry the stronger Community mute; merging cannot clear source restrictions.
        users = conn.execute(select(U).where(U.c.id.in_([source, survivor]))).mappings().all()
        mutes = [u['community_muted_until'] for u in users if u['community_muted_until']]
        if mutes:
            conn.execute(update(U).where(U.c.id == survivor).values(community_muted_until=max(mutes)))
        for address in (source, survivor):
            conn.execute(sqlite_insert(base.IdentityEmailReservation.__table__).values(
                canonical_email=normalize_email(address).canonical).on_conflict_do_nothing())
        identity = normalize_email(source)
        # Same-canonical aliases are already reserved. A shared marker would incorrectly block the survivor.
        if identity.canonical != normalize_email(survivor).canonical:
            conn.execute(insert(signup.M).values(identity_hash=signup.identity_hash(identity), key_id=signup.devices.key_id(signup.key()),
                reason='merged', generation=str(uuid.uuid4()), created_at=base.now()))
        conn.execute(delete(U).where(U.c.stable_id == row['source']))
        recalculate_votes(conn, result)
        audit(conn, row, actor)
        # Files are immutable: their ownership lives in the backed-up DB, so no file mutation/undo is necessary.
        result.update(actor=actor, backup=backup, completed_at=base.now().isoformat(), reason=row['reason'], recovery=row['recovery'])
        conn.execute(update(M).where(M.c.id == mid).values(status='completed', completed_at=base.now(),
            source_proof=None, survivor_proof=None, receipt_json=json.dumps(result)))
        if conn.exec_driver_sql('PRAGMA foreign_key_check').first():
            raise ValueError('Merge reference validation failed.')
    db.session.expire_all()
    from app import MATCH_INSTANCES, MATCH_INSTANCES_LOCK
    with MATCH_INSTANCES_LOCK:
        for key, match in list(MATCH_INSTANCES.items()):
            if match.data.get('created_by') in (source, survivor):
                MATCH_INSTANCES.pop(key, None)
    return result


def transfer_stable(conn, row, result):
    d = table('identity_device_sightings')
    keys = ('key_id', 'device_hash', 'fingerprint_hash', 'fingerprint_status')
    for item in rows(conn, d, 'user_stable_id', row['source']):
        existing = conn.execute(select(d).where(d.c.user_stable_id == row['survivor'], *[d.c[k] == item[k] for k in keys])).mappings().first()
        if existing:
            conn.execute(update(d).where(d.c.id == existing['id']).values(first_seen=min(existing['first_seen'], item['first_seen']),
                last_seen=max(existing['last_seen'], item['last_seen']), cookie_confirmed=existing['cookie_confirmed'] or item['cookie_confirmed']))
            conn.execute(delete(d).where(d.c.id == item['id']))
        else:
            conn.execute(update(d).where(d.c.id == item['id']).values(user_stable_id=row['survivor']))
    for name in ('identity_network_appeals', 'identity_network_events', 'identity_archive_owners'):
        t = table(name)
        values = {'user_stable_id': row['survivor']}
        if name == 'identity_network_appeals':
            values.update(status='revoked', open_key=None, closed_at=base.now(), expires_at=base.now(), version=t.c.version+1)
        conn.execute(update(t).where(t.c.user_stable_id == row['source']).values(**values))


def revoke(conn, row):
    ids = [row['source'], row['survivor']]
    addresses = [row['source_address'], row['survivor_address']]
    conn.execute(delete(table('active_sessions')).where(table('active_sessions').c.user_id.in_(addresses)))
    fields = ('pending_email', 'pending_email_token', 'pending_email_token_expires', 'email_verify_token',
              'email_verify_token_expires', 'reset_token', 'reset_token_expires', 'pw_change_otp_hash',
              'pw_change_otp_expires', 'pw_change_pending_hash')
    conn.execute(update(U).where(U.c.stable_id.in_(ids)).values(**{k: None for k in fields}))
    c = base.C
    conn.execute(update(c).where(c.c.user_stable_id.in_(ids), c.c.status == 'pending').values(status='cancelled',
        open_key=None, old_token_hash=None, new_token_hash=None, version=c.c.version+1))
    hashes = [signup.identity_hash(normalize_email(address)) for address in addresses]
    conn.execute(update(signup.P).where(signup.P.c.identity_hash.in_(hashes), signup.P.c.status.in_(['pending', 'approved'])).values(
        status='revoked', open_key=None, closed_at=base.now(), expires_at=base.now(), version=signup.P.c.version+1))
    for delivery in conn.execute(select(signup.V)).mappings().all():
        try:
            matches = signup.identity_hash(normalize_email(delivery['email'])) in hashes
        except ValueError:
            matches = delivery['email'] in addresses
        if matches:
            conn.execute(delete(signup.V).where(signup.V.c.id == delivery['id']))


def recalculate_votes(conn, result):
    v, p = table('community_votes'), table('community_posts')
    # Recompute posts whose duplicate votes were removed, including opposite-direction votes.
    for item in result['losses']:
        if item['table'] == 'community_votes':
            # Original source vote IDs are gone; the preview records all source keys,
            # so recompute all post totals safely inside the serialized transaction.
            for post in conn.execute(select(p.c.id)).scalars():
                up = conn.execute(select(func.count()).select_from(v).where(v.c.post_id == post, v.c.value == 1)).scalar_one()
                down = conn.execute(select(func.count()).select_from(v).where(v.c.post_id == post, v.c.value == -1)).scalar_one()
                conn.execute(update(p).where(p.c.id == post).values(vote_count=up, downvote_count=down, score=up-down))
            break


def match_artifacts(conn, address):
    from app import MATCH_INSTANCES, MATCH_INSTANCES_LOCK
    matches = table('matches')
    with MATCH_INSTANCES_LOCK:
        for match in MATCH_INSTANCES.values():
            data = match.data
            owner = conn.execute(select(matches.c.user_id).where(matches.c.id == data.get('match_id'))).scalar_one_or_none()
            if (owner == address or (owner is None and data.get('created_by') == address)) and data.get('current_state') != 'completed':
                raise ValueError('Finish active matches before merging.')
    result = []
    for path in sorted(Path(current_app.config['IDENTITY_EMAIL_MATCH_DIR']).glob('*.json*')):
        if path.is_symlink():
            raise ValueError('Unsafe match artifact; operator review required.')
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
            mid = data.get('match_id')
        except (OSError, ValueError, AttributeError):
            raise ValueError('Unreadable match artifact; operator review required.') from None
        owner = conn.execute(select(matches.c.user_id).where(matches.c.id == mid)).scalar_one_or_none() if mid else None
        if owner == address or (owner is None and data.get('created_by') == address):
            if owner != address or data.get('current_state') != 'completed':
                raise ValueError('Finish and archive saved matches/checkpoints before merging.')
            result.append({'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest()})
    return result


def archive_artifacts(conn, user):
    root = Path(current_app.config['ARCHIVES_FOLDER'])
    pinned = {r['filename']: dict(r) for r in conn.execute(select(table('identity_archive_owners'))).mappings()}
    for path in root.glob('*.zip'):
        pin = pinned.get(path.name)
        owned = pin['user_stable_id'] == user['stable_id'] if pin else f"_{user['id']}_" in path.name
        if not owned:
            continue
        if path.is_symlink():
            raise ValueError('Unsafe archive artifact; operator review required.')
        try:
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        except OSError:
            raise ValueError('Unreadable archive artifact; operator review required.') from None
        if pin and digest != pin['content_hash']:
            raise ValueError('Archive content changed; operator review required.')
    return base.archive_exports(conn, user)


def pool_relinks(conn, losses, addresses):
    """Keep snapshot ratings/stats intact while preserving the retained pool link."""
    moves, snapshots = [], []
    for loss in losses:
        if loss['table'] != 'user_players':
            continue
        source_id, survivor_id = loss['key']['id'], loss['survivor_key']['id']
        for name in ('players', 'auction_players'):
            child = table(name)
            for row in rows(conn, child, 'user_player_id', source_id):
                if name == 'players':
                    teams = table('teams')
                    owner = conn.execute(select(teams.c.user_id).where(teams.c.id == row['team_id'])).scalar_one_or_none()
                else:
                    auctions, seasons, leagues = table('auctions'), table('seasons'), table('leagues')
                    owner = conn.execute(select(leagues.c.user_id).select_from(auctions.join(seasons,
                        auctions.c.season_id == seasons.c.id).join(leagues, seasons.c.league_id == leagues.c.id))
                        .where(auctions.c.id == row['auction_id'])).scalar_one_or_none()
                if owner not in addresses:
                    raise ValueError('A discarded override is referenced by another owner; operator review required.')
                moves.append({'table': name, 'key': primary(row, child), 'source_id': source_id, 'survivor_id': survivor_id})
                snapshots.append({'table': name, 'row': row})
    return moves, snapshots
