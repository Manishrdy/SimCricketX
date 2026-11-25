# Match instance lifecycle development plan

Status: Phase 1 registry migration implemented; application consumers use the
shared store. Phases 2–4 remain pending and early completed eviction is disabled.
See the latest implementation checkpoint at the end for validation and scope.
Intended developer: Astra.
Prepared: 2026-11-21.

## Objective and scope

Implement the user's selected proposals:

1. Release completed match instances after successful post-match processing.
2. Preserve archive access and retries independently of cached instances.
5. Centralize match-instance lifecycle management.
6. Improve cache visibility in `/admin/health`.

Preserve gameplay, resume behavior, completed results, tournament statistics,
archive contents, ownership enforcement, and existing archive retention rules.
Do not change cricket simulation behavior or implement commentary deduplication
as part of this work.

## Is proposal 4 already fixed?

Only partially. The current working tree changes inactive-instance retention from
24 hours to **12 hours**, and inactivity cleanup from six hours to **four hours**.
Removal therefore normally occurs approximately 12–16 hours after last access.
These changes take effect when the application restarts.

The proposed **one-hour eviction of paused matches is not implemented** and is
outside this scope. Recovery support exists for FC, Hundred, and Super Overs,
but full restoration across every format and every delivery has not been
established by this review. Do not assume setup JSON alone restores a live game.
Keep the existing incomplete-match policy during this implementation.

## Approved target behavior

| Area | Target |
| --- | --- |
| Completed instances | Eligible for removal 15 minutes after both successful result persistence and final archive readiness, provided no operation is using them. |
| Failed or pending post-match work | Do not apply short retention while required persistence or archiving is unfinished. Preserve recoverable data and expose failures. |
| Incomplete matches | Keep the 12-hour inactivity threshold and four-hour cleanup interval. |
| Completed cleanup scheduler | Sweep approximately every minute so the 15-minute grace period is meaningful. |
| Archive downloads | Work after instance eviction and application restart using durable ownership and archive metadata. |
| Registry access | Use a shared match-store service, including protection against concurrent eviction. |
| Health page | Show cache composition, age, archive readiness, protected instances, and cleanup outcomes. |
| Release | Update version and changelog; expected next patch version is 3.0.4, subject to rechecking current release state. |

## Repository precautions and investigation

Read `AGENTS.md` before implementation. Preserve existing uncommitted edits;
the working tree already contains unrelated engine, route, UI, test, and
changelog changes. Do not reset or overwrite them.

Review these integration points:

- `app.py`: `MATCH_INSTANCES`, its lock, inactivity cleanup, completion
  persistence, background workers, Socket.IO handlers, active-match display.
- `routes/match_routes.py`: restoration, live state, delivery, completion,
  commentary uploads, scorecard uploads, archive creation/download, deletion.
- `engine/match.py`: automatic archive creation and temporary JSON deletion.
- `match_archiver.py`: archive contents, locations, ownership, validation,
  and database/statistics side effects.
- `services/admin_workspace.py`: health loaders and shared cleanup interval.
- `templates/admin/partials/system_overview.html` and `system_tasks.html`.
- Tournament deletion/replay and every other reader or writer of the registry.
- Existing completed-match database fields, archive metadata, and migrations.

Confirm actual archive paths and relationships from the code rather than
assuming all archives use the same directory. Identify every consumer of a
completed instance before changing its lifetime.

## Phase 1: Shared match-store service (proposal 5)

Create a service such as `services/match_store.py` that owns the registry and
lifecycle metadata. Preserve the application's single-worker deployment model.

### Required operations

| Operation | Responsibility |
| --- | --- |
| Acquire/release | Return an instance and protect it for the full duration of an operation. Prefer a context manager with guaranteed release. |
| Load/restore | Coordinate loading so concurrent requests cannot create different live instances for the same match. |
| Touch | Update meaningful gameplay access consistently. |
| Mark completion | Record successful result persistence separately from archive readiness. |
| Mark archive state | Track pending, successful, or failed final archiving and relevant timestamps. |
| Remove/invalidate | Handle completed eviction, inactivity cleanup, explicit deletion, and tournament replay. |
| Metrics snapshot | Return an inexpensive, consistent aggregate view for the health page. |

### Concurrency requirements

- Never evict during simulation, pending captain calculations, commentary or
  image uploads, archive building, or another operation that needs the instance.
