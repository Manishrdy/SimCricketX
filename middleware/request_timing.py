"""Low-overhead request diagnostics; no database writes or payload logging."""
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from flask import g, has_request_context, request
from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger('simcricketx.http')
logger.setLevel(logging.INFO)
logger.propagate = False  # Never duplicate into app file/session/database handlers.
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)


def _state():
    return getattr(g, '_http_timing', None) if has_request_context() else None


@event.listens_for(Engine, 'before_cursor_execute')
def _query_start(conn, cursor, statement, parameters, context, executemany):
    state = _state()
    if state is not None:
        context._scx_query_started = time.perf_counter()
        state['db_query_count'] += 1


def _query_end(context):
    state = _state()
    started = getattr(context, '_scx_query_started', None)
    if state is not None and started is not None:
        state['db_duration_ms'] += (time.perf_counter() - started) * 1000
        context._scx_query_started = None


@event.listens_for(Engine, 'after_cursor_execute')
def _query_success(conn, cursor, statement, parameters, context, executemany):
    _query_end(context)


@event.listens_for(Engine, 'handle_error')
def _query_failure(exception_context):
    _query_end(exception_context.execution_context)


def install_request_timing(app):
    """Install before other hooks, so their work is included in timings."""
    @app.before_request
    def start():
        supplied = request.headers.get('X-Request-ID', '')
        # Nginx must overwrite this header; validation bounds untrusted input.
        g.request_id = supplied if re.fullmatch(r'[a-fA-F0-9]{32}', supplied) else uuid.uuid4().hex
        ray = request.headers.get('CF-Ray', '')
        g._http_timing = {
            'started': time.perf_counter(),
            'db_query_count': 0,
            'db_duration_ms': 0.0,
            'cf_ray': ray if re.fullmatch(r'[a-fA-F0-9]{16,32}-[A-Za-z]{3}', ray) else None,
        }

    @app.after_request
    def finish(response):
        state = _state()
        if state is None:
            return response
        response.headers['X-Request-ID'] = g.request_id
        state['status'] = response.status_code
        return response

    @app.teardown_request
    def record(error):
        state = _state()
        if state is None or state.get('logged'):
            return
        state['logged'] = True
        record = {
            'event': 'http_request',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'request_id': g.request_id,
            'cf_ray': state['cf_ray'],
            'method': request.method,
            'route': request.url_rule.rule if request.url_rule else '<unmatched>',
            'status': state.get('status', 500),
            'duration_ms': round((time.perf_counter() - state['started']) * 1000, 3),
            'db_query_count': state['db_query_count'],
            'db_duration_ms': round(state['db_duration_ms'], 3),
            'exception_type': type(error).__name__ if error else None,
        }
        try:
            logger.info(json.dumps(record, separators=(',', ':')))
        except Exception:
            pass  # Diagnostics must never fail a request.
