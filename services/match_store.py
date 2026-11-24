"""Process-local ownership and lifecycle state for full match instances.

This module starts no workers and performs no I/O. Integration must pass a
loader which checks durable ownership/deletion state; a store lease is NOT
authorization. Record successful lifecycle events only after durable writes.

Lock ordering: the private condition protects registry/metadata changes only.
Loaders, simulation, persistence, executor submission and callbacks run outside
it. Callers may hold a per-match operation/file lock while entering the store;
the store never takes those locks. Leases prevent eviction, not simultaneous
simulation: keep the existing delivery and database transaction guards.
"""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Condition, RLock
from time import monotonic
from typing import Any, Callable, Optional


COMPLETED_GRACE_SECONDS = 15 * 60
INCOMPLETE_IDLE_SECONDS = 12 * 60 * 60


class MatchUnavailable(LookupError):
    """The instance is absent and no loader was supplied."""


class MatchInvalidated(MatchUnavailable):
    """Deletion/replay invalidated this ID or an in-flight operation."""


@dataclass
class _Entry:
    last_access: float
    instance: Any = None
    loading: bool = True
    users: int = 0
    retiring: bool = False
    jobs: set = field(default_factory=set)
    match_format: str = "unknown"
    completed: bool = False
    result_status: str = "pending"
    result_at: Optional[datetime] = None
    result_error: Optional[str] = None
    archive_status: str = "pending"
    archive_revision: int = 0
    archive_path: Optional[str] = None
    archive_at: Optional[datetime] = None
    archive_error: Optional[str] = None
    ready_since: Optional[float] = None


class MatchLease:
    """Explicit, exception-safe protection; always close or use as a context.

    Fork BEFORE scheduling deferred work. A fork is independent of its parent
    and must remain open until that work finishes, including cancellation.
    ``submit`` handles this automatically for concurrent.futures executors.
    """

    def __init__(self, store, match_id, entry):
        self._store = store
        self.match_id = match_id
        self._entry = entry

    @property
    def instance(self):
        store = self._store
        if store is None:
            raise RuntimeError("Match lease is closed")
        with store._condition:
            store._check_lease(self)
            return self._entry.instance

    def __enter__(self):
        try:
            self.instance  # Reject reuse of closed/invalidated leases.
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        store = self._store
        if store is None:
            return
        with store._condition:
            if self._store is None:
                return
            entry = self._entry
            entry.users -= 1
            if (self.match_id in store._invalidated or entry.retiring) and not entry.users and not entry.loading:
                store._entries.pop(self.match_id, None)
            # A retained closed lease/future must not retain a full Match.
            self._entry = self._store = None
            store._condition.notify_all()

    def fork(self):
        store = self._store
        if store is None:
            raise RuntimeError("Match lease is closed")
        with store._condition:
            store._check_lease(self)
            self._entry.users += 1
            return MatchLease(store, self.match_id, self._entry)

    def submit(self, executor: Executor, function: Callable, *args, **kwargs):
        """Run function(instance, ...) with protection through completion."""
        child = self.fork()
        try:
            future = executor.submit(lambda: function(child.instance, *args, **kwargs))
        except BaseException:
            child.close()
            raise
        future.add_done_callback(lambda _future: child.close())
        return future

    def protect_job(self, job):
        """Keep a gevent offload protected until completion, even after timeout.

        rawlink callbacks run on the owning hub, never on its native CPU thread.
        There is no polling worker or additional reference after completion.
        """
        store = self._store
        with store._condition:
            entry = store._check_lease(self)
            key = id(job)
            if key in entry.jobs or job.ready():
                return
            child = self.fork()
            entry.jobs.add(key)
        def finished(_job):
            with store._condition:
                if child._entry is not None:
                    child._entry.jobs.discard(key)
            child.close()
        try:
            job.rawlink(finished)
        except BaseException:
            finished(job)
            raise


