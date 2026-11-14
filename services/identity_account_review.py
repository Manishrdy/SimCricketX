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
FINDING_ORDER = (
    'canonical_email', 'confirmed_device', 'fingerprint', 'open_review',
    'malformed_email', 'email_policy', 'legacy_correction',
    'active_ban', 'forced_verification', 'unverified_email',
    'missing_device', 'missing_network',
)
GROUP_MODES = {
    '': 'List view',
    'finding': 'Finding',
    'category': 'Category',
    'domain': 'Email domain',
    'status': 'Verification status',
}
CATEGORIES = (
    ('duplicate_signals', 'Duplicate & shared-signal findings', 'Shared canonical email, device, fingerprint or open duplicate case',
     {'canonical_email', 'confirmed_device', 'fingerprint', 'open_review'}, 'finding'),
    ('email_policy', 'Email syntax & policy findings', 'Malformed, disposable, plus-tagged or pending legacy remediation',
     {'malformed_email', 'email_policy', 'legacy_correction'}, 'finding'),
    ('account_state', 'Verification & account restrictions', 'Active ban, forced verification or unverified email address',
     {'active_ban', 'forced_verification', 'unverified_email'}, 'finding'),
    ('coverage_gaps', 'Coverage gaps only', 'No duplicate or policy findings; missing retained device or network observations',
     GAPS, 'gap'),
    ('clear', 'Clear accounts', 'No findings or coverage gaps',
     set(), 'clear'),
)


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


class _Job:
    def __init__(self, async_result):
        self._result = async_result

    def ready(self):
        return self._result.ready()

    def get(self):
        ok, payload = self._result.get()
        if not ok:
            raise payload
        return payload


def _run_safely(path, actor, configuration, kwargs):
    try:
        return True, run(path, actor, configuration, **kwargs)
    except Exception as exc:
        return False, exc


def start(path, actor, configuration, **kwargs):
    # gevent's native thread pool keeps SQLite and grouping off the request hub.
    # Capture exceptions as return values inside the worker thread so gevent's
    # ThreadPool does not dump unhandled tracebacks to stderr before .get() runs.
    from gevent import get_hub
    return _Job(get_hub().threadpool.spawn(_run_safely, path, actor, configuration, kwargs))


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


def _build_groups(items, group_by, finding_filter=''):
    if not group_by or not items:
        return []
    groups = []
    if group_by == 'finding':
        codes = [finding_filter] if finding_filter in LABELS else list(FINDING_ORDER)
        for code in codes:
            matched = [item for item in items if code in item['findings']]
            if not matched:
                continue
            kind = 'gap' if code in GAPS else 'finding'
            groups.append({
                'key': code,
                'title': LABELS[code],
                'subtitle': 'Coverage gap' if kind == 'gap' else 'Actionable finding',
                'kind': kind,
                'count': len(matched),
                'actionable_count': sum(1 for i in matched if i['actionable_findings']),
                'items': matched[:50],
                'more_count': max(0, len(matched) - 50),
                'filter_finding': code,
                'filter_q': '',
                'open_default': kind == 'finding' or finding_filter == code,
            })
        clean = [item for item in items if not item['findings']]
        if clean and not finding_filter:
            groups.append({
                'key': 'clear',
                'title': 'No findings or coverage gaps',
                'subtitle': 'Clear accounts',
                'kind': 'clear',
                'count': len(clean),
                'actionable_count': 0,
                'items': clean[:50],
                'more_count': max(0, len(clean) - 50),
                'filter_finding': '',
                'filter_q': '',
                'open_default': not groups,
            })
    elif group_by == 'category':
        buckets = {key: [] for key, *_ in CATEGORIES}
        for item in items:
            codes = set(item['findings'])
            actionable = codes - GAPS
            if actionable & CATEGORIES[0][3]:
                buckets['duplicate_signals'].append(item)
            elif actionable & CATEGORIES[1][3]:
                buckets['email_policy'].append(item)
            elif actionable & CATEGORIES[2][3]:
                buckets['account_state'].append(item)
            elif codes:
                buckets['coverage_gaps'].append(item)
            else:
                buckets['clear'].append(item)
        for key, title, subtitle, _, kind in CATEGORIES:
            matched = buckets[key]
            if not matched:
                continue
            groups.append({
                'key': key,
                'title': title,
                'subtitle': subtitle,
                'kind': kind,
                'count': len(matched),
                'actionable_count': sum(1 for i in matched if i['actionable_findings']),
                'items': matched[:50],
                'more_count': max(0, len(matched) - 50),
                'filter_finding': '',
                'filter_q': '',
                'open_default': kind == 'finding',
            })
    elif group_by == 'domain':
        by_domain = {}
        for item in items:
            by_domain.setdefault(item['domain'], []).append(item)
        ordered = sorted(
            by_domain.items(),
            key=lambda kv: (
                0 if any(i['actionable_findings'] for i in kv[1]) else 1,
                -sum(1 for i in kv[1] if i['actionable_findings']),
                -len(kv[1]),
                kv[0],
            ),
        )
        for domain, matched in ordered:
            act_count = sum(1 for i in matched if i['actionable_findings'])
            kind = 'finding' if act_count else ('gap' if any(i['gap_findings'] for i in matched) else 'clear')
            groups.append({
                'key': domain,
                'title': f'@{domain}' if domain != '(invalid address)' else domain,
                'subtitle': f'{act_count} with actionable findings' if act_count else 'No actionable findings',
                'kind': kind,
                'count': len(matched),
                'actionable_count': act_count,
                'items': matched[:50],
                'more_count': max(0, len(matched) - 50),
                'filter_finding': '',
                'filter_q': f'@{domain}' if domain != '(invalid address)' else '',
                'open_default': act_count > 0 or len(ordered) <= 5,
            })
    elif group_by == 'status':
        status_defs = (
            ('banned', 'Active ban', 'Accounts currently suspended', 'finding',
             lambda i: 'active_ban' in i['findings']),
            ('forced_verify', 'Forced email verification', 'Accounts requiring email verification or legacy correction on sign-in', 'finding',
             lambda i: 'active_ban' not in i['findings'] and (i.get('force_email_verify') or 'legacy_correction' in i['findings'])),
            ('unverified', 'Unverified email', 'Email address not yet recorded as verified', 'finding',
             lambda i: 'active_ban' not in i['findings'] and not (i.get('force_email_verify') or 'legacy_correction' in i['findings']) and not i.get('email_verified')),
            ('verified', 'Verified & active', 'Email verified with no active ban or forced-verification hold', 'clear',
             lambda i: 'active_ban' not in i['findings'] and not (i.get('force_email_verify') or 'legacy_correction' in i['findings']) and bool(i.get('email_verified'))),
        )
        for key, title, subtitle, kind, predicate in status_defs:
            matched = [item for item in items if predicate(item)]
            if not matched:
                continue
            act_count = sum(1 for i in matched if i['actionable_findings'])
            groups.append({
                'key': key,
                'title': title,
                'subtitle': subtitle,
                'kind': 'finding' if act_count else kind,
                'count': len(matched),
                'actionable_count': act_count,
                'items': matched[:50],
                'more_count': max(0, len(matched) - 50),
                'filter_finding': '',
                'filter_q': '',
                'open_default': kind == 'finding' or act_count > 0,
            })
    if groups and not any(g['open_default'] for g in groups):
        groups[0]['open_default'] = True
    return groups


