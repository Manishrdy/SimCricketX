"""Test-only mapping to seed/inspect the real store in legacy engine fixtures.

Returned objects are deliberately unleased for assertions. Production callers
must use operation scopes. This proxy owns no cache and is never imported by app.
"""
from collections.abc import MutableMapping
from threading import RLock
from services.match_store import MatchUnavailable


class RegistryFixture(MutableMapping):
    @property
    def store(self):
        import app
        return app.MATCH_STORE

    def __getitem__(self, key):
        try:
            with self.store.acquire(key) as lease:
                return lease.instance
        except MatchUnavailable:
            raise KeyError(key) from None

    def __setitem__(self, key, value):
        if self.store.describe(key) is not None and not self.store.discard(key):
            raise AssertionError("Fixture tried to replace a protected match")
        with self.store.acquire(key, loader=lambda: value):
            pass

    def __delitem__(self, key):
        if not self.store.discard(key):
            raise KeyError(key)

    def __len__(self):
        return self.store.metrics()["total"]

    def __iter__(self):
        return iter(self.store.cached_ids())


instances = RegistryFixture()
lock = RLock()
