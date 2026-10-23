"""Optional first-party evidence collection; never an access-control decision."""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
from flask import current_app, jsonify, render_template, request, session
from flask_login import current_user, login_required
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from database import db
from database.models import IdentityDeviceSighting

DAYS = 180
MAX_AGE = DAYS * 86400
COOKIE = '__Host-scx_device'
SALT = 'scx-device-cookie-v1'


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def keys():
    """Current key and optional fixed-expiry predecessor, never log values."""
    config = current_app.config
    key = config.get('IDENTITY_DEVICE_KEY', '')
    if not key:
        return []
    if not isinstance(key, str) or len(key.encode()) < 32:
        raise ValueError('IDENTITY_DEVICE_KEY must contain at least 32 bytes')
    result = [key]
    previous = config.get('IDENTITY_DEVICE_PREVIOUS_KEY', '')
    if previous:
        expiry = datetime.fromisoformat(config.get('IDENTITY_DEVICE_PREVIOUS_UNTIL', '').replace('Z', '+00:00'))
        if expiry.tzinfo is None:
            raise ValueError('Previous device key expiry must include a timezone')
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        if len(previous.encode()) < 32 or expiry > now() + timedelta(days=DAYS):
            raise ValueError('Invalid previous device key or expiry beyond 180 days')
        if expiry > now() and previous != key:
            result.append(previous)
    return result


def digest(key, purpose, value):
    return hmac.new(key.encode(), (purpose + '\0' + value).encode(), hashlib.sha256).hexdigest()


def key_id(key):
    return digest(key, 'key-id', '')[:16]


def fingerprint(raw):
    """Canonicalize only the approved signals; discard raw values after hashing."""
    if not raw:
        return '', 'missing'
    try:
        if isinstance(raw, str):
            if len(raw) > 2048:
                raise ValueError()
            raw = json.loads(raw)
        if not isinstance(raw, dict) or set(raw) != {'language', 'timezone', 'screen', 'platform'}:
            raise ValueError()
        language, tz, platform, screen = (raw[k] for k in ('language', 'timezone', 'platform', 'screen'))
        if not isinstance(language, str) or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,4}', language):
            raise ValueError()
        if not isinstance(tz, str) or len(tz) > 80:
            raise ValueError()
        ZoneInfo(tz)
        if not isinstance(platform, str) or not re.fullmatch(r'[A-Za-z0-9 _.-]{1,64}', platform):
            raise ValueError()
        if not isinstance(screen, list) or len(screen) != 2 or any(type(n) is not int or not 1 <= n <= 32768 for n in screen):
            raise ValueError()
        return json.dumps({'language': language.lower(), 'timezone': tz,
                           'platform': platform.lower(), 'screen': sorted(screen)}, sort_keys=True, separators=(',', ':')), 'ok'
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return '', 'invalid'


def device_token():
    cache_key = 'scx.identity_device_token'
    if cache_key in request.environ:
        return request.environ[cache_key]
    token, confirmed = None, False
    cookie = request.cookies.get(COOKIE, '')
    if len(cookie) <= 1024:
        for key in keys():
            try:
                candidate = URLSafeTimedSerializer(key, salt=SALT).loads(cookie, max_age=MAX_AGE)
                if isinstance(candidate, str) and re.fullmatch(r'[0-9a-f]{64}', candidate):
                    token, confirmed = candidate, True
                    break
            except BadSignature:
                pass
    request.environ[cache_key] = (token or secrets.token_hex(32), confirmed)
    return request.environ[cache_key]


def session_scope():
    # HMAC stored only in the signed session cookie, never the evidence table.
    return digest(keys()[0], 'session-scope', str(session.get('session_token', session.get('_id', ''))))


def needs_collection():
    if not keys() or not current_user.is_authenticated or session.get('impersonating_from'):
        return False
    marker = session.get('identity_device_collected', {})
    return not isinstance(marker, dict) or marker.get('scope') != session_scope() or marker.get('until', 0) <= now().replace(tzinfo=timezone.utc).timestamp()


def purge_expired(conn, at=None):
    cutoff = (at or now()) - timedelta(days=DAYS)
    return conn.execute(delete(IdentityDeviceSighting).where(IdentityDeviceSighting.last_seen <= cutoff)).rowcount


