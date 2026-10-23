"""Read-only duplicate evidence report. No application initialization or writes."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
import sqlite3

from utils.identity_rules import InvalidEmail, normalize_email

RANKING_VERSION = 1
RANKS = {'canonical_email': 1, 'confirmed_device': 2, 'fingerprint': 3}
# Counts are deliberately bounded to these known business ownership references.
COUNTS = {'teams': 'user_id', 'matches': 'user_id', 'tours': 'user_id',
          'tournaments': 'user_id', 'leagues': 'user_id', 'user_players': 'user_id'}


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def scan(path=None, *, at=None, _connection=None):
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError('Scan time must include a timezone')
    at = at.astimezone(timezone.utc)
    cutoff = at - timedelta(days=180)
    if _connection is None:
        path = Path(path).resolve(strict=True)
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    else:
        connection = _connection  # Internal caller owns a consistent transaction.
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        if _connection is None:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')  # One consistent snapshot, including committed WAL data.
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def columns(table):
            return {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
        if 'users' not in tables or 'id' not in columns('users'):
            raise ValueError('Database has no supported users table')
        fields = ['id'] + [c for c in ('stable_id',) if c in columns('users')]
        users = {r['id']: dict(r) for r in connection.execute('SELECT ' + ','.join(fields) + ' FROM users')}
        groups = defaultdict(dict)
        invalid = 0
        for uid in users:
            try:
                groups[('canonical_email', normalize_email(uid).canonical)][uid] = (None, None)
            except InvalidEmail:
                invalid += 1
        warnings = []
        stable = defaultdict(list)
        for uid, user in users.items():
            if user.get('stable_id'):
                stable[user['stable_id']].append(uid)
        required = {'user_stable_id', 'key_id', 'device_hash', 'fingerprint_hash',
                    'fingerprint_status', 'cookie_confirmed', 'first_seen', 'last_seen'}
        device_available = 'identity_device_sightings' in tables and required <= columns('identity_device_sightings')
        skipped = 0
        if device_available:
            for row in connection.execute('SELECT ' + ','.join(sorted(required)) + ' FROM identity_device_sightings'):
                first, last = timestamp(row['first_seen']), timestamp(row['last_seen'])
                owners = stable.get(row['user_stable_id'], [])
                if not first or not last or first > last or last > at or len(owners) != 1:
                    skipped += 1
                    continue
                if last <= cutoff:
                    continue
                uid = owners[0]
                for kind, value, eligible in (
                    ('confirmed_device', row['device_hash'], row['cookie_confirmed'] == 1),
                    ('fingerprint', row['fingerprint_hash'], row['fingerprint_status'] == 'ok'),
                ):
                    if not eligible or not value or not row['key_id']:
                        continue
                    group = groups[(kind, row['key_id'], value)]
                    old_first, old_last = group.get(uid, (first, last))
                    group[uid] = (min(first, old_first), max(last, old_last))
        else:
            warnings.append('Device evidence unavailable in this schema; no historical matches inferred.')
        pairs = defaultdict(dict)
        for key, members in groups.items():
            kind = key[0]
            for left, right in combinations(sorted(members), 2):
                evidence = pairs[(left, right)].setdefault(kind, {'reason': kind, 'observations': {}})
                if kind != 'canonical_email':
                    valid_until = (min(members[left][1], members[right][1]) + timedelta(days=180)).isoformat()
                    evidence['expires_at'] = max(evidence.get('expires_at', valid_until), valid_until)
                for uid in (left, right):
                    first, last = members[uid]
                    if first is not None:
                        old = evidence['observations'].get(uid)
                        evidence['observations'][uid] = {
                            'first_seen': min(first.isoformat(), old['first_seen']) if old else first.isoformat(),
                            'last_seen': max(last.isoformat(), old['last_seen']) if old else last.isoformat(),
                        }
        counts = {uid: {} for pair in pairs for uid in pair}
        available_counts = []
        for table, field in COUNTS.items():
            if table in tables and field in columns(table):
                available_counts.append(table)
                for uid in counts:
                    counts[uid][table] = 0
                for uid, count in connection.execute(f'SELECT "{field}", COUNT(*) FROM "{table}" GROUP BY "{field}"'):
                    if uid in counts:
                        counts[uid][table] = count
        results = []
        for (left, right), evidence in pairs.items():
            results.append({'left': users[left], 'right': users[right],
                            'priority': min(RANKS[k] for k in evidence),
                            'evidence': sorted(evidence.values(), key=lambda e: RANKS[e['reason']]),
                            'data_counts': {left: counts[left], right: counts[right]}})
        results.sort(key=lambda p: (p['priority'], p['left']['id'], p['right']['id']))
        return {'ranking_version': RANKING_VERSION, 'generated_at': at.isoformat(),
                'delete_by': (at + timedelta(days=30)).isoformat(),
                'user_count': len(users), 'invalid_email_count': invalid,
                'skipped_device_rows': skipped, 'device_evidence_available': device_available,
                'available_count_tables': available_counts, 'warnings': warnings,
                'notice': 'Review evidence only; no proof of common ownership. Admin-only; delete exports within 30 days.',
                'pairs': results}
    finally:
        connection.row_factory = previous_factory
        if _connection is None:
            connection.close()
