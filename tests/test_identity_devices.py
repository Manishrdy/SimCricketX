"""Device collection uses shared QA identities and isolated test databases."""
import json
from datetime import timedelta, timezone
from unittest.mock import patch

import pytest
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select

from database import db
from database.models import IdentityDeviceSighting as Sighting
from migrations.add_identity_devices import run_migration
from migrations.verify import model_drift
from utils import identity_devices as devices

SIGNALS = {'language': 'en-US', 'timezone': 'America/New_York', 'screen': [1920, 1080], 'platform': 'MacIntel'}
KEY = 'test-device-key-only-not-for-production-123456'


@pytest.fixture(autouse=True)
def enable_devices(app):
    app.config['IDENTITY_DEVICE_KEY'] = KEY
    app.config['IDENTITY_DEVICE_PREVIOUS_KEY'] = ''


def rows():
    return db.session.execute(select(Sighting)).scalars().all()


def post(client, payload=SIGNALS):
    return client.post('/api/identity/device', json={'fingerprint': payload})


def test_cookie_flags_and_disclosure(client):
    response = client.get('/login')
    cookie = next(h for h in response.headers.getlist('Set-Cookie') if h.startswith(devices.COOKIE))
    for flag in ('Secure', 'HttpOnly', 'SameSite=Lax', 'Max-Age=15552000', 'Path=/'):
        assert flag in cookie
    assert 'Domain=' not in cookie
    assert b'Device privacy details' in response.data
    assert b'Device privacy details' in client.get('/register').data
    assert client.get('/privacy/device-data').status_code == 200
    assert not rows()


def test_disabled_no_cookie_or_script(app, client):
    app.config['IDENTITY_DEVICE_KEY'] = ''
    response = client.get('/login')
    assert devices.COOKIE not in ''.join(response.headers.getlist('Set-Cookie'))
    assert b'identity_device.js' not in response.data
    assert b'data-device-fingerprint' not in response.data


def test_authenticated_collection_once_per_session(authenticated_client, regular_user):
    authenticated_client.get('/')
    assert post(authenticated_client).json['status'] == 'collected'
    saved = rows()[0]
    assert saved.user_stable_id == regular_user.stable_id
    assert saved.cookie_confirmed and saved.fingerprint_status == 'ok'
    assert len(saved.device_hash) == len(saved.fingerprint_hash) == 64
    assert 'MacIntel' not in repr(saved.__dict__)
    assert post(authenticated_client).json['status'] == 'already_collected'
    assert len(rows()) == 1
    assert b'data-collect-session' not in authenticated_client.get('/').data


@pytest.mark.parametrize('payload,status', [(None, 'missing'), ({'extra': 'bad'}, 'invalid'), (SIGNALS, 'ok')])
def test_missing_invalid_signals_do_not_block(authenticated_client, payload, status):
    assert post(authenticated_client, payload).status_code == 200
    assert rows()[0].fingerprint_status == status
    assert authenticated_client.get('/').status_code == 200


def test_no_cookie_recorded_as_unconfirmed(authenticated_client):
    assert post(authenticated_client).status_code == 200
    assert not rows()[0].cookie_confirmed


def test_cookie_tampering_replaced(authenticated_client):
    authenticated_client.set_cookie(devices.COOKIE, 'invented-cookie')
    post(authenticated_client)
    assert not rows()[0].cookie_confirmed
    assert authenticated_client.get_cookie(devices.COOKIE).value != 'invented-cookie'


def test_expired_cookie_not_recognized(authenticated_client):
    old = devices.now().replace(tzinfo=timezone.utc).timestamp() - devices.MAX_AGE - 1
    with patch('itsdangerous.timed.time.time', return_value=old):
        cookie = URLSafeTimedSerializer(KEY, salt=devices.SALT).dumps('a' * 64)
    authenticated_client.set_cookie(devices.COOKIE, cookie)
    post(authenticated_client)
    assert not rows()[0].cookie_confirmed


def test_changed_fingerprint_and_cookie_create_distinct_evidence(app, regular_user):
    with app.test_request_context('/login'):
        assert devices.collect(regular_user, SIGNALS)
        changed = dict(SIGNALS, timezone='UTC')
        assert devices.collect(regular_user, changed)
        assert devices.collect(regular_user, changed)
    assert len(rows()) == 2
    assert len({r.device_hash for r in rows()}) == 1
    with app.test_request_context('/login'):
        devices.collect(regular_user, SIGNALS)
    assert len(rows()) == 3
    assert len({r.device_hash for r in rows()}) == 2


def test_retention_boundary_and_cleanup(app, regular_user):
    start = devices.now()
    with app.test_request_context('/login'), patch.object(devices, 'now', return_value=start):
        devices.collect(regular_user, SIGNALS)
    with patch.object(devices, 'now', return_value=start + timedelta(days=180)):
        with db.engine.connect() as conn:
            assert devices.live_sightings(conn) == []
        result = app.test_cli_runner().invoke(args=['purge-device-evidence'])
        assert result.exit_code == 0
        assert 'Deleted 1' in result.output
    assert not rows()


def test_rolling_sighting_retention(app, regular_user):
    start = devices.now()
    with app.test_request_context('/login'):
        with patch.object(devices, 'now', return_value=start):
            devices.collect(regular_user, SIGNALS)
        with patch.object(devices, 'now', return_value=start + timedelta(days=100)):
            devices.collect(regular_user, SIGNALS)
    db.session.expire_all()
    assert rows()[0].first_seen == start
    assert rows()[0].last_seen == start + timedelta(days=100)
    with patch.object(devices, 'now', return_value=start + timedelta(days=180)):
        with db.engine.connect() as conn:
            assert len(devices.live_sightings(conn)) == 1


