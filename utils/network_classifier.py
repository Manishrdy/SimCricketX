"""Privacy-minimal ProxyCheck v3 adapter and short-lived classification cache."""
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import ipaddress
import json

from flask import current_app
import requests
from gevent import Timeout
from sqlalchemy import select, insert, update, delete
from sqlalchemy.dialects.sqlite import insert as upsert
from sqlalchemy.exc import SQLAlchemyError

from database import db
from database.models import IdentityNetworkCache, IdentityNetworkBudget, IdentityRelayFeed
from services.identity_signup import hashed

C = IdentityNetworkCache.__table__
B = IdentityNetworkBudget.__table__
F = IdentityRelayFeed.__table__
API = 'https://proxycheck.io/v3/'
RELAY_URL = 'https://mask-api.icloud.com/egress-ip-ranges.csv'
FLAGS = ('vpn', 'proxy', 'tor', 'hosting', 'anonymous')


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Classification:
    state: str
    categories: tuple = ()
    source: str = 'unknown'
    checked_at: datetime | None = None
    expires_at: datetime | None = None


def unknown(source='unavailable', at=None):
    at = at or now()
    return Classification('unknown', (), source, at, at+timedelta(minutes=1))


def parse_response(payload, address, at):
    """Only explicit booleans and documented categories affect access."""
    if not isinstance(payload, dict) or payload.get('status') not in ('ok', 'warning'):
        return unknown('provider_error', at)
    result = payload.get(address)
    if not isinstance(result, dict):
        return unknown('invalid_response', at)
    flags, network = result.get('detections'), result.get('network')
    if (not isinstance(flags, dict) or any(type(flags.get(k)) is not bool for k in FLAGS)
            or not isinstance(network, dict) or network.get('type') not in ('Residential', 'Business', 'Wireless', 'Hosting', None)):
        return unknown('invalid_response', at)
    categories = tuple(k for k in FLAGS if flags[k])
    if network.get('type') == 'Hosting' and 'hosting' not in categories:
        categories += ('hosting',)
    if not categories and network.get('type') is None:
        return unknown('incomplete_response', at)
    state = 'flagged' if categories else 'clean'
    expiry = at+timedelta(minutes=5 if categories else 15)
    history = result.get('detection_history')
    history = history.get('anonymous') if isinstance(history, dict) else None
    if isinstance(history, dict) and history.get('date') is not None:
        try:
            date = datetime.fromisoformat(history['date'].replace('Z', '+00:00'))
            if date.tzinfo is None:
                raise ValueError()
            date = date.astimezone(timezone.utc).replace(tzinfo=None)
            if state == 'flagged' and history.get('delisted') is False:
                if date <= at:
                    return unknown('stale_response', at)
                expiry = min(expiry, date)
        except (ValueError, TypeError, AttributeError):
            return unknown('invalid_response', at)
    return Classification(state, categories, 'proxycheck', at, expiry)


