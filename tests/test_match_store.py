"""Lifecycle service contract tests; no database, users or real clock waits."""
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import gc
from threading import Barrier, Event
import weakref

import pytest

from services.match_store import (
    COMPLETED_GRACE_SECONDS, INCOMPLETE_IDLE_SECONDS,
    MatchInvalidated, MatchStore, MatchUnavailable,
)


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.wall = datetime(2026, 11, 21, tzinfo=timezone.utc)

    def now(self):
        return self.elapsed

    def utcnow(self):
        return self.wall

    def advance(self, seconds):
        self.elapsed += seconds
        self.wall += timedelta(seconds=seconds)


class Match:
    def __init__(self, fmt="T20", completed=False):
        self.data = {"match_format": fmt, "current_state": "completed" if completed else "live"}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return MatchStore(clock=clock.now, utcnow=clock.utcnow)


def ready(store, lease):
    store.mark_result(lease, "succeeded")
    revision = store.begin_archive(lease)
    assert store.mark_archive(lease, revision, "ready", path="data/final.zip")
    return revision


@pytest.mark.parametrize("offset,removed", [(-0.001, 0), (0, 1), (0.001, 1)])
def test_completed_boundary(store, clock, offset, removed):
    with store.acquire("match", loader=Match) as lease:
        ready(store, lease)
    clock.advance(COMPLETED_GRACE_SECONDS + offset)
    assert store.sweep("completed")["removed"] == removed


@pytest.mark.parametrize("result", ["pending", "failed", "succeeded"])
@pytest.mark.parametrize("archive", ["pending", "failed", "ready"])
def test_both_durable_events_required_and_idle_cannot_bypass(store, clock, result, archive):
    with store.acquire("match", loader=Match) as lease:
        store.mark_completed(lease)
        store.mark_result(lease, result, error="result unavailable")
        revision = store.begin_archive(lease)
        if archive != "pending":
            store.mark_archive(lease, revision, archive, path="data/final.zip", error="archive unavailable")
    clock.advance(INCOMPLETE_IDLE_SECONDS * 2)
    assert store.sweep("incomplete")["removed"] == 0
    assert store.sweep("completed")["removed"] == int(result == "succeeded" and archive == "ready")


@pytest.mark.parametrize("first", ["result", "archive"])
def test_grace_starts_at_second_event_and_duplicate_events_do_not_extend_it(store, clock, first):
    with store.acquire("match", loader=Match) as lease:
        revision = store.begin_archive(lease)
        def result():
            store.mark_result(lease, "succeeded")
        def archive():
            store.mark_archive(lease, revision, "ready", path="data/final.zip")
        operations = (result, archive) if first == "result" else (archive, result)
        operations[0]()
        clock.advance(1000)
        assert store.describe("match")["ready_age_seconds"] is None
        operations[1]()
        before = store.describe("match")
        clock.advance(500)
        result()
        archive()
        after = store.describe("match")
        assert after["result_at"] == before["result_at"]
        assert after["archive_at"] == before["archive_at"]
        assert after["ready_age_seconds"] == 500
    clock.advance(400)
    assert store.sweep("completed")["removed"] == 1


def test_engine_completion_is_not_persistence_evidence(store, clock):
    with store.acquire("match", loader=lambda: Match(completed=True)):
        pass
    clock.advance(INCOMPLETE_IDLE_SECONDS * 2)
    assert store.sweep("completed")["pending"] == 1
    assert store.sweep("incomplete")["removed"] == 0


def test_late_inputs_fence_stale_builds_and_restart_grace(store, clock):
    with store.acquire("match", loader=Match) as lease:
        old = ready(store, lease)
        clock.advance(COMPLETED_GRACE_SECONDS)
        new = store.begin_archive(lease)
        assert not store.mark_archive(lease, old, "ready", path="data/stale.zip")
        assert store.describe("match")["archive_status"] == "pending"
        assert store.describe("match")["ready_age_seconds"] is None
        assert store.mark_archive(lease, new, "ready", path="data/new.zip")
    assert store.sweep("completed")["removed"] == 0
    clock.advance(COMPLETED_GRACE_SECONDS)
    assert store.sweep("completed")["removed"] == 1