- Protect background work until it actually finishes, including work that
  outlives the HTTP request that started it.
- Release protection on exceptions, cancellation, and normal completion.
- Do not hold the global registry lock during simulation, database access,
  archive generation, or filesystem scans. Define and document lock ordering.
- Prevent stale requests from reinstating a deleted or replayed match.
- Admin inspection and ordinary archive downloads must not refresh gameplay
  inactivity timestamps.
- Migrate direct dictionary accesses deliberately. Avoid independent registries
  or compatibility aliases that can drift from the authoritative store.
- Use appropriate clocks: monotonic time for in-process elapsed durations and
  durable UTC timestamps for persisted lifecycle events.

Validate this phase before enabling new eviction behavior.

## Phase 2: Durable completed data and archives (proposal 2)

Implement before enabling early completed-match eviction. Current archive
generation can fall back to cached match metadata after temporary JSON deletion;
that dependency must be removed safely.

Inspect existing database/archive structures first. Reuse sufficient fields;
add migrations only when needed. Durable state should identify:

- Match ID and owner.
- Successful result-persistence status.
- Final archive status, location, and completion time.
- Inputs needed for supported recovery or rebuild after cache loss/restart.
- Response metadata used by the existing browser workflow, including milestones
  where required.

### Archive request flow

1. Authorize using durable ownership information.
2. Locate a valid, current final archive. If available, return its existing
   download information without rebuilding it or constructing a full `Match`.
3. If missing or invalid, rebuild from durable completed data where supported.
4. If recovery is unavailable, return an actionable failure and record it.
5. Never reconstruct a fresh simulation from setup metadata and present it as
   the completed match.

### Required safeguards

- Distinguish the engine's initial ZIP from the final browser-enriched archive
  containing commentary and scorecard uploads. Define final readiness explicitly;
  initial ZIP creation alone must not start the eviction timer.
- Document the post-match upload protocol and its completion signal. Avoid a
  race where the instance is considered final before all required uploads finish.
- Write through temporary files and publish validated archives atomically.
- Coordinate concurrent archive requests and use idempotent completion handling.
- Repeated requests or archive rebuilds must not duplicate career statistics,
  milestones, match records, tournament standings, or other side effects.
- Track accepted late commentary/images and invalidate/rebuild the final archive
  when necessary. Do not return a stale ZIP as the final output.
- Preserve ownership checks, path safety, and existing archive retention rules.
- Support older archives that lack new lifecycle metadata where possible.
- Define behavior for deleted archives, missing legacy metadata, and corrupt ZIPs.
- Keep durable fallback records subject to a documented retention policy aligned
  with existing match/archive retention; avoid creating unbounded disk growth.

## Phase 3: Completed-match eviction (proposal 1)

A completed instance is eligible only when all conditions hold:

```text
result persistence succeeded
AND final archive is durably saved and validated
AND 15 minutes have elapsed since both conditions became true
AND no operation is using the instance
```

- Add a lightweight completed-instance sweep approximately every minute.
- Keep the four-hour scheduler for 12-hour incomplete-match inactivity cleanup.
- Start the completed grace period when both durable conditions succeed.
- Ordinary downloads and admin views must not extend this grace period.
- New accepted archive inputs must be processed consistently before eviction.
- On failure, preserve the instance for retry and record visible diagnostics.
- Define a bounded fallback for a browser that closes before commentary/image
  uploads finish. Persist available completed data before eventual removal,
  explicitly record incomplete enrichment, and do not silently discard the only
  recoverable state. Document the chosen timeout and fallback behavior as an
  implementation decision; the user has not specified its duration.
- Audit general inactivity cleanup so it cannot bypass durable-data safeguards
  for completed matches.
- After eviction, results and archive downloads must work without re-creating
  the simulation. A stale live-page request must not start a completed game again.
- Startup/shutdown handling must not launch duplicate sweep workers within the
  same process or leak workers across repeated app construction in tests.

## Phase 4: Admin health metrics (proposal 6)

| Metric | Definition |
| --- | --- |
| Total instances | Full match objects currently retained. |
| Incomplete recent / idle | Separate by a documented inactivity threshold; idle does not prove that a user abandoned a match. |
| Completed instances | Finished matches still cached. |
| Archive ready / pending / failed | Post-match processing states for completed cached instances. |
| Format breakdown | Counts by supported match format. |
| Oldest access | Age of the least recently accessed cached instance; define handling for legacy missing timestamps. |
| Protected instances | Matches excluded from eviction because operations are using them. |
| Cleanup outcomes | Last successful sweep, removed counts by reason, skipped/protected counts, and failures. |
| Completed policy | 15-minute grace and approximately one-minute sweep. |
| Incomplete policy | 12-hour inactivity threshold and four-hour cleanup interval. |

