"""Optional per-match random stream; legacy formats retain the module stream."""
from contextvars import ContextVar
from contextlib import contextmanager
import random as _random

_stream = ContextVar('cricket_random_stream', default=None)


def __getattr__(name):
    return getattr(_stream.get() or _random, name)


@contextmanager
def use_state(state):
    rng = _random.Random()
    rng.setstate(state)
    token = _stream.set(rng)
    try:
        yield rng
    finally:
        _stream.reset(token)