def test_failed_rebuild_exposes_error_and_blocks_eviction(store, clock):
    with store.acquire("match", loader=Match) as lease:
        ready(store, lease)
        attempt = store.begin_archive(lease)
        store.mark_archive(lease, attempt, "failed", error="ZIP validation failed")
    clock.advance(INCOMPLETE_IDLE_SECONDS * 2)
    state = store.describe("match")
    assert state["archive_error"] == "ZIP validation failed"
    assert state["archive_path"] is None
    assert store.sweep("completed")["pending"] == 1


def test_exception_releases_protection(store, clock):
    with pytest.raises(ValueError, match="simulation failed"):
        with store.acquire("match", loader=Match) as lease:
            ready(store, lease)
            clock.advance(COMPLETED_GRACE_SECONDS)
            assert store.sweep("completed")["protected"] == 1
            assert not store.discard("match")
            raise ValueError("simulation failed")
    assert store.sweep("completed")["removed"] == 1


def test_deferred_work_outlives_parent_request(store, clock):
    with store.acquire("match", loader=Match) as request:
        ready(store, request)
        background = request.fork()
    clock.advance(COMPLETED_GRACE_SECONDS)
    assert store.sweep("completed")["protected"] == 1
    assert background.instance.data["match_format"] == "T20"
    background.close()
    background.close()  # Release is idempotent, including cancellation races.
    assert store.sweep("completed")["removed"] == 1


def test_executor_protects_until_worker_finishes(store, clock):
    started, finish = Event(), Event()
    def work(instance):
        started.set()
        assert finish.wait(5)
        return instance.data["match_format"]
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            with store.acquire("match", loader=Match) as request:
                ready(store, request)
                future = request.submit(executor, work)
            assert started.wait(5)
            clock.advance(COMPLETED_GRACE_SECONDS)
            assert store.sweep("completed")["protected"] == 1
        finally:
            finish.set()
        assert future.result(timeout=5) == "T20"
    assert store.sweep("completed")["removed"] == 1


def test_cancelled_queued_work_and_failed_submission_release(store, clock):
    class QueuedExecutor:
        def submit(self, function):
            return Future()
    class FailedExecutor:
        def submit(self, function):
            raise RuntimeError("executor stopped")
    with store.acquire("match", loader=Match) as request:
        ready(store, request)
        future = request.submit(QueuedExecutor(), lambda m: None)
        with pytest.raises(RuntimeError, match="executor stopped"):
            request.submit(FailedExecutor(), lambda m: None)
    clock.advance(COMPLETED_GRACE_SECONDS)
    assert store.sweep("completed")["protected"] == 1
    assert future.cancel()
    assert store.sweep("completed")["removed"] == 1


def test_worker_exception_releases_lease(store, clock):
    def fail(instance):
        raise ValueError("worker failed")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.acquire("match", loader=Match) as request:
            ready(store, request)
            future = request.submit(executor, fail)
        with pytest.raises(ValueError, match="worker failed"):
            future.result(timeout=5)
    clock.advance(COMPLETED_GRACE_SECONDS)
    assert store.sweep("completed")["removed"] == 1


def test_concurrent_loaders_publish_one_instance(store):
    barrier = Barrier(8)
    loaded = []
    def loader():
        instance = Match()
        loaded.append(instance)
        return instance
    def reader():
        barrier.wait(timeout=5)
        with store.acquire("match", loader=loader) as lease:
            return lease.instance
    with ThreadPoolExecutor(max_workers=8) as executor:
        instances = [future.result(timeout=5) for future in [executor.submit(reader) for _ in range(8)]]
    assert len(loaded) == 1
    assert all(instance is loaded[0] for instance in instances)
    assert store.metrics()["protected"] == 0