def read(*, q='', finding='', view='findings', group_by='', page=1, at=None):
    at = at or utcnow()
    if group_by == 'none':
        group_by = ''
    if finding and finding not in LABELS or view not in ('findings', 'all') or group_by not in GROUP_MODES:
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
        raw_counts = {code: 0 for code in FINDING_ORDER}
        unfiltered_total = 0
        for stable, payload, label in conn.execute(query):
            item = effective(json.loads(payload), at)
            codes = set(item['findings'])
            if view == 'findings' and not codes:
                continue
            unfiltered_total += 1
            for code in codes:
                if code in raw_counts:
                    raw_counts[code] += 1
            if finding and finding not in codes:
                continue
            actionable = {c: item['findings'][c] for c in FINDING_ORDER if c in item['findings'] and c not in GAPS}
            gaps = {c: item['findings'][c] for c in FINDING_ORDER if c in item['findings'] and c in GAPS}
            domain = label.rsplit('@', 1)[1].lower() if '@' in label else '(invalid address)'
            item.update(stable_id=stable, label=label, actionable_findings=actionable, gap_findings=gaps, domain=domain)
            items.append(item)
        items.sort(key=lambda x: (0 if set(x['findings'])-GAPS else 1 if x['findings'] else 2,
                                 min((RANKS[c] for c in x['findings'] if c in RANKS), default=4), x['stable_id']))
        finding_counts = [
            {'code': code, 'label': LABELS[code], 'count': raw_counts[code], 'is_gap': code in GAPS}
            for code in FINDING_ORDER if raw_counts.get(code)
        ]
        actionable_total = sum(1 for item in items if item['actionable_findings'])
        groups = _build_groups(items, group_by, finding)
        offset = (page-1)*30
        return {
            'header': header,
            'items': items[offset:offset+30],
            'total': len(items),
            'more': len(items) > offset+30,
            'actionable_total': actionable_total,
            'unfiltered_total': unfiltered_total,
            'finding_counts': finding_counts,
            'group_by': group_by,
            'groups': groups,
        }


def cleanup(conn, at):
    expired = select(Report.id).where(Report.expires_at <= at)
    conn.execute(delete(Result).where(Result.report_id.in_(expired)))
    return conn.execute(delete(Report).where(Report.id.in_(expired))).rowcount
