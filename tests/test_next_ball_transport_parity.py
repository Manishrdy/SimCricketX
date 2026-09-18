"""Both next_ball transports must run one implementation.

The HTTP route (POST /match/<id>/next-ball) and the WebSocket handler
(app.py's socketio 'next_ball') were once two hand-copied bodies. The copy
drifted: it never restored an FC/super-over snapshot -- rebuilding a fresh
fc_innings=1 Match -- and never persisted one, so a first-class match played
over WebSocket ran completely uncheckpointed and could silently restart.

These tests pin the shared-implementation arrangement that fixed it.
"""
import inspect
import re

import app as app_module
from routes.match_routes import register_match_routes


SHARED_HELPERS = {
    "advance_one_ball",
    "get_or_restore_match_instance",
    "persist_fc_snapshot",
    "persist_super_over_snapshot",
    "finalize_completed_match",
}


def _match_routes_source():
    return inspect.getsource(register_match_routes)


def _create_app_source():
    # create_app is deliberately rebound at import to an idempotent wrapper;
    # the real body (which defines the WS handler) is the uncached original.
    return inspect.getsource(app_module._create_app_uncached)


def test_register_match_routes_exports_shared_helpers():
    src = _match_routes_source()
    returned = set(re.findall(r'"([a-z_]+)":\s*_[a-z_]+,', src))
    missing = SHARED_HELPERS - returned
    assert not missing, f"register_match_routes stopped exporting: {missing}"


def test_ws_handler_delegates_to_shared_helper():
    src = _create_app_source()
    ws = src[src.index("def _ws_next_ball"):]
    ws = ws[:ws.index("app.logger.info('[SocketIO]")]

    assert "_match_route_helpers['advance_one_ball']" in ws, (
        "The WebSocket next_ball handler no longer delegates to the shared "
        "_advance_one_ball implementation -- the two transports have diverged "
        "again."
    )
    # The failure mode that caused the original divergence: the WS copy built
    # its own Match, bypassing snapshot restore entirely.
    assert "Match(match_data_ws)" not in ws, (
        "The WebSocket handler is constructing a Match directly again. That "
        "bypasses FC/super-over snapshot restore and silently restarts the "
        "match at fc_innings=1."
    )


def test_http_route_delegates_to_shared_helper():
    src = _match_routes_source()
    route = src[src.index('@app.route("/match/<match_id>/next-ball"'):]
    route = route[:route.index("@app.route", 10)]
    assert "payload, err = _advance_one_ball(" in route
    assert 'get("delivery_token")' in route


def test_shared_helper_persists_fc_snapshot_and_weather_status():
    src = _match_routes_source()
    body = src[src.index("def _advance_one_ball"):src.index('@app.route("/match/<match_id>/next-ball"')]
    # Both were missing from the WebSocket copy.
    assert "_persist_fc_snapshot(match, match_id)" in body
    assert "fc_weather_status" in body
    assert "_get_or_restore_match_instance(match_id)" in body


def test_create_app_is_idempotent_for_the_gunicorn_entrypoint():
    """`gunicorn "app:create_app()"` must not build a second Flask app."""
    assert app_module.create_app is not app_module._create_app_uncached
    if app_module.app is not None:
        assert app_module.create_app() is app_module.app
