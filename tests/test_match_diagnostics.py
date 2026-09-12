import json
import sqlite3
from flask import Flask
from database import db
from database.models import MatchDiagnosticEvent
from services.match_diagnostics import record


def test_trace_opt_in_payload_metrics_and_transaction_isolation(tmp_path, monkeypatch):
    app = Flask(__name__)
    path = tmp_path / 'trace.db'
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(path)
    db.init_app(app)
    with app.app_context():
        MatchDiagnosticEvent.__table__.create(db.engine)
        with app.test_request_context('/'):
            monkeypatch.delenv('MATCH_DIAGNOSTICS', raising=False)
            record('test-match', 'engine_result', {'score': 42})
            with sqlite3.connect(path) as con:
                assert con.execute('select count(*) from match_diagnostic_events').fetchone()[0] == 0
            monkeypatch.setenv('MATCH_DIAGNOSTICS', 'test-match')
            record('test-match', 'engine_result', {'score': 42}, trace_id='request-1')
            with sqlite3.connect(path) as con:
                row = con.execute('select trace_id, payload_json, metrics_json from match_diagnostic_events').fetchone()
            assert row[0] == 'request-1'
            assert json.loads(row[1]) == {'score': 42}
            assert json.loads(row[2])['process_rss_bytes'] > 0
            record('test-match', 'large', {'value': 'x' * 140000})
            with sqlite3.connect(path) as con:
                assert con.execute("select payload_truncated from match_diagnostic_events where stage='large'").fetchone()[0] == 1


def test_trace_missing_table_does_not_fail_gameplay(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(tmp_path / 'missing.db')
    db.init_app(app)
    monkeypatch.setenv('MATCH_DIAGNOSTICS', '*')
    with app.test_request_context('/'):
        record('test', 'engine_start')