def test_slow_loader_does_not_block_other_matches_or_metrics(store):
    entered, finish = Event(), Event()
    def loader():
        entered.set()
        assert finish.wait(5)
        return Match()
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(store.acquire, "slow", loader=loader)
        try:
            assert entered.wait(5)
            other = executor.submit(store.acquire, "fast", loader=Match)
            with other.result(timeout=2):
                assert store.metrics()["loading"] == 1
                assert store.metrics()["total"] == 1
                assert not store.discard("slow")
        finally:
            finish.set()
        future.result(timeout=5).close()


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt])
def test_loader_failure_wakes_waiter_and_allows_retry(store, failure):
    entered, finish = Event(), Event()
    def fail():
        entered.set()
        assert finish.wait(5)
        raise failure("load failed")
    with ThreadPoolExecutor(max_workers=2) as executor:
        failed = executor.submit(store.acquire, "match", loader=fail)
        try:
            assert entered.wait(5)
            retry = executor.submit(store.acquire, "match", loader=Match)
        finally:
            finish.set()
        with pytest.raises(failure, match="load failed"):
            failed.result(timeout=5)
        with retry.result(timeout=5) as lease:
            assert isinstance(lease.instance, Match)
    assert store.metrics()["loading"] == 0


def test_invalidation_during_load_prevents_resurrection(store):
    entered, finish = Event(), Event()
    def loader():
        entered.set()
        assert finish.wait(5)
        return Match()
    with ThreadPoolExecutor(max_workers=2) as executor:
        loading = executor.submit(store.acquire, "match", loader=loader)
        try:
            assert entered.wait(5)
            waiting = executor.submit(store.acquire, "match", loader=Match)
            store.invalidate("match")
            with pytest.raises(MatchInvalidated):
                waiting.result(timeout=5)
        finally:
            finish.set()
        with pytest.raises(MatchInvalidated):
            loading.result(timeout=5)
    with pytest.raises(MatchInvalidated):
        store.acquire("match", loader=Match)
    assert store.metrics()["total"] == store.metrics()["loading"] == 0


def test_invalidation_drains_existing_leases_and_rejects_stale_lifecycle_events(store):
    lease = store.acquire("match", loader=Match)
    background = lease.fork()
    store.invalidate("match")
    assert store.metrics()["total"] == store.metrics()["protected"] == 1
    with pytest.raises(MatchInvalidated):
        store.mark_result(lease, "succeeded")
    with pytest.raises(MatchInvalidated):
        lease.fork()
    lease.close()
    assert store.metrics()["total"] == 1
    background.close()
    assert store.describe("match") is None


def test_invalidated_before_context_entry_does_not_leak_protection(store):
    lease = store.acquire("match", loader=Match)
    store.invalidate("match")
    with pytest.raises(MatchInvalidated):
        with lease:
            pytest.fail("invalidated lease entered")
    assert store.metrics()["total"] == 0


def test_inspection_and_downloads_do_not_touch_but_gameplay_does(store, clock):
    with store.acquire("match", loader=Match):
        pass
    clock.advance(INCOMPLETE_IDLE_SECONDS - 1)
    with store.acquire("match"):
        assert store.describe("match")["access_age_seconds"] == INCOMPLETE_IDLE_SECONDS - 1
        assert store.metrics()["incomplete_recent"] == 1
    assert store.sweep("incomplete")["removed"] == 0
    with store.acquire("match", touch=True):
        pass
    clock.advance(INCOMPLETE_IDLE_SECONDS - 1)
    assert store.sweep("incomplete")["removed"] == 0
    clock.advance(1)
    assert store.metrics()["incomplete_idle"] == 1
    assert store.sweep("incomplete")["removed"] == 1


def test_clock_adjustments_cannot_shorten_or_extend_grace(store, clock):
    with store.acquire("match", loader=Match) as lease:
        ready(store, lease)
    clock.wall += timedelta(days=365)
    assert store.sweep("completed")["removed"] == 0
    clock.advance(COMPLETED_GRACE_SECONDS)
    clock.wall -= timedelta(days=730)
    assert store.sweep("completed")["removed"] == 1