Keep page loads inexpensive and preserve existing admin authorization. Do not
scan all archives or calculate recursive object sizes on each refresh. Optional
sampled size estimates can follow later; they are not required for this release.
Do not imply cache metrics account for the entire process RSS. State whether
counters are process-local and reset on restart. Distinguish incomplete-game
cleanup status from completed-game sweep status.

## Validation and acceptance criteria

Use shared QA accounts for authenticated testing according to `AGENTS.md` and
`.claude/TEST_ACCOUNTS.md`. Never commit or expose their credentials. Do not
create additional users.

Use a controllable clock and explicit lifecycle events instead of real waiting.
Add meaningful focused tests for:

- Before, at, and after the 15-minute completed-eviction boundary.
- Failed or pending result persistence and archive generation preventing early
  eviction.
- Protected operations blocking eviction, including deferred background work.
- Exceptions releasing protection correctly.
- Concurrent loading and concurrent/repeated archive requests.
- Archive downloads after clearing the registry and simulated application restart.
- Missing/corrupt archives, durable rebuilds, and unrecoverable failures.
- Late commentary/images and an interrupted browser post-match workflow.
- Duplicate completion/archive calls causing no duplicate statistics or records.
- Unauthorized users being denied access before and after cache eviction.
- Tournament deletion/replay racing with cache access and archive operations.
- The 12-hour incomplete inactivity policy and four-hour scheduling remaining
  intact, including recently resumed and legacy instances.
- Health counts/timestamps being correct without touching gameplay access.
- Existing archive retention and legacy archive compatibility.

Run relevant existing FC, Hundred, Super Over, archive, tournament-completion,
and admin-workspace tests, selecting exact test files after repository inspection.
Include `tests/test_fc_resume.py`, `tests/test_super_over_resume.py`, applicable
Hundred integration tests, and changelog-page tests. Report pre-existing failures
separately and do not conceal unrelated working-tree changes.

Perform a controlled batch-of-completed-matches check. Record cached counts by
state before completion, after archive readiness, and after the grace period.
Confirm released instances are no longer retained by the store or workers.
Measure process memory as supporting evidence, but do not require RSS to fall
immediately: Python may retain freed allocations for reuse. Do not claim a
specific memory saving without measurement.

## Version and changelog

At planning time, `version.txt` and the latest changelog entry are **3.0.3**.
Recheck before editing. If unchanged, release this work as **3.0.4**; otherwise
coordinate with the current release state and avoid duplicate version headers.

Update:

- `version.txt`.
- `changelog.txt`, preserving existing uncommitted entries and all release history.
- Any other version surfaces actually derived or maintained separately, after
  inspecting how the application displays its version.

Use the actual release date, not automatically the plan date. Suggested entries
must be adjusted to match verified final behavior:

- Release completed match instances after results and final archives are safely
  saved, with a short grace period for post-match requests.
- Preserve archive downloads after cached matches are removed or the server
  restarts.
- Protect active match operations during cache cleanup.
- Show cached-match status, format breakdown, and cleanup results in System Health.
- Reduce inactive-match retention to 12 hours and run inactivity cleanup every
  four hours.

Run changelog-page tests and verify displayed version consistency. Do not bump
the version or describe features as shipped before implementation is complete.

## Delivery sequence and developer report

Implement sequentially: shared lifecycle service, durable archive recovery,
completed eviction, health metrics, then release documentation. Run relevant
checks at each stage. Keep code changes reviewable and preserve the scope limits.

Provide a final report containing:

- Changed files and reasons.
- Final lifecycle rules, upload-completion contract, and fallback decisions.
- Any schema migration, compatibility/backfill needs, and rollback considerations.
- Test commands/results and any pre-existing failures.
- Observed cached-instance counts and memory-check limitations.
- Known limitations, particularly incomplete-format restoration outside this scope.
- Version/changelog update and restart/deployment instructions.

Do not deploy or restart production as part of preparing the implementation
unless separately authorized.

## Implementation checkpoint — 2026-11-21

### Completed in the first increment