def _download(url, *, params=None, limit=65536, seconds=2):
    # Fixed HTTPS destinations only. Do not log exceptions (request URLs contain
    # the API key), follow redirects, submit account details or retry requests.
    with Timeout(seconds, TimeoutError('Network lookup timed out')):
        with requests.get(url, params=params, timeout=(min(.75, seconds), min(1.25, seconds)),
                          allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError('Remote response unavailable')
            content = bytearray()
            for chunk in response.iter_content(4096):
                content.extend(chunk)
                if len(content) > limit:
                    raise ValueError('Remote response too large')
            return bytes(content)


def provider(address, at):
    api_key = current_app.config.get('PROXYCHECK_API_KEY', '')
    if not api_key:
        return unknown('not_configured', at)
    try:
        payload = json.loads(_download(API+address, params={'key': api_key, 'tag': '0', 'ver': '24-June-2026'}))
        return parse_response(payload, address, at)
    except (requests.RequestException, TimeoutError, ValueError, UnicodeError):
        return unknown('provider_error', at)


def relay_match(conn, address, at):
    row = conn.execute(select(F).where(F.c.id == 1, F.c.imported_at > at-timedelta(hours=48), F.c.imported_at <= at)).mappings().first()
    if not row:
        return None
    try:
        # Compiled ranges are process-local, keyed by the imported checksum.
        cache = current_app.extensions.setdefault('identity_relay_ranges', {})
        if cache.get('checksum') != row['checksum']:
            cache.update(checksum=row['checksum'], networks=tuple(ipaddress.ip_network(n) for n in json.loads(row['networks'])))
        ip = ipaddress.ip_address(address)
        if any(ip.version == n.version and ip in n for n in cache['networks']):
            return Classification('flagged', ('private_relay',), 'apple_feed', at,
                                  min(at+timedelta(minutes=5), row['imported_at']+timedelta(hours=48)))
    except (ValueError, TypeError):
        return None
    return None


def classify(address):
    at = now()
    try:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return unknown('non_public', at)
        address = str(ip)
        ip_hash = hashed('network-ip-v1', address)
        with db.engine.begin() as conn:
            relay = relay_match(conn, address, at)
            if relay:
                return relay
            row = conn.execute(select(C).where(C.c.ip_hash == ip_hash, C.c.expires_at > at, C.c.checked_at <= at)).mappings().first()
            if row:
                return Classification(row['state'], tuple(json.loads(row['categories'])), row['source'], row['checked_at'], row['expires_at'])
        if not current_app.config.get('PROXYCHECK_API_KEY'):
            return unknown('not_configured', at)
        # Reserve a quota slot and a short pending cache entry under the write
        # lock, then release it before network I/O. Never spend beyond free cap.
        with db.engine.begin() as conn:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            row = conn.execute(select(C).where(C.c.ip_hash == ip_hash, C.c.expires_at > at)).mappings().first()
            if row:
                return Classification(row['state'], tuple(json.loads(row['categories'])), row['source'], row['checked_at'], row['expires_at'])
            day = at.date().isoformat()
            conn.execute(upsert(B).values(day=day, attempts=0, last_status='ready', updated_at=at).on_conflict_do_nothing())
            budget = conn.execute(select(B).where(B.c.day == day)).mappings().one()
            if budget['attempts'] >= 1000:
                conn.execute(update(B).where(B.c.day == day).values(last_status='quota_exhausted', updated_at=at))
                return unknown('quota_exhausted', at)
            conn.execute(update(B).where(B.c.day == day).values(attempts=B.c.attempts+1, last_status='pending', updated_at=at))
            pending = dict(ip_hash=ip_hash, state='unknown', categories='[]', source='pending', checked_at=at, expires_at=at+timedelta(seconds=3))
            conn.execute(upsert(C).values(**pending).on_conflict_do_update(index_elements=['ip_hash'], set_=pending))
        result = provider(address, at)
        with db.engine.begin() as conn:
            conn.execute(update(C).where(C.c.ip_hash == ip_hash, C.c.checked_at == at).values(
                state=result.state, categories=json.dumps(result.categories), source=result.source,
                checked_at=result.checked_at, expires_at=result.expires_at))
            conn.execute(update(B).where(B.c.day == day).values(last_status=result.source if result.state == 'unknown' else result.state, updated_at=now()))
        return result
    except (ValueError, TypeError, SQLAlchemyError):
        # This fail-open applies only to classification, never authentication.
        current_app.logger.warning('Network classification unavailable; treating this lookup as unknown')
        return unknown('storage_or_config', at)


def import_relay_csv(content, at=None):
    at = at or now()
    if not isinstance(content, bytes) or not 1 <= len(content) <= 5_000_000:
        raise ValueError('Invalid relay feed size')
    networks = set()
    try:
        for row in csv.reader(io.StringIO(content.decode('utf-8-sig'))):
            if not row:
                continue
            # Apple's feed has no header; other formats are deliberately rejected.
            n = ipaddress.ip_network(row[0].strip(), strict=True)
            if not n.is_global or n.prefixlen < (8 if n.version == 4 else 16):
                raise ValueError()
            networks.add(str(n))
            if len(networks) > 100000:
                raise ValueError()
    except (ValueError, UnicodeError, csv.Error):
        raise ValueError('Malformed relay feed; previous feed preserved') from None
    if not networks:
        raise ValueError('Empty relay feed; previous feed preserved')
    values = dict(id=1, networks=json.dumps(sorted(networks)), imported_at=at,
                  checksum=hashlib.sha256(content).hexdigest())
    with db.engine.begin() as conn:
        conn.execute(upsert(F).values(**values).on_conflict_do_update(index_elements=['id'], set_=values))
    return len(networks)


def cleanup(conn, at):
    conn.execute(delete(C).where(C.c.expires_at <= at))
    conn.execute(delete(B).where(B.c.day < (at-timedelta(days=1)).date().isoformat()))