def test_metrics_are_consistent_copies_and_do_not_read_engine_properties(store, clock):
    with store.acquire("old", loader=lambda: Match("FC")):
        pass
    clock.advance(INCOMPLETE_IDLE_SECONDS)
    with store.acquire("new", loader=lambda: Match("Hundred")) as lease:
        ready(store, lease)
        # Metadata was captured on load/events; inspection never traverses Match.
        lease.instance.data = None
        metrics = store.metrics()
        assert metrics["total"] == 2
        assert metrics["completed"] == metrics["incomplete_idle"] == metrics["protected"] == 1
        assert metrics["incomplete_recent"] == 0
        assert metrics["archives"] == {"ready": 1, "pending": 0, "failed": 0}
        assert metrics["formats"] == {"FC": 1, "Hundred": 1}
        assert metrics["oldest_access_seconds"] == INCOMPLETE_IDLE_SECONDS
    assert store.sweep("incomplete")["removed"] == 1
    metrics = store.metrics()
    metrics["last_sweep"]["incomplete"]["removed"] = 999
    metrics["formats"].clear()
    assert store.metrics()["last_sweep"]["incomplete"]["removed"] == 1
    assert store.metrics()["formats"] == {"Hundred": 1}


def test_batch_eviction_releases_objects_even_when_closed_leases_are_retained(store, clock):
    refs, leases = [], []
    for index in range(30):
        lease = store.acquire(str(index), loader=Match)
        refs.append(weakref.ref(lease.instance))
        leases.append(lease)
        lease.close()
    assert store.metrics()["incomplete_recent"] == 30
    for index in range(30):
        with store.acquire(str(index)) as lease:
            ready(store, lease)
    assert store.metrics()["archives"]["ready"] == 30
    clock.advance(COMPLETED_GRACE_SECONDS)
    assert store.sweep("completed")["removed"] == 30
    gc.collect()
    assert store.metrics()["total"] == 0
    assert all(ref() is None for ref in refs)


def test_missing_closed_and_foreign_leases_are_rejected(store):
    with pytest.raises(MatchUnavailable):
        store.acquire("missing")
    with pytest.raises(MatchUnavailable):
        store.acquire("missing", loader=lambda: None)
    assert store.metrics()["loading"] == 0
    with store.acquire("match", loader=Match) as lease:
        with pytest.raises(RuntimeError, match="foreign"):
            MatchStore().touch(lease)
    with pytest.raises(RuntimeError, match="closed"):
        lease.instance
    with pytest.raises(RuntimeError, match="closed"):
        lease.fork()
    with pytest.raises(RuntimeError):
        store.mark_completed(lease)


def test_invalid_lifecycle_events_do_not_mutate_state(store):
    with store.acquire("match", loader=Match) as lease:
        before = store.describe("match")
        with pytest.raises(ValueError):
            store.mark_result(lease, "ready")
        with pytest.raises(ValueError):
            store.mark_archive(lease, 0, "pending")
        with pytest.raises(ValueError):
            store.mark_archive(lease, 0, "ready")
        assert not store.mark_archive(lease, 0, "ready", path="data/initial.zip")
        with pytest.raises(ValueError):
            store.sweep("anything")
        assert store.describe("match") == before


def test_discard_allows_reload_but_invalidation_does_not(store):
    with store.acquire("match", loader=Match) as lease:
        first = lease.instance
    assert store.discard("match")
    with store.acquire("match", loader=Match) as lease:
        assert lease.instance is not first
    store.invalidate("match")
    with pytest.raises(MatchInvalidated):
        store.acquire("match", loader=Match)


def test_owner_scope_orders_deletion_without_blocking_other_owners(store):
    entered, release, attempted, deleted = Event(), Event(), Event(), Event()
    def request():
        with store.scope('owner') as scope:
            scope.get('match', loader=Match)
            entered.set()
            assert release.wait(5)
    def deletion():
        attempted.set()
        with store.scope('owner'):
            store.invalidate('match')
            deleted.set()
    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(request)
        try:
            assert entered.wait(5)
            second = executor.submit(deletion)
            assert attempted.wait(5)
            def other():
                with store.scope('different') as scope:
                    assert scope.get('another', loader=Match)
            executor.submit(other).result(timeout=5)
            assert not deleted.is_set()
            assert store.metrics()['protected'] == 1
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert store.is_invalidated('match')
    assert store.describe('match') is None


