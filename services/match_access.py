"""Flask operation boundaries for the authoritative match store."""
from functools import wraps
from flask import current_app, jsonify, make_response
from flask_login import current_user
from services.match_store import MatchInvalidated


def match_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        store = current_app.extensions["match_store"]
        with store.scope(getattr(current_user, "id", None)) as scope:
            if store.is_invalidated(kwargs.get("match_id")):
                raise MatchInvalidated(kwargs["match_id"])
            if kwargs.get("match_id"):
                # Includes image uploads which may authorize from JSON/DB
                # without otherwise reading the already-cached Match.
                scope.get(kwargs["match_id"])
            result = function(*args, **kwargs)
            # Gameplay/resume refresh inactivity; archive and admin reads do not.
            if function.__name__ in {
                "match_detail", "match_live_state", "impact_player_swap", "update_final_lineups",
                "ack_interval_card", "set_simulation_mode",
            } and 200 <= make_response(result).status_code < 300:
                lease = store.current_scope().leases.get(kwargs.get("match_id"))
                if lease is not None:
                    store.touch(lease)
            return result
    return wrapped


def protect_registered_routes(app, previous):
    """Wrap all newly registered routes, including nested tour registration."""
    def protect(function):
        function = match_operation(function)
        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except MatchInvalidated:
                return jsonify(error="This match was removed. Return to your matches or tournament."), 410
        return guarded
    for endpoint in app.view_functions.keys() - previous:
        app.view_functions[endpoint] = protect(app.view_functions[endpoint])
