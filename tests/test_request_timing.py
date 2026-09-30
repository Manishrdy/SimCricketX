import json
from unittest.mock import patch

from flask import Flask, abort
from sqlalchemy import create_engine, text

from middleware.request_timing import install_request_timing


def test_correlation_privacy_queries_and_statuses():
    app = Flask(__name__)
    install_request_timing(app)
    engine = create_engine('sqlite://')

    @app.get('/reset/<token>')
    def page(token):
        with engine.connect() as connection:
            connection.execute(text('select 1'))
            try:
                connection.execute(text('select * from missing_table'))
            except Exception:
                pass
        return 'ok'

    @app.get('/denied')
    def denied():
        abort(403)

    @app.get('/broken')
    def broken():
        raise RuntimeError('private-exception-message')

    with patch('middleware.request_timing.logger.info') as log:
        client = app.test_client()
        response = client.get('/reset/SECRET?password=SECRET', headers={
            'X-Request-ID': 'a' * 32, 'CF-Ray': 'b' * 16 + '-ATL',
            'Cookie': 'session=SECRET',
        })
        record = json.loads(log.call_args.args[0])
        assert response.headers['X-Request-ID'] == record['request_id'] == 'a' * 32
        assert record['route'] == '/reset/<token>'
        assert record['db_query_count'] == 2
        assert record['duration_ms'] >= record['db_duration_ms'] >= 0
        assert record['cf_ray'] == 'b' * 16 + '-ATL'
        assert 'SECRET' not in log.call_args.args[0]
        for path, status in [('/denied', 403), ('/missing/SECRET', 404), ('/broken', 500)]:
            before = log.call_count
            response = client.get(path, headers={'X-Request-ID': 'untrusted', 'CF-Ray': 'SECRET'})
            record = json.loads(log.call_args.args[0])
            assert log.call_count == before + 1
            assert response.status_code == record['status'] == status
            assert len(record['request_id']) == 32
            assert record['cf_ray'] is None
            assert 'SECRET' not in log.call_args.args[0]
            assert 'private-exception-message' not in log.call_args.args[0]
    engine.dispose()


def test_early_rejection_and_logger_failure_do_not_break_response():
    app = Flask(__name__)
    install_request_timing(app)

    @app.before_request
    def reject():
        return 'blocked', 429

    with patch('middleware.request_timing.logger.info', side_effect=OSError('full')):
        response = app.test_client().get('/')
    assert response.status_code == 429
    assert len(response.headers['X-Request-ID']) == 32


def test_real_app_public_routes(client):
    with patch('middleware.request_timing.logger.info') as log:
        for path in ['/', '/login', '/register', '/community']:
            before = log.call_count
            response = client.get(path)
            record = json.loads(log.call_args.args[0])
            assert response.status_code in (200, 302, 308)
            assert log.call_count == before + 1
            assert record['request_id'] == response.headers['X-Request-ID']
            assert record['status'] == response.status_code
            assert record['duration_ms'] >= 0
