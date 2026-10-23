"""Transactional review workflow. Review decisions never change user access."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import select, insert, update, delete, or_

from database import db
from database.models import User, IdentityReviewCase, IdentityReviewEvidence, IdentityReviewAction
from utils.identity_scan import scan, timestamp

C = IdentityReviewCase.__table__
E = IdentityReviewEvidence.__table__
A = IdentityReviewAction.__table__
DAYS = 180
STATES = ('open', 'investigating', 'resolved', 'dismissed')
CLOSED = ('resolved', 'dismissed')
TRANSITIONS = {'open': {'investigating', 'dismissed'},
               'investigating': {'resolved', 'dismissed'},
               'resolved': {'open'}, 'dismissed': {'open'}}


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def visible(at):
    return or_(C.c.closed_at.is_(None), C.c.closed_at > at - timedelta(days=DAYS))


def cleanup(conn, at):
    expired = select(C.c.id).where(C.c.closed_at <= at - timedelta(days=DAYS))
    evidence = conn.execute(delete(E).where(or_(E.c.expires_at <= at, E.c.case_id.in_(expired)))).rowcount
    actions = conn.execute(delete(A).where(or_(A.c.expires_at <= at, A.c.case_id.in_(expired)))).rowcount
    cases = conn.execute(delete(C).where(C.c.id.in_(expired))).rowcount
    return {'cases': cases, 'evidence': evidence, 'actions': actions}


def transaction():
    """Caller uses with; BEGIN IMMEDIATE serializes preview validation and writes."""
    return db.engine.begin()


def _report(conn, at):
    report = scan(at=at.replace(tzinfo=timezone.utc), _connection=conn.connection.driver_connection)
    if len(report['pairs']) > 200:
        raise ValueError('More than 200 candidate pairs. Use the operator report to review scope before importing.')
    for pair in report['pairs']:
        ids = [pair[side].get('stable_id') for side in ('left', 'right')]
        if not all(ids) or ids[0] == ids[1]:
            raise ValueError('Some candidates lack distinct stable account IDs. Complete the identity foundation migration first.')
    return report


def digest(conn, report, at):
    # Capture case versions too: another review/import invalidates this preview.
    revisions = [tuple(r) for r in conn.execute(select(C.c.id, C.c.version).where(visible(at)).order_by(C.c.id))]
    data = {'pairs': report['pairs'], 'ranking_version': report['ranking_version'], 'revisions': revisions}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def preview():
    at = now()
    with transaction() as conn:
        conn.exec_driver_sql('BEGIN')
        report = _report(conn, at)
        return report, digest(conn, report, at)


def audit(conn, case_id, actor, action, reason, at, expiry=None):
    conn.execute(insert(A).values(case_id=case_id, actor_id=actor, action=action,
                                 reason=reason, created_at=at, expires_at=expiry or at+timedelta(days=DAYS)))


def import_scan(actor, expected):
    at = now()
    with transaction() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        report = _report(conn, at)
        if digest(conn, report, at) != expected:
            raise ValueError('The scan or review cases changed. Preview again before importing.')
        cleanup(conn, at)
        count = 0
        for pair in report['pairs']:
            left, right = sorted([pair['left']['stable_id'], pair['right']['stable_id']])
            case = conn.execute(select(C).where(C.c.left_id == left, C.c.right_id == right)).mappings().first()
            if case and case['status'] in CLOSED:
                continue  # An import never reopens a completed review or extends its retention.
            if case:
                case_id = case['id']
                conn.execute(update(C).where(C.c.id == case_id).values(version=case['version']+1, updated_at=at))
            else:
                case_id = conn.execute(insert(C).values(left_id=left, right_id=right, status='open',
                    version=1, created_at=at, updated_at=at)).inserted_primary_key[0]
            # Replace current snapshot; removed evidence cannot remain presented as current.
            conn.execute(delete(E).where(E.c.case_id == case_id))
            for item in pair['evidence']:
                expiry = at + timedelta(days=DAYS)
                if item['observations']:
                    expiry = min(expiry, timestamp(item['expires_at']).replace(tzinfo=None))
                conn.execute(insert(E).values(case_id=case_id, reason=item['reason'],
                    payload=json.dumps({'evidence': item, 'data_counts': pair['data_counts']}),
                    captured_at=at, expires_at=expiry))
            audit(conn, case_id, actor, 'scan_import', 'Imported reviewed, revalidated scan evidence.', at)
            count += 1
        return count


def change(case_id, actor, version, action, reason, assignee=''):
    reason = (reason or '').strip()
    if not reason or len(reason) > 2000:
        raise ValueError('Enter a reason or private note of 1–2000 characters.')
    at = now()
    with transaction() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        case = conn.execute(select(C).where(C.c.id == case_id, visible(at))).mappings().first()
        if not case or case['version'] != version:
            raise ValueError('This case changed or expired. Reload before trying again.')
        values = {'version': version+1, 'updated_at': at}
        expiry = case['closed_at'] + timedelta(days=DAYS) if case['closed_at'] else at+timedelta(days=DAYS)
        if action in STATES:
            if action not in TRANSITIONS[case['status']]:
                raise ValueError('That state change is not available for this case.')
            values.update(status=action, closed_at=at if action in CLOSED else None)
            expiry = at+timedelta(days=DAYS)
            if action in CLOSED:
                conn.execute(update(A).where(A.c.case_id == case_id, A.c.expires_at > at).values(expires_at=expiry))
        elif action == 'assign':
            if assignee:
                admin = conn.execute(select(User.stable_id).where(User.stable_id == assignee, User.is_admin.is_(True), User.is_banned.is_(False))).first()
                if not admin:
                    raise ValueError('Choose an available administrator.')
            values['assignee'] = assignee or None
            reason = f"Assignee: {case['assignee'] or 'unassigned'} → {assignee or 'unassigned'}.\n{reason}"
        elif action != 'note':
            raise ValueError('Unknown review action.')
        conn.execute(update(C).where(C.c.id == case_id, C.c.version == version).values(**values))
        audit(conn, case_id, actor, action, reason, at, expiry)