def test_borrow_all_protects_inspection_without_refreshing_access(store, clock):
    with store.acquire('match', loader=Match):
        pass
    clock.advance(INCOMPLETE_IDLE_SECONDS)
    with store.borrow_all() as pairs:
        assert len(pairs) == 1
        assert store.sweep('incomplete')['protected'] == 1
        assert store.describe('match')['access_age_seconds'] == INCOMPLETE_IDLE_SECONDS
    assert store.sweep('incomplete')['removed'] == 1


def test_scope_tracks_offload_until_actual_finish_even_if_engine_drops_handle(store, clock):
    class Job:
        def ready(self):
            return False
        def rawlink(self, callback):
            self.callback = callback
    job = Job()
    with store.scope('owner') as scope:
        match = scope.get('match', loader=Match)
        match._fc_declaration_job = job
    assert store.metrics()['protected'] == 1
    with store.scope('owner') as scope:
        assert scope.get('match') is match
    assert store.describe('match')['protected'] == 1  # one fork, not one per poll
    match._fc_declaration_job = None  # engine deadline fallback
    clock.advance(INCOMPLETE_IDLE_SECONDS)
    assert store.sweep('incomplete')['protected'] == 1
    job.callback(job)
    assert store.sweep('incomplete')['removed'] == 1


def test_retire_drains_current_lease_before_reload(store):
    lease = store.acquire('match', loader=Match)
    first = lease.instance
    store.retire('match')
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(store.acquire, 'match', loader=Match)
        lease.close()
        with future.result(timeout=5) as new:
            assert new.instance is not first
    assert not store.is_invalidated('match')


def test_scope_exception_closes_leases_and_rejects_unscoped_access(store):
    with pytest.raises(RuntimeError, match='operation scope'):
        store.current_scope()
    with pytest.raises(ValueError):
        with store.scope('owner') as scope:
            scope.get('match', loader=Match)
            with store.scope('owner') as nested:
                assert nested is scope
                raise ValueError('request failed')
    assert store.metrics()['protected'] == 0
    with pytest.raises(RuntimeError, match='operation scope'):
        store.current_scope()


def test_real_offload_completion_releases_on_the_gevent_hub(store, clock):
    from gevent import monkey
    if not monkey.is_module_patched('threading'):
        pytest.skip('Native-thread run: offload is synchronous without a gevent hub')
    from gevent.event import Event as GreenEvent
    from utils.cpu_offload import start_offloaded
    gate = monkey.get_original('_thread', 'allocate_lock')()
    gate.acquire()
    done = GreenEvent()
    with store.scope('owner') as scope:
        match = scope.get('match', loader=Match)
        # Only a native lock is touched on the CPU thread; no gevent objects.
        job = match._fc_declaration_job = start_offloaded(gate.acquire)
    try:
        clock.advance(INCOMPLETE_IDLE_SECONDS)
        assert store.sweep('incomplete')['protected'] == 1
        match._fc_declaration_job = None
        job.rawlink(lambda _: done.set())
    finally:
        gate.release()
    assert done.wait(timeout=5)
    assert store.sweep('incomplete')['removed'] == 1


def test_retained_offload_callback_does_not_keep_evicted_instance_alive(store, clock):
    class Job:
        def ready(self):
            return False
        def rawlink(self, callback):
            self.callback = callback
    job = Job()
    with store.scope('owner') as scope:
        match = scope.get('match', loader=Match)
        ref = weakref.ref(match)
        match._fc_declaration_job = job
    del match
    job.callback(job)  # A caller may retain the completed job and its callback.
    clock.advance(INCOMPLETE_IDLE_SECONDS)
    assert store.sweep('incomplete')['removed'] == 1
    gc.collect()
    assert ref() is None
