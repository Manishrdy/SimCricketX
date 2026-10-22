"""Ingress security tests use a minimal Flask app: no accounts or database."""
import pytest
from flask import Flask, request
from utils.client_ip import TrustedProxyMiddleware, get_client_ip


def client(enabled=True, cidrs=None):
    app = Flask(__name__)
    app.wsgi_app = TrustedProxyMiddleware(
        app.wsgi_app, enabled=enabled,
        trusted_cidrs=['127.0.0.1/32', '::1/128'] if cidrs is None else cidrs,
    )

    @app.get('/')
    def info():
        return dict(ip=get_client_ip(), remote=request.remote_addr,
                    scheme=request.scheme, host=request.host,
                    peer=request.environ['simcricketx.transport_peer'],
                    xff=request.headers.get('X-Forwarded-For'),
                    cf=request.headers.get('CF-Connecting-IP'))

    return app.test_client()


@pytest.mark.parametrize('peer', ['198.51.100.9', '2001:db8::9'])
def test_untrusted_peer_cannot_spoof_identity_or_url(peer):
    result = client().get('/', environ_overrides={'REMOTE_ADDR': peer}, headers={
        'X-Real-IP': '192.0.2.7', 'X-Forwarded-For': '192.0.2.8',
        'CF-Connecting-IP': '192.0.2.10', 'Forwarded': 'for=192.0.2.11',
        'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Proto': 'https',
    }).json
    assert result['ip'] == result['remote'] == peer
    assert result['scheme'] == 'http'
    assert result['host'] == 'localhost'
    assert result['xff'] is result['cf'] is None


@pytest.mark.parametrize('peer', ['127.0.0.1', '::1', '::ffff:127.0.0.1'])
@pytest.mark.parametrize('visitor', ['192.0.2.7', '2001:db8::7'])
def test_nginx_normalized_header_wins_over_competing_chain(peer, visitor):
    result = client().get('/', environ_overrides={'REMOTE_ADDR': peer}, headers={
        'X-Real-IP': visitor, 'X-Forwarded-For': '203.0.113.99, 173.245.48.1',
        'CF-Connecting-IP': '203.0.113.98', 'X-Forwarded-Proto': 'https',
        'X-Forwarded-Host': 'evil.example',
    }).json
    assert result['ip'] == result['remote'] == visitor
    assert result['scheme'] == 'https'
    assert result['host'] == 'localhost'
    assert result['peer'] in ('127.0.0.1', '::1')


@pytest.mark.parametrize('value', ['', 'bad', '192.0.2.1, 192.0.2.2',
                                    '192.0.2.1:123', '[2001:db8::1]', 'fe80::1%lo0'])
def test_malformed_normalized_ip_falls_back_to_transport(value):
    result = client().get('/', headers={'X-Real-IP': value,
                                        'X-Forwarded-For': '192.0.2.5'}).json
    assert result['ip'] == '127.0.0.1'


def test_missing_normalized_header_never_falls_back_to_xff():
    assert client().get('/', headers={'X-Forwarded-For': '192.0.2.5'}).json['ip'] == '127.0.0.1'


@pytest.mark.parametrize('enabled,cidrs', [(False, ['127.0.0.1/32']), (True, [])])
def test_disabled_or_unconfigured_trust_ignores_headers(enabled, cidrs):
    result = client(enabled, cidrs).get('/', headers={
        'X-Real-IP': '192.0.2.7', 'X-Forwarded-Proto': 'https'}).json
    assert result['ip'] == '127.0.0.1'
    assert result['scheme'] == 'http'


@pytest.mark.parametrize('proto', ['https,http', 'javascript', ''])
def test_invalid_forwarded_protocol_ignored(proto):
    assert client().get('/', headers={'X-Forwarded-Proto': proto}).json['scheme'] == 'http'


@pytest.mark.parametrize('cidrs', ['127.0.0.1', ['garbage'], ['0.0.0.0/0'], ['::/0']])
def test_bad_proxy_configuration_rejected(cidrs):
    with pytest.raises(ValueError):
        client(cidrs=cidrs)


def test_no_request_context():
    assert get_client_ip() == ''


def test_unparseable_transport_never_trusted():
    result = client().get('/', environ_overrides={'REMOTE_ADDR': 'bad'},
                          headers={'X-Real-IP': '192.0.2.1'}).json
    assert result['ip'] == ''


def test_real_app_and_auth_share_normalized_identity(app):
    from ipaddress import ip_network
    from auth.user_auth import get_client_ip as auth_client_ip
    assert isinstance(app.wsgi_app, TrustedProxyMiddleware)
    app.wsgi_app.enabled = True
    app.wsgi_app.networks = (ip_network('127.0.0.1/32'),)

    @app.get('/_test_ingress_identity')
    def probe():
        return dict(shared=get_client_ip(), auth=auth_client_ip(),
                    remote=request.remote_addr, scheme=request.scheme)

    result = app.test_client().get('/_test_ingress_identity', headers={
        'X-Real-IP': '198.51.100.42', 'X-Forwarded-For': '192.0.2.1',
        'X-Forwarded-Proto': 'https',
    })
    assert result.status_code == 200
    assert result.json == dict(shared='198.51.100.42', auth='198.51.100.42',
                               remote='198.51.100.42', scheme='https')


def test_socketio_handshake_receives_normalized_peer(app, monkeypatch):
    from ipaddress import ip_network
    import json
    app.wsgi_app.enabled = True
    app.wsgi_app.networks = (ip_network('127.0.0.1/32'),)
    engine = app.extensions['socketio'].server.eio
    original = engine.handlers['connect']
    observed = {}

    def capture(sid, environ):
        observed['ip'] = environ['REMOTE_ADDR']
        observed['peer'] = environ['simcricketx.transport_peer']
        return original(sid, environ)

    monkeypatch.setitem(engine.handlers, 'connect', capture)
    response = app.test_client().get('/socket.io/?EIO=4&transport=polling', headers={
        'X-Real-IP': '2001:db8::42', 'X-Forwarded-For': '192.0.2.99',
    })
    assert response.status_code == 200
    sid = json.loads(response.get_data(as_text=True)[1:])['sid']
    try:
        assert observed == {'ip': '2001:db8::42', 'peer': '127.0.0.1'}
    finally:
        # No polling client remains to drain the close queue in this test.
        engine.sockets[sid].close(wait=False, abort=True)
        engine.sockets.pop(sid, None)