def live_sightings(conn, **filters):
    """Expiry-aware evidence lookup; never use an unfiltered table for decisions."""
    query = select(IdentityDeviceSighting.__table__).where(IdentityDeviceSighting.last_seen > now() - timedelta(days=DAYS))
    for name, value in filters.items():
        if name not in {'user_stable_id', 'key_id', 'device_hash', 'fingerprint_hash'}:
            raise ValueError('Unsupported evidence filter')
        query = query.where(getattr(IdentityDeviceSighting, name) == value)
    return conn.execute(query).mappings().all()


def collect(user, raw, mark_session=False):
    """Call only after successful account creation/authentication commits.

    Independent transaction: evidence failure must not roll back authentication.
    No identity or raw request data is included in failure logs.
    """
    if not keys() or session.get('impersonating_from'):
        return False
    if user is None or not user.stable_id:
        return False
    token, confirmed = device_token()
    canonical, status = fingerprint(raw)
    at = now()
    table = IdentityDeviceSighting.__table__
    try:
        with db.engine.begin() as conn:
            purge_expired(conn, at)
            for key in keys():
                values = dict(user_stable_id=user.stable_id, key_id=key_id(key),
                              device_hash=digest(key, 'device', token),
                              fingerprint_hash=digest(key, 'fingerprint-v1', canonical) if canonical else '',
                              fingerprint_status=status, cookie_confirmed=confirmed,
                              first_seen=at, last_seen=at)
                statement = insert(table).values(**values)
                conn.execute(statement.on_conflict_do_update(
                    index_elements=['user_stable_id', 'key_id', 'device_hash', 'fingerprint_hash', 'fingerprint_status'],
                    set_={'last_seen': at, 'cookie_confirmed': confirmed}))
        if mark_session:
            session['identity_device_collected'] = {'scope': session_scope(), 'until': (at + timedelta(days=DAYS)).replace(tzinfo=timezone.utc).timestamp()}
        return True
    except Exception:
        current_app.logger.warning('Device evidence could not be saved; collection remains retryable')
        return False


def init_app(app, limiter):
    for name in ('IDENTITY_DEVICE_KEY', 'IDENTITY_DEVICE_PREVIOUS_KEY', 'IDENTITY_DEVICE_PREVIOUS_UNTIL'):
        app.config.setdefault(name, os.environ.get(name, ''))
    with app.app_context():
        keys()  # Reject invalid configuration before serving requests.

    @app.context_processor
    def device_context():
        return {'identity_device_enabled': bool(keys()), 'identity_device_collect_session': needs_collection()}

    @app.after_request
    def device_cookie(response):
        eligible = request.endpoint in {'login', 'register', 'identity_device_collect'} or (
            current_user.is_authenticated and response.mimetype == 'text/html')
        if keys() and eligible and response.status_code < 400:
            token, _ = device_token()
            signed = URLSafeTimedSerializer(keys()[0], salt=SALT).dumps(token)
            response.set_cookie(COOKIE, signed, max_age=MAX_AGE, secure=True, httponly=True,
                                samesite='Lax', path='/')
            response.headers['Cache-Control'] = 'private, no-store'
        return response

    @app.post('/api/identity/device')
    @limiter.limit('10 per minute')
    @login_required
    def identity_device_collect():
        if not keys():
            return jsonify(status='disabled'), 200
        if not needs_collection():
            return jsonify(status='already_collected'), 200
        if request.content_length is None or request.content_length > 4096:
            return jsonify(status='invalid_payload'), 413
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'fingerprint'}:
            return jsonify(status='invalid_payload'), 400
        if not collect(current_user, body['fingerprint'], mark_session=True):
            return jsonify(status='unavailable'), 503
        return jsonify(status='collected'), 200

    @app.get('/privacy/device-data')
    def identity_device_privacy():
        return render_template('device_privacy.html')

    @app.cli.command('purge-device-evidence')
    def purge_device_evidence():
        """Delete device evidence whose last observation is at least 180 days old."""
        with db.engine.begin() as conn:
            count = purge_expired(conn)
        click.echo(f'Deleted {count} expired device evidence rows.')
