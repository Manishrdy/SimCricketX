"""Run CPU-bound work without freezing the gevent hub.

Production serves this app from a single gunicorn worker
(``gunicorn.conf.py``: ``workers = 1``, geventwebsocket worker class) because
match state lives in the in-process ``MATCH_INSTANCES`` dict. gevent is
cooperative, so a greenlet that runs a long stretch of straight computation
never yields — and for its whole duration the worker serves nobody. It does
not answer other requests, and, more damagingly, it does not run Socket.IO's
own heartbeat.

That is not hypothetical. The FC captain's declaration model
(``engine/fc_captain.evaluate_declaration``) took 52.6 s and 45.6 s in two
measured production calls. Socket.IO's defaults allow ``pingInterval`` 25 s +
``pingTimeout`` 20 s = 45 s, so both calls outlived the connection: the client
declared a ping timeout and tore the socket down *before* the server emitted
the result. For the Lunch call that result was the session scorecard, which is
delivered exactly once — so the card was lost and the session had already been
consumed server-side.

``run_offloaded`` hands the work to gevent's own thread pool. The calling
greenlet blocks, but the hub keeps running: CPython releases the GIL every
``sys.setswitchinterval()`` (5 ms by default) and for the duration of NumPy's
larger array operations, which is ample for heartbeats and for other requests
to be served. The offloaded call itself gets no faster — slightly slower, if
anything — but it stops taking the whole server down with it.

Outside gevent (tests, benchmark scripts, ``scripts/bench_fc.py``) there is no
hub, and the callable simply runs inline.

The callable **must be pure CPU over plain data**: no Flask request context, no
``db.session``, no ``MATCH_INSTANCES`` mutation. It runs on a real OS thread,
where none of those are safe to touch.
"""
import logging

logger = logging.getLogger(__name__)

_UNAVAILABLE = object()
_threadpool = _UNAVAILABLE


def _get_threadpool():
    """gevent's hub thread pool, or None when not running under gevent.

    Resolved once and cached. Importing gevent is not enough on its own — a
    hub only exists in a gevent-driven process, and asking for one from a
    plain synchronous process would create it as a side effect, so this also
    checks that monkey-patching actually happened.
    """
    global _threadpool
    if _threadpool is not _UNAVAILABLE:
        return _threadpool
    try:
        from gevent import monkey
        if not monkey.is_module_patched("threading"):
            _threadpool = None
        else:
            from gevent import get_hub
            _threadpool = get_hub().threadpool
    except Exception:
        _threadpool = None
    return _threadpool


def run_offloaded(func, *args, **kwargs):
    """Call ``func(*args, **kwargs)`` without blocking the gevent hub.

    Under gevent the call runs on the hub's thread pool and this greenlet
    waits for it; everywhere else it runs inline. Exceptions propagate
    identically either way, so callers need no special handling.
    """
    pool = _get_threadpool()
    if pool is None:
        return func(*args, **kwargs)
    return pool.apply(func, args, kwargs)
