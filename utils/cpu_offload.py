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

It must also avoid every gevent-patched primitive — ``threading.Event``,
``Lock``, ``Queue``, ``time.sleep``, sockets. After ``monkey.patch_all()`` those
are cooperative objects that expect a hub, and the pool's worker threads have
none, so blocking on one deadlocks instead of waiting. Pure computation over
numbers, dicts and NumPy arrays is the whole of what belongs here.
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

class _InlineResult:
    """The no-gevent stand-in for a thread-pool AsyncResult.

    The work has already run by the time this exists, so ``ready()`` is always
    True and ``get()`` replays the outcome. That keeps the deferred-call shape
    usable from tests and from scripts/bench_fc.py, where there is no hub and
    everything is synchronous anyway.
    """
    __slots__ = ("_value", "_error")

    def __init__(self, func, args, kwargs):
        self._value = None
        self._error = None
        try:
            self._value = func(*args, **kwargs)
        except BaseException as exc:  # re-raised from get(), as the pool does
            self._error = exc

    def ready(self):
        return True

    def get(self):
        if self._error is not None:
            raise self._error
        return self._value


def start_offloaded(func, *args, **kwargs):
    """Begin a CPU-bound call and return a handle, without waiting for it.

    Under gevent the work starts on the hub's thread pool immediately and the
    caller carries on; ``handle.get()`` waits for the result and re-raises
    anything the call raised. Without a hub the call runs inline right now and
    the handle just replays it, so ordering and exceptions match either way.

    This is what lets the FC engine answer a session break before it has
    decided whether the captain declares: the decision is started here and
    settled on the next delivery, by which time the user has been looking at
    the scorecard for several seconds. The same purity rules as
    ``run_offloaded`` apply — plain data only, no Flask context, no session,
    no mutation of the Match.
    """
    pool = _get_threadpool()
    if pool is None:
        return _InlineResult(func, args, kwargs)
    return pool.spawn(func, *args, **kwargs)