- Added `services/match_store.py`: a standalone `MatchStore` with one private
  registry, context-managed leases, independently releasable background leases,
  and an executor helper covering completion, exceptions, queued cancellation,
  and failed submission.
- Concurrent loads for one ID share one instance. Loading runs outside the
  registry lock. Failure wakes waiting callers and permits retry. Invalidation
  blocks new operations and publication of in-flight loads; existing protected
  instances are retained until their leases drain.
- Result persistence and final archive readiness are separate explicit events.
  Archive revisions reject stale completion events after accepted new inputs
  or a new build attempt. Repeated successful events do not extend the grace
  period. Readiness is a caller assertion made **after** durable validation;
  the service itself does not perform persistence or ZIP validation.
- Added callable cleanup policy primitives (no scheduler): 15 minutes from both
  successful events for completed matches, 12-hour inactivity for incomplete
  matches, and no removal while protected. Completed pending/failed work cannot
  fall through to the incomplete cleanup policy.
- Added metadata-only inspection and aggregate metrics. Access/grace durations
  use monotonic time; lifecycle event times are timezone-aware UTC. New loads,
  including legacy instances with no access timestamp, start their inactivity
  clock at load time. Admin/download access defaults to no touch; authorized
  gameplay must explicitly touch.
- Added `tests/test_match_store.py` with 36 cases covering concurrency,
  invalidation, failed loads, background work, late archive inputs, idempotent
  events, grace boundaries, clock changes, inactivity, and reference release.

This increment deliberately does **not** instantiate a second application
registry or redirect existing callers. `app.py` still owns the existing
dictionary until the migration below is completed. Phase 1 is **not complete**.
The service is not imported by production code, and there is no behavior,
schema, version, changelog, deployment, or restart change in this increment.
Existing uncommitted application/engine/UI edits were preserved.

### Investigation findings to carry into integration

- `app.py` owns the current dictionary and inactivity worker. Match, tournament,
  admin, fixture-replay, and identity services all consume it; migrating only
  match routes would leave unprotected paths. Tests also insert/remove directly.
- `_get_or_restore_match_instance` currently performs file/database access and
  engine construction under the global registry lock. Migration should use
  `acquire(..., loader=...)` and retain its lease through the entire operation,
  including the HTTP and Socket.IO delivery paths.
- `MatchLease` protects lifetime, not simulation serialization or ownership.
  Retain delivery/file/transaction guards. Check ownership on cached instances
  as well as loads, and touch only after authorization. Invalidation fences
  store publication; deletion/replay must still serialize durable side effects.
  Process-local tombstones retain IDs only and reset on restart, so durable
  replay/deletion checks must remain in the loader.
- FC declarations use `utils.cpu_offload.start_offloaded`, which is not the
  `concurrent.futures.Executor` API. Its lifecycle needs an explicit fork and
  release adapter, including deadline fallback; the new executor helper alone
  does not integrate this worker. Inspect its snapshot-only computation and
  pending result handling when defining the full protection duration.
- `match_archiver.py` publishes ZIPs in `<PROJECT_ROOT>/data`, matching
  `ARCHIVES_FOLDER`. Some comments still mention `data/files`; do not use those
  comments as authoritative paths. ZIP creation currently writes directly to
  the final filename, then validates it. Atomic publication remains to implement.
- `create_archive()` also writes non-tournament database/statistics state and
  cleans temporary files. Archive-only retries need separation from those
  side effects, including durable milestone response metadata.
- `/download-archive` falls back to cached metadata after JSON cleanup and can
  construct `Match(match_meta)` when the cache is absent. This is not a valid
  completed-state recovery path and must be replaced before enabling eviction.
- `saveMatchArchive()` waits for the current scorecard upload promises, posts
  commentary, then requests `/download-archive`. It does not check the commentary
  response status; settled image promises do not establish successful uploads.
  This is not yet a reliable final-enrichment completion contract. Define and
  verify that contract in Phase 2 before marking archives ready.
- `DBMatch` has durable ownership and result/statistics fields but no explicit
  final-archive readiness/location or full rebuild record. Inspect reuse of
  existing archive data before selecting migration/storage details. Existing
  archive retention defaults to seven days via `ARCHIVE_MAX_AGE_DAYS`.
- Current orphan JSON cleanup uses file age alone. Its 24-hour deletion must
  be audited alongside completed-data safeguards during integration.

### Validation and remaining work

Commands run successfully:

```sh
.venv/bin/python -m pytest tests/test_match_store.py -o addopts='-q --tb=short'
# 36 passed (application/gevent setup); existing late SSL monkey-patch warning.
.venv/bin/python -m pytest --noconftest tests/test_match_store.py -o addopts='-q --tb=short'
# 36 passed (native threads).
```

These tests need no logged-in users and create no accounts. A synthetic batch
of 30 instances measured **30 incomplete → 30 completed/archive-ready → 0 cached**
after the controlled grace period. All weak references cleared after collection,
including with closed lease objects still retained. This proves service reference
release only; no engine batch/RSS measurement or application-level memory-saving
claim has been made. Existing FC, Hundred, Super Over, archive, tournament and
admin integration suites remain required when production paths are migrated.

Next: replace the authoritative registry and migrate every consumer to protected
operations, integrate background jobs and durable invalidation guards, then run
the Phase 1 integration regressions before starting durable archive recovery.
Do not enable completed cleanup from the standalone policy tests alone.
Browser-abandonment timeout and durable fallback retention decisions remain for
Phases 2–3; no timeout is chosen or implicitly approved by this increment.
`version.txt` and the latest changelog header remain `3.0.3`; recheck before the
eventual release bump. Rollback of this increment requires only removing the
unused new service/tests and this checkpoint; there is no data migration.

## Registry migration checkpoint — 2026-11-25

This checkpoint supersedes the first increment's pending-integration status.
The user's subsequent instructions authorized the full registry migration and
selected a full stop/deploy/start rollout. No production operation was performed.

### Implemented integration

- `app.py` now owns one `MATCH_STORE`, also exposed through
  `app.extensions['match_store']`. The production `MATCH_INSTANCES` dictionary
  and its public lock have been removed. There is no parallel registry or
  production dictionary compatibility alias.
- `services/match_access.py` provides request boundaries. All match routes,
  tournament routes, and nested tour routes run in operation scopes. The same
  scope mechanism wraps the shared HTTP/WebSocket delivery helper. Cached
  instances, including those used only indirectly during image uploads, remain
  leased through the operation's completion or exception.
- Scopes serialize operations **per owner**, including deletion/replay and
  archive uploads/builds. Different owners have independent gates. This is an
  intentional tradeoff: two matches belonging to the same account do not mutate
  concurrently. Gate records are removed when no operation is waiting or active.
  Lock order is identity admission → owner operation gate → file/delivery locks
  → short store metadata locks. Store methods do not wait on owner/file locks
  while holding the registry lock, or hold it during restoration/database/file I/O.
- Restoration uses the store's coordinated loader, with ownership checks before
  gameplay touch. Completed setup JSON is rejected rather than constructing a
  fresh simulation. Durable replay tombstones remain checked, and missing,
  reassigned or deleted tournament fixtures cannot be restored from stale JSON.
- Toss refresh retires a cached instance for a fresh load. Deletion/replay
  invalidates the ID; protected objects drain before their references are removed.
  Tournament/tour and loose-match invalidation follows successful commit.
  In-process tombstones retain IDs only; durable replay checks remain necessary
  across process restarts.
- FC offloaded captain work receives a separate lease via the gevent completion
  callback. It remains protected if the engine drops the job handle after a
  deadline fallback. The callback runs on the gevent hub and releases on actual
  completion without a polling worker. Closed leases and retained completion
  callbacks do not keep an evicted instance alive.
- Admin counts, active-match display, temporary image cleanup, fixture replay,
  identity email review, account merge and account deletion now use store
  snapshots/counts/invalidation. Existing identity admission, policy and ownership
  rules remain unchanged. Identity merge retires cached ownership state so it
  can reload; deletion permanently invalidates it for this process.
- Incomplete cleanup uses the existing 12-hour policy and four-hour scheduler.
  It skips leases and completed entries. Age-only orphan JSON cleanup now protects
  cached and completed snapshots and rechecks under the owner's operation gate.
  Legacy/newly loaded entries start their monotonic inactivity clock at loading;
  there is no conversion of wall-clock instance fields into elapsed time.
- Result persistence success/failure is recorded separately from engine completion.
  Non-tournament persistence no longer reports success when `_save_to_database()`
  returns false, and retry attempts do not increment the match counter again.
  Accepted commentary/images invalidate archive readiness; archive build failures
  are recorded. Successful current browser builds remain **pending final-readiness
  certification** until Phase 2 defines the upload protocol and durable storage.