def test_key_rotation_and_expiry(app, authenticated_client):
    old_key = 'old-test-device-key-which-is-long-enough'
    app.config.update(IDENTITY_DEVICE_PREVIOUS_KEY=old_key,
                      IDENTITY_DEVICE_PREVIOUS_UNTIL=(devices.now() + timedelta(days=30)).replace(tzinfo=timezone.utc).isoformat())
    cookie = URLSafeTimedSerializer(old_key, salt=devices.SALT).dumps('b' * 64)
    authenticated_client.set_cookie(devices.COOKIE, cookie)
    post(authenticated_client)
    assert len(rows()) == 2
    assert all(r.cookie_confirmed for r in rows())
    assert len({r.device_hash for r in rows()}) == 2
    app.config['IDENTITY_DEVICE_PREVIOUS_UNTIL'] = (devices.now() - timedelta(seconds=1)).replace(tzinfo=timezone.utc).isoformat()
    assert devices.keys() == [KEY]


def test_key_validation(app):
    app.config['IDENTITY_DEVICE_KEY'] = 'short'
    with pytest.raises(ValueError):
        devices.keys()


def test_anonymous_and_csrf_denied(app, client, authenticated_client):
    assert client.post('/api/identity/device', json={'fingerprint': SIGNALS}).status_code in (302, 401)
    app.config['WTF_CSRF_ENABLED'] = True
    assert post(authenticated_client).status_code == 400
    assert not rows()


def test_large_payload_rejected(authenticated_client):
    response = authenticated_client.post('/api/identity/device', data='x' * 4097, content_type='application/json')
    assert response.status_code == 413
    assert not rows()


def test_storage_failure_does_not_fail_login_collection(app, regular_user):
    with app.test_request_context('/login'), patch.object(db.engine, 'begin', side_effect=RuntimeError('test')):
        assert not devices.collect(regular_user, SIGNALS, mark_session=True)
        from flask import session
        assert 'identity_device_collected' not in session


def test_impersonation_does_not_link_admin_device(authenticated_client):
    with authenticated_client.session_transaction() as session:
        session['impersonating_from'] = 'qa.admin@example.com'
    assert post(authenticated_client).json['status'] == 'already_collected'
    assert not rows()


def test_migration_idempotent_and_fresh_parity(app):
    with db.engine.begin() as conn:
        Sighting.__table__.drop(conn)
    run_migration(db, app)
    run_migration(db, app)
    assert model_drift(db.engine, db.metadata) == []


def test_fingerprint_validation():
    expected, status = devices.fingerprint(SIGNALS)
    assert status == 'ok'
    assert devices.fingerprint(dict(SIGNALS, screen=[1080, 1920]))[0] == expected
    for changed in ({'screen': [True, 100]}, {'screen': [0, 100]}, {'timezone': '../bad'},
                    {'platform': '<script>'}, {'language': 'x' * 90}):
        assert devices.fingerprint(dict(SIGNALS, **changed))[1] == 'invalid'
    assert devices.fingerprint('x' * 2049)[1] == 'invalid'


def test_real_login_collects_only_after_success(app, client, regular_user):
    from scripts.dev_test_accounts import load_credentials
    credentials = load_credentials()
    password = credentials['accounts']['user1']['password']
    client.get('/login')
    rejected = client.post('/login', data={'email': regular_user.id, 'password': 'incorrect-password',
                                         'device_fingerprint': json.dumps(SIGNALS)})
    assert rejected.status_code == 200
    assert not rows()
    response = client.post('/login', data={'email': regular_user.id, 'password': password,
                                          'device_fingerprint': json.dumps(SIGNALS)})
    assert response.status_code == 302
    assert len(rows()) == 1
    assert rows()[0].fingerprint_status == 'ok'


def test_signup_records_shared_qa_identity(app, client, regular_user, monkeypatch):
    from scripts.dev_test_accounts import load_credentials
    email = regular_user.id
    db.session.delete(regular_user)  # Isolated fixture only; reuse this shared QA identity.
    db.session.commit()
    monkeypatch.setattr('routes.auth_routes.send_verification_email', lambda *args, **kwargs: True)
    password = load_credentials()['accounts']['user1']['password']
    client.get('/register')
    response = client.post('/register', data={
        'email': email, 'display_name': 'QA registration',
        'password': password, 'confirm_password': password,
        'device_fingerprint': json.dumps(SIGNALS),
    })
    assert response.status_code == 302
    assert len(rows()) == 1
    assert rows()[0].cookie_confirmed


def test_collection_failure_does_not_break_real_login(app, client, regular_user, monkeypatch):
    from scripts.dev_test_accounts import load_credentials
    monkeypatch.setattr(devices, 'purge_expired', lambda *args: (_ for _ in ()).throw(RuntimeError('storage failure')))
    response = client.post('/login', data={'email': regular_user.id,
        'password': load_credentials()['accounts']['user1']['password']})
    assert response.status_code == 302
    assert not rows()
    with client.session_transaction() as sess:
        assert sess.get('session_token')
        assert 'identity_device_collected' not in sess


def test_privacy_accessible_during_forced_verification(authenticated_client, regular_user):
    regular_user.force_email_verify = True
    db.session.commit()
    assert authenticated_client.get('/privacy/device-data').status_code == 200


def test_new_signed_in_session_collects_again(authenticated_client):
    post(authenticated_client)
    from database.models import ActiveSession
    with authenticated_client.session_transaction() as sess:
        token = sess['session_token']
    active = ActiveSession.query.filter_by(session_token=token).one()
    active.session_token = 'new-qa-session-token'
    db.session.commit()
    with authenticated_client.session_transaction() as sess:
        sess['session_token'] = 'new-qa-session-token'
    assert post(authenticated_client).json['status'] == 'collected'
