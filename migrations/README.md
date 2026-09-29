# Checking database migration status

After applying your planned migrations, run:

```bash
python3 -m migrations.precheck --check --db ./cricket_sim.db
```

The equivalent standalone command offers JSON output and strict manual-review handling:

```bash
python3 -m migrations.verify --db ./cricket_sim.db
python3 -m migrations.verify --db ./cricket_sim.db --json
python3 -m migrations.verify --db ./cricket_sim.db --strict
```

**Keep `--check` on the precheck command.** The existing precheck without that
flag applies startup migrations to the selected database.

## What verification does

The source database is opened with SQLite `mode=ro`. SQLite's backup API makes
a consistent temporary snapshot, including committed WAL contents. A minimal
Flask application is bound only to that snapshot; the production application
factory, startup jobs, and match engine are not imported by the verifier.

The verifier checks SQLite integrity and foreign-key violations, and compares
required model tables, columns, indexes, unique constraints and foreign keys.
It then rehearses the startup migration registry in order on the snapshot,
including data backfills, followed by additional standalone migrations.
Community/support table deletion, admin-audit deletion, orphan cleanup and the
cache-table rebuild are applied **only to the disposable snapshot**. Logical
schema/data hashes before and after each step identify pending changes. Exception
logging is intercepted so verification does not send GitHub notifications.

Results describe the database at snapshot time. An earlier rehearsal may satisfy
a later migration; read the whole report, including `model_schema`. A pending
step means that script would change the database; it is not permission to apply
it blindly. Review its own dry run and operational requirements. Model drift
may need a dedicated repair when an existing migration does not fix it.

The temporary directory is removed on completion, including handled failures.
Allow free temporary disk space for a full database copy and rebuilt tables.
This is a manual post-deployment check, not an additional full scan on app boot.
Hashing scans the snapshot for each migration, so large databases take longer.

## Status and exit codes

- `CURRENT`: the structural check passed, or the rehearsal made no logical change.
- `PENDING`: missing schema requirements or a migration would change the snapshot.
- `ERROR`: integrity failure, exception, or reported migration failure/skip.
- `UNVERIFIED`: a new standalone script has no verification policy.
- `MANUAL`: optional historical repair/recovery needs operator review or outside evidence.

Exit `0` means automated checks are current; `1` means pending changes; `2` means
verification failed or is incomplete. `--strict` also returns `2` for `MANUAL`
items. Default success explicitly does **not** certify those manual repairs.
Backup recovery, knockout bracket repair and tournament standings repair are
listed individually, never silently marked applied. Optional player-to-pool
identity linking is also outside the schema migration's automatic scope.

This verifies current effects, not migration execution history. There is no
historical migration ledger to reconstruct. It does not prove that all past
match data is complete, nor compare every SQL type/default/check constraint or
search-index content. Do not interpret a no-op rehearsal as proof of those facts.

## Adding migrations

Startup migrations are discovered from `migrations.precheck.MIGRATIONS`.
Standalone scripts must be added to `EXTRA` with an appropriate `_runner` adapter,
or to `MANUAL` with a concrete explanation. Any unclassified `.py` script in
this directory fails coverage with `UNVERIFIED`. Add tests for its expected
pending/current behavior; never point a verification runner at the source path.

The verifier's syntax supports Python 3.8 and newer. It uses the application's
installed Flask/SQLAlchemy dependencies; this does not upgrade the VM runtime.
