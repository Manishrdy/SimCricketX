"""Cooperative CPU deadline for live captain decisions, scoped to one worker.

Analytical callers have no deadline. Cached forecasts are never stored half
finished: expiry raises through the cached function, so only complete results
enter the cache. No locks or gevent primitives are used by worker threads.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

_deadline = ContextVar("fc_decision_deadline", default=None)


class DecisionBudgetExceeded(Exception):
    pass


def check_budget():
    deadline = _deadline.get()
    if deadline is not None and monotonic() >= deadline:
        raise DecisionBudgetExceeded


@contextmanager
def decision_budget(deadline):
    previous = _deadline.get()
    token = _deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        check_budget()
        yield
        check_budget()
    finally:
        _deadline.reset(token)