- An archive build without a cached completed instance now returns an actionable
  failure directing the user to existing archives. It no longer makes a fresh
  `Match` from setup metadata and exports it as a completed result. Existing ZIP
  downloads continue through their existing ownership-checked route.

### Tests and compatibility

Legacy test fixtures now use `tests/match_store_helpers.py`, a **test-only**
mapping over the real store. It owns no data and is never imported by production.
App fixtures use isolated stores; a test's `monkeypatch.undo()` cannot swap out
the store behind its registered routes. Manual-mode tests use the shared QA
accounts instead of a synthetic login. A pending FC forecast is retired during
test teardown, rather than bypassing its active lease.

Validation includes native threads and the app's gevent runtime; concurrent
loading, per-owner serialization, background completion/cancellation, HTTP and
WebSocket delivery, image/commentary/archive protection, unauthorized access,
deletion/replay, orphan cleanup, failed persistence and stale completed setup
metadata. Fake clocks cover retention boundaries without real retention waits.
The controlled 30-instance batch and weak-reference assertions remain service
reference-release evidence, not an RSS saving claim.

Validation results (shared QA accounts for authenticated application tests):

```sh
.venv/bin/python -m pytest -p tests.qa_accounts_plugin \
  tests/test_match_store.py tests/test_match_store_integration.py \
  tests/test_fc_declaration_stall.py tests/test_hundred_integration.py \
  tests/test_tournament_review_fixes.py tests/test_fixture_replay.py \
  tests/test_changelog_page.py -o addopts='-q --tb=short --maxfail=3'
# Final closeout: 146 passed; existing late SSL monkey-patch warning.

.venv/bin/python -m pytest --noconftest \
  tests/test_match_store.py tests/test_deploy_offline.py \
  -o addopts='-q --tb=short --maxfail=3'
# Native-thread/deployment checks: 56 passed, 1 expected skip (gevent-only offload).
```

The broader 25-file regression run covered store integration, delivery, match
routes, tournaments, replay, admin workspaces, transport parity, toss, first-ball
restore, manual mode, live innings, FC/Super Over resume, Hundred, FC pending
decisions/stalls/archives/end-to-end, T10, header/logo display and archive ownership.
It produced **370 passed and one teardown failure**: the dropped-socket card test
attempted to force-discard an instance with a captain-job lease still draining.
The test now retires it; its full file passed in the final 146-test run above.
Earlier identity email/merge/campaign and changelog tests passed as part of a
174-pass run whose only failure was a new integration-test directory setup
collision. That collision was fixed and the entire new integration file passed
at closeout. No unresolved failure remains in the selected validation.

`git diff --check` and Python compilation passed. A production-source search
found no remaining `MATCH_INSTANCES`/`MATCH_INSTANCES_LOCK` accesses. Existing
unrelated engine, release, retention and UI work was preserved; no simulation
behavior or identity policy change was introduced by this migration.

### Release and remaining lifecycle work

- The workspace independently advanced to **3.0.5** (commit `da7b87c`), with a
  **three-day** default ZIP retention introduced in 3.0.4. These changes are
  preserved. The earlier expected 3.0.4 lifecycle version and seven-day retention
  assumptions are historical; recheck release state before shipping Phases 2–4.
- No schema migration or completed-data backfill was introduced by this registry
  migration. A full application restart switches all consumers to the store.
  Rollback must revert the store and its consumers together; restoring the old
  dictionary alone is incompatible with the new route signatures.
- **Completed eviction remains disabled.** Completed cached instances are retained
  while durable recovery/final-readiness certification is missing. This intermediate
  state does not deliver the planned completed-match memory reduction.
- Phase 2 still needs durable completed/archive data, atomic ZIP publication,
  archive-only idempotent retries, final upload-completion certification and
  retention-aligned rebuild records. Phase 3 still needs the completed scheduler
  and interrupted-browser fallback. Phase 4 still needs the expanded health UI.
  Do not describe those features as shipped or enable the completed sweep yet.
- `scripts/deploy_offline.py` and `docs/operations/offline-rollout.md` prepare the
  selected offline rollout: read-only by default, exact commit, stopped-service
  backup with SQLite WAL-safe copying, migrations, restart, health check, and
  retained recovery material. The existing deployment workflow was not replaced.
  No production migration, restart, rollout or rollback has been run.