class MatchStore:
    """One authoritative registry per process; clocks are injectable for tests.

    Invalidation retains only ID tombstones after operations drain. These reset
    on process restart; durable deletion/replay guards remain the loader's job.
    An invalidated ID cannot be reused in this store. Toss changes should use
    ``discard`` under the existing per-match operation guard instead.
    """

    def __init__(self, *, clock=monotonic, utcnow=None):
        self._clock = clock
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._condition = Condition(RLock())
        self._entries = {}
        self._invalidated = set()
        self._removed = Counter()
        self._last_sweep = {}
        self._owner_gates = {}
        self._scope = ContextVar("match_store_scope", default=None)

    def acquire(self, match_id, *, loader=None, touch=False):
        """Acquire an existing instance or coalesce concurrent loads for its ID.

        Loader exceptions propagate, wake waiters, and permit a subsequent
        retry. Invalidating during loading prevents publication of that result.
        New/legacy objects without access metadata start their idle clock when
        loaded. Wall-clock ``created_at``/``last_accessed`` are not compared to
        monotonic time. Only meaningful, authorized gameplay should use touch.
        """
        with self._condition:
            while True:
                if match_id in self._invalidated:
                    raise MatchInvalidated(match_id)
                entry = self._entries.get(match_id)
                if entry is None:
                    if loader is None:
                        raise MatchUnavailable(match_id)
                    entry = _Entry(last_access=self._clock())
                    self._entries[match_id] = entry
                    break
                if entry.loading or entry.retiring:
                    self._condition.wait()
                    continue
                entry.users += 1
                if touch:
                    entry.last_access = self._clock()
                return MatchLease(self, match_id, entry)

        try:
            instance = loader()
            if instance is None:
                raise MatchUnavailable(match_id)
            # Inspect the instance outside the registry lock (no arbitrary
            # engine properties or callbacks under the global lock).
            data = getattr(instance, "data", {})
            match_format = str(data.get("match_format") or "unknown")
            completed = data.get("current_state") == "completed"
        except BaseException:
            with self._condition:
                self._entries.pop(match_id, None)
                self._condition.notify_all()
            raise

        with self._condition:
            if match_id in self._invalidated or entry.retiring:
                self._entries.pop(match_id, None)
                self._condition.notify_all()
                raise MatchInvalidated(match_id)
            entry.instance = instance
            entry.loading = False
            entry.match_format = match_format
            entry.completed = completed
            entry.last_access = self._clock()
            entry.users = 1
            self._condition.notify_all()
            return MatchLease(self, match_id, entry)

    @contextmanager
    def borrow_all(self):
        """Protect a point-in-time collection for inspection, without touching."""
        with self._condition:
            leases = []
            pairs = []
            for mid, entry in self._entries.items():
                if entry.loading or entry.retiring or mid in self._invalidated:
                    continue
                entry.users += 1
                leases.append(MatchLease(self, mid, entry))
                pairs.append((mid, entry.instance))
        try:
            yield tuple(pairs)
        finally:
            for lease in leases:
                lease.close()

    def cached_ids(self):
        with self._condition:
            return tuple(self._entries)

    def is_invalidated(self, match_id):
        with self._condition:
            return match_id in self._invalidated

    @contextmanager
    def scope(self, owner):
        """Serialize an owner's match/tournament operations and hold their leases.

        Owner gates are separate from the registry lock. Different owners never
        wait on simulation or I/O here. This also orders deletion/replay against
        uploads and completion, without needing multi-match lock acquisition.
        Existing identity admission locks must be acquired before this scope.
        """
        active = self._scope.get()
        if active is not None:
            if active.owner != owner:
                raise RuntimeError("Cannot nest scopes for different owners")
            yield active
            return
        with self._condition:
            gate = self._owner_gates.setdefault(owner, [RLock(), 0])
            gate[1] += 1
        try:
            with gate[0]:
                active = MatchScope(self, owner)
                token = self._scope.set(active)
                try:
                    yield active
                finally:
                    try:
                        active.close()
                    finally:
                        self._scope.reset(token)
        finally:
            with self._condition:
                gate[1] -= 1
                if not gate[1]:
                    del self._owner_gates[owner]

    def current_scope(self):
        scope = self._scope.get()
        if scope is None:
            raise RuntimeError("Match access requires an operation scope")
        return scope

    def retire(self, match_id):
        """Ownership/cache refresh: drain leases, then allow a durable reload."""
        with self._condition:
            entry = self._entries.get(match_id)
            if entry is not None:
                entry.retiring = True
                if not entry.users and not entry.loading:
                    del self._entries[match_id]
            self._condition.notify_all()

    def _check_lease(self, lease):
        if lease._store is not self or lease._entry is None:
            raise RuntimeError("Closed or foreign match lease")
        if (lease.match_id in self._invalidated
                or self._entries.get(lease.match_id) is not lease._entry):
            raise MatchInvalidated(lease.match_id)
        return lease._entry

    def touch(self, lease):
        with self._condition:
            self._check_lease(lease).last_access = self._clock()

    def mark_completed(self, lease):
        """Engine completion alone is not evidence of persistence/readiness."""
        with self._condition:
            entry = self._check_lease(lease)
            entry.completed = True
            self._refresh_ready(entry)

    def mark_result(self, lease, status, *, error=None):
        if status not in {"pending", "succeeded", "failed"}:
            raise ValueError("Invalid result status")
        with self._condition:
            entry = self._check_lease(lease)
            entry.completed = True
            if status == "succeeded" and entry.result_status != "succeeded":
                entry.result_at = self._utcnow()
            elif status != "succeeded":
                entry.result_at = None
            entry.result_status = status
            entry.result_error = str(error) if error is not None and status == "failed" else None
            self._refresh_ready(entry)

    def begin_archive(self, lease):
        """Invalidate readiness for accepted new inputs or a new build attempt.

        The returned revision fences stale builds: an older build cannot mark
        newer inputs ready. Archive publication itself also needs a per-match
        lock/atomic rename; this method fences metadata, not filesystem writes.
        """
        with self._condition:
            entry = self._check_lease(lease)
            entry.archive_revision += 1
            entry.archive_status = "pending"
            entry.archive_at = entry.archive_path = entry.archive_error = None
            entry.ready_since = None
            return entry.archive_revision

    def mark_archive(self, lease, revision, status, *, path=None, error=None):
        """Record a validated, durably published FINAL archive, or its failure.

        The engine's initial ZIP must not be reported as ready. Returns False
        for an obsolete build, without changing the newer archive state.
        """
        if status not in {"ready", "failed"}:
            raise ValueError("Invalid archive status")
        if status == "ready" and not path:
            raise ValueError("A final archive path is required")
        with self._condition:
            entry = self._check_lease(lease)
            if revision != entry.archive_revision or revision < 1:
                return False
            if status == "ready":
                if entry.archive_status != "ready":
                    entry.archive_at = self._utcnow()
                entry.archive_path = str(path)
            else:
                entry.archive_at = entry.archive_path = None
            entry.archive_status = status
            entry.archive_error = str(error) if error is not None and status == "failed" else None
            self._refresh_ready(entry)
            return True

    def _refresh_ready(self, entry):
        if entry.completed and entry.result_status == "succeeded" and entry.archive_status == "ready":
            if entry.ready_since is None:
                entry.ready_since = self._clock()
        else:
            entry.ready_since = None

    def invalidate(self, match_id):
        """Block new operations/publication; retain protected objects until release.

        Callers must coordinate durable deletion with their transaction/operation
        guards. This cannot cancel engine or database work already in progress.
        """
        with self._condition:
            self._invalidated.add(match_id)
            entry = self._entries.get(match_id)
            if entry is not None and not entry.users and not entry.loading:
                del self._entries[match_id]
            self._condition.notify_all()

    def discard(self, match_id):
        """Discard a quiescent instance for reload; never interrupt a load/lease."""
        with self._condition:
            entry = self._entries.get(match_id)
            if entry is None or entry.loading or entry.users:
                return False
            del self._entries[match_id]
            self._removed["discard"] += 1
            return True

    def sweep(self, kind):
        """Explicit policy sweep; no scheduler is installed by this service.

        Completed entries never fall through to the inactivity policy. A caller
        must mark engine completion even when subsequent persistence fails.
        """
        if kind not in {"completed", "incomplete"}:
            raise ValueError("Unknown cleanup policy")
        with self._condition:
            now = self._clock()
            removed, protected, pending = [], 0, 0
            for match_id, entry in list(self._entries.items()):
                if entry.loading or match_id in self._invalidated:
                    continue
                if entry.completed != (kind == "completed"):
                    continue
                if entry.users:
                    protected += 1
                    continue
                if kind == "completed":
                    if entry.ready_since is None:
                        pending += 1
                        continue
                    eligible = now - entry.ready_since >= COMPLETED_GRACE_SECONDS
                else:
                    eligible = now - entry.last_access >= INCOMPLETE_IDLE_SECONDS
                if eligible:
                    del self._entries[match_id]
                    removed.append(match_id)
            self._removed[kind] += len(removed)
            outcome = {"at": self._utcnow(), "removed": len(removed),
                       "protected": protected, "pending": pending}
            self._last_sweep[kind] = outcome
            return {**outcome, "match_ids": tuple(removed)}

    def describe(self, match_id):
        """Copy lifecycle metadata without returning or touching the instance."""
        with self._condition:
            entry = self._entries.get(match_id)
            if entry is None:
                return None
            now = self._clock()
            return {
                "loading": entry.loading, "protected": entry.users,
                "invalidated": match_id in self._invalidated,
                "match_format": entry.match_format, "completed": entry.completed,
                "access_age_seconds": max(0, now - entry.last_access),
                "result_status": entry.result_status, "result_at": entry.result_at,
                "result_error": entry.result_error,
                "archive_status": entry.archive_status, "archive_at": entry.archive_at,
                "archive_path": entry.archive_path, "archive_error": entry.archive_error,
                "archive_revision": entry.archive_revision,
                "ready_age_seconds": (None if entry.ready_since is None
                                      else max(0, now - entry.ready_since)),
            }

    def metrics(self):
        """Cheap O(cached matches) snapshot: no I/O, engine reads or touches.

        Counts are process-local and reset on restart. 'Idle' means 12 hours
        since gameplay access, not proof of abandonment. No RSS estimate.
        """
        with self._condition:
            now = self._clock()
            entries = [e for e in self._entries.values() if not e.loading]
            completed = [e for e in entries if e.completed]
            incomplete = [e for e in entries if not e.completed]
            idle = sum(now - e.last_access >= INCOMPLETE_IDLE_SECONDS for e in incomplete)
            return {
                "total": len(entries), "loading": len(self._entries) - len(entries),
                "completed": len(completed), "incomplete_recent": len(incomplete) - idle,
                "incomplete_idle": idle,
                "archives": {status: sum(e.archive_status == status for e in completed)
                             for status in ("ready", "pending", "failed")},
                "formats": dict(Counter(e.match_format for e in entries)),
                "protected": sum(e.users > 0 for e in entries),
                "oldest_access_seconds": max((max(0, now - e.last_access) for e in entries), default=None),
                "invalidated_ids": len(self._invalidated),
                "removed": dict(self._removed),
                "last_sweep": {kind: dict(value) for kind, value in self._last_sweep.items()},
                "completed_grace_seconds": COMPLETED_GRACE_SECONDS,
                "incomplete_idle_seconds": INCOMPLETE_IDLE_SECONDS,
            }


class MatchScope:
    """Request/operation-owned leases; never return an instance past this scope."""
    def __init__(self, store, owner):
        self.store, self.owner = store, owner
        self.leases = {}

    def get(self, match_id, *, loader=None):
        if match_id not in self.leases:
            try:
                self.leases[match_id] = self.store.acquire(match_id, loader=loader)
            except MatchInvalidated:
                raise
            except MatchUnavailable:
                return None
        return self.leases[match_id].instance

    def discard(self, match_id):
        lease = self.leases.pop(match_id, None)
        if lease:
            lease.close()
        self.store.retire(match_id)

    def close(self):
        try:
            for lease in self.leases.values():
                try:
                    instance = lease.instance
                except MatchInvalidated:
                    continue
                if getattr(instance, "data", {}).get("current_state") == "completed":
                    self.store.mark_completed(lease)
                job = getattr(instance, "_fc_declaration_job", None)
                if job is not None:
                    lease.protect_job(job)
        finally:
            for lease in self.leases.values():
                lease.close()
            self.leases.clear()
