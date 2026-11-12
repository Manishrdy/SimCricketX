"""Request-driven, report-only account review. Workers receive no Flask/ORM state."""
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from sqlalchemy import delete, select
from database import db
from database.models import IdentityAccountReport as Report, IdentityAccountResult as Result, User
from utils.identity_rules import InvalidEmail, normalize_email, validate_new_email, NORMALIZATION_VERSION
from utils.identity_scan import evidence_groups, timestamp, RANKS, RANKING_VERSION

RULES_VERSION = 1
LABELS = {
    'malformed_email': 'Malformed email', 'email_policy': 'Fails current new-email policy',
    'canonical_email': 'Canonical-email collision', 'confirmed_device': 'Shared confirmed device',
    'fingerprint': 'Shared fingerprint', 'unverified_email': 'Email not recorded as verified',
    'forced_verification': 'Forced email verification', 'active_ban': 'Active ban',
    'legacy_correction': 'Pending legacy-email correction', 'open_review': 'Open duplicate review',
    'missing_device': 'No usable retained device evidence', 'missing_network': 'No retained network observation',
}
GAPS = {'missing_device', 'missing_network'}


class ReviewUnavailable(ValueError):
    pass


class ReviewBusy(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(' ')


@contextmanager
def scan_lock(path):
    # Same canonical database path across processes; separate from admission locks.
    fd = os.open(str(Path(path).resolve()) + '.account-review.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReviewBusy('An identity review is already running. The previous report is unchanged.') from None
        yield
    finally:
        os.close(fd)


def snapshot(path, configuration, check, progress=lambda *args, **kwargs: None):
    check()
    started = utcnow()
    rows = {}
    with closing(sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.set_progress_handler(lambda: int(time.monotonic() >= check.deadline), 1000)
        conn.execute('BEGIN')
        progress('Reading account snapshot')
        users, groups, invalid, skipped, available, warnings, _ = evidence_groups(
            conn, started, check=check, progress=progress)
        check()
        progress('Checking email policy', skipped_device_rows=skipped)
        for user in users.values():
            stable = user.get('stable_id')
            if not stable or stable in rows:
                raise ReviewUnavailable('Some accounts lack distinct stable IDs. An operator must repair these before every account can be reviewed. The previous report is unchanged.')
            item = rows[stable] = {'findings': {}, 'device': None, 'network': None}
            try:
                normalize_email(user['id'])
            except InvalidEmail as exc:
                item['findings']['malformed_email'] = {'reason': exc.code}
            else:
                try:
                    validate_new_email(user['id'])
                except InvalidEmail as exc:
                    item['findings']['email_policy'] = {'reason': exc.code}
            check()
        progress('Matching duplicate evidence')
        for key, members in groups.items():
            kind = key[0]
            # The second newest member bounds whether ANY other account still
            # supports a match. No pair expansion, even for huge shared groups.
            newest = sorted((last for _, last in members.values() if last), reverse=True)[:2]
            for uid, (first, last) in members.items():
                item = rows[users[uid]['stable_id']]
                if last and (not item['device'] or last > timestamp(item['device']['last_seen'])):
                    item['device'] = {'last_seen': last.isoformat(), 'expires_at': (last+timedelta(days=180)).isoformat()}
                if len(members) > 1:
                    finding = {}
                    if kind != 'canonical_email':
                        expiry = min(last, newest[1])+timedelta(days=180)
                        finding = {'expires_at': expiry.isoformat(), 'first_seen': first.isoformat(), 'last_seen': last.isoformat()}
                    old = item['findings'].get(kind)
                    if old is None or finding.get('expires_at', '') > old.get('expires_at', ''):
                        item['findings'][kind] = finding
                check()
        progress('Checking account status and open reviews')
        for row in conn.execute('SELECT stable_id,email_verified,force_email_verify,is_banned,banned_until FROM users'):
            check()
            item = rows[row['stable_id']]
            item['email_verified'] = bool(row['email_verified'])
            item['force_email_verify'] = bool(row['force_email_verify'])
            for code, enabled in [('unverified_email', not row['email_verified']), ('forced_verification', row['force_email_verify'])]:
                if enabled:
                    item['findings'][code] = {}
            until = timestamp(row['banned_until'])
            if row['is_banned'] and (not row['banned_until'] or (until and until > started)):
                item['findings']['active_ban'] = {'until': until.isoformat() if until else None}
        for row in conn.execute("SELECT subject FROM identity_campaign_members WHERE status='pending'"):
            check()
            if row['subject'] in rows:
                rows[row['subject']]['findings']['legacy_correction'] = {}
        for row in conn.execute("SELECT left_id,right_id FROM identity_review_cases WHERE status IN ('open','investigating')"):
            check()
            for stable in row:
                if stable in rows:
                    rows[stable]['findings']['open_review'] = {}
        progress('Reading retained network observations')
        for row in conn.execute('SELECT user_stable_id,state,created_at,expires_at FROM identity_network_events ORDER BY created_at,id'):
            observed, expires = timestamp(row['created_at']), timestamp(row['expires_at'])
            if row['user_stable_id'] in rows and observed and expires and observed <= started < expires and observed < expires:
                previous = rows[row['user_stable_id']]['network']
                if previous and timestamp(previous['observed_at']) > observed:
                    continue
                rows[row['user_stable_id']]['network'] = {'state': row['state'], 'observed_at': observed.isoformat(), 'expires_at': expires.isoformat()}
            check()
        blocked = conn.execute('SELECT COUNT(*) FROM identity_signup_attempts WHERE expires_at > ?', (stamp(started),)).fetchone()[0]
        progress('Finalizing account results', processed=0, findings=0)
        total = len(rows)
        findings = device_accounts = network_accounts = 0
        next_update = time.monotonic() + 0.25
        for processed, item in enumerate(rows.values(), 1):
            check()
            findings += bool(item['findings'])
            device_accounts += bool(item['device'])
            network_accounts += bool(item['network'])
            if not item['device']:
                item['findings']['missing_device'] = {}
            if not item['network']:
                item['findings']['missing_network'] = {}
            now = time.monotonic()
            if processed == total or now >= next_update:
                progress('Finalizing account results', processed=processed, findings=findings)
                next_update = now + 0.25
        metadata = {'accounts': total, 'blocked_signup_groups': blocked,
                    'device_accounts': device_accounts, 'network_accounts': network_accounts,
                    'finding_accounts': findings,
                    'invalid_emails': invalid, 'skipped_device_rows': skipped,
                    'device_schema_available': available, 'warnings': warnings,
                    'normalization_version': NORMALIZATION_VERSION, 'ranking_version': RANKING_VERSION,
                    'configuration': configuration}
        check()
    return started, rows, metadata


class Deadline:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def __call__(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Identity review timed out. The previous report is unchanged.')


def run(path, actor, configuration, seconds=60, *, progress=None):
    """Native-thread entry: snapshot then atomically replace only report tables."""
    check = Deadline(seconds)
    path = Path(path).resolve(strict=True)
    with scan_lock(path):
        if progress is None:
            started, rows, metadata = snapshot(path, configuration, check)
        else:
            started, rows, metadata = snapshot(path, configuration, check, progress)
            progress('Saving report', device_accounts=metadata['device_accounts'],
                     network_accounts=metadata['network_accounts'],
                     blocked_signup_groups=metadata['blocked_signup_groups'])
        check()
        conn = sqlite3.connect(str(path), timeout=1)
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            check()
            # Deletions during the snapshot must not resurrect account rows.
            existing = {r[0] for r in conn.execute('SELECT stable_id FROM users')}
            conn.execute('DELETE FROM identity_account_results')
            conn.execute('DELETE FROM identity_account_reports')
            completed = utcnow()
            conn.execute('INSERT INTO identity_account_reports VALUES (1,?,?,?,?,?,?)',
                         (stamp(started), stamp(completed), stamp(completed+timedelta(days=30)), actor, RULES_VERSION, json.dumps(metadata)))
            conn.executemany('INSERT INTO identity_account_results (stable_id,report_id,payload) VALUES (?,1,?)',
                             ((stable, json.dumps(item)) for stable, item in rows.items() if stable in existing))
            check()
            completed = utcnow()
            conn.execute('UPDATE identity_account_reports SET completed_at=?,expires_at=? WHERE id=1',
                         (stamp(completed), stamp(completed+timedelta(days=30))))
            conn.commit()
        finally:
            conn.close()


def database_path():
    path = db.engine.url.database
    if not path or path == ':memory:':
        raise ValueError('Identity review requires a file-backed SQLite database.')
    return path


def start(path, actor, configuration, **kwargs):
    # gevent's native thread pool keeps SQLite and grouping off the request hub.
    from gevent import get_hub
    return get_hub().threadpool.spawn(run, path, actor, configuration, **kwargs)


def execute(actor, configuration):
    return start(database_path(), actor, configuration).get()


def effective(item, at):
    """Suppress expired evidence in display, filters AND ordering without writes."""
    item['findings'] = {code: detail for code, detail in item['findings'].items()
                        if not detail.get('expires_at') or timestamp(detail['expires_at']) > at}
    for kind in ('device', 'network'):
        if item[kind] and timestamp(item[kind]['expires_at']) <= at:
            item[kind] = None
        if not item[kind]:
            item['findings']['missing_'+kind] = {}
    return item


def read(*, q='', finding='', view='findings', page=1, at=None):
    at = at or utcnow()
    if finding and finding not in LABELS or view not in ('findings', 'all'):
        raise ValueError('Unknown report filter.')
    # Header and results are read together; a concurrent publication cannot mix them.
    with db.engine.connect() as conn:
        conn.exec_driver_sql('BEGIN')
        header = conn.execute(select(Report.__table__).where(Report.expires_at > at.replace(tzinfo=None))).mappings().first()
        if not header:
            return None
        header = dict(header)
        header['metadata'] = json.loads(header.pop('metadata_json'))
        header['actor_label'] = conn.execute(select(User.id).where(User.stable_id == header['actor_id'])).scalar() or header['actor_id']
        query = select(Result.stable_id, Result.payload, User.id).join(User, User.stable_id == Result.stable_id)
        if q:
            query = query.where(User.id.contains(q, autoescape=True) | User.stable_id.contains(q, autoescape=True))
        items = []
        for stable, payload, label in conn.execute(query):
            item = effective(json.loads(payload), at)
            codes = set(item['findings'])
            if finding and finding not in codes or view == 'findings' and not codes:
                continue
            item.update(stable_id=stable, label=label)
            items.append(item)
        items.sort(key=lambda x: (0 if set(x['findings'])-GAPS else 1 if x['findings'] else 2,
                                 min((RANKS[c] for c in x['findings'] if c in RANKS), default=4), x['stable_id']))
        offset = (page-1)*30
        return {'header': header, 'items': items[offset:offset+30], 'total': len(items), 'more': len(items)>offset+30}


def cleanup(conn, at):
    expired = select(Report.id).where(Report.expires_at <= at)
    conn.execute(delete(Result).where(Result.report_id.in_(expired)))
    return conn.execute(delete(Report).where(Report.id.in_(expired))).rowcount
