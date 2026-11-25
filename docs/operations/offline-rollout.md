# Offline production rollout

**The complete match-instance lifecycle feature is not ready to deploy.** The
registry migration is implemented; durable archive recovery, scheduled completed
eviction and expanded health-page metrics remain to implement.
`deploy_offline.py` deploys a chosen, already tested commit. It does not perform
those remaining code changes or turn registry migration into the completed feature.

## Deployment model

Use `scripts/deploy_offline.py` for the user's selected full
stop → backup → update → migrate → start rollout. This is an optional manual
alternative to `scripts/deploy.sh`; the existing GitHub Actions workflow has
not been changed. Never run the two deployment paths concurrently. Pause any
external cron jobs or other processes that write application data for the
maintenance window; stopping the systemd unit stops only its own processes.

The defaults match the checked-in deployment configuration:

- Checkout: `/home/ubuntu/SimCricketX`.
- Systemd service: `simcricketx`, with that checkout as its `WorkingDirectory`.
- Virtualenv: `/home/ubuntu/SimCricketX/venv`.
- Database: `<checkout>/cricket_sim.db`.
- Direct health check: HTTP 200 from `http://127.0.0.1:5000/`, matching Gunicorn.
- Backups: sibling `SimCricketX-rollout-backups` directory; each deployment
  gets a private directory with mode `0700`.

Confirm these against the actual server. Override `--app-dir`, `--service`,
`--venv` and `--backup-root` as needed. Run as the existing deployment user with
sudo access to systemctl, not by running the whole script as root. A nonstandard
database path, service environment/configuration, port, or symlinked data layout
needs adaptation before applying this script. Service-only environment overrides
are not imported into the migration process; use the same application environment
as the existing manual migration/deployment procedure.

## Commands (after the release is complete, committed and pushed)

Use the **full SHA of the verified release**, not a moving branch name. Fetching
updates Git refs but does not change the running code. Run the rollout script
from outside the checkout so checkout updates cannot replace the script file.

```sh
APP_DIR=/home/ubuntu/SimCricketX
RELEASE_COMMIT=REPLACE_WITH_FULL_VERIFIED_COMMIT_SHA

git -C "$APP_DIR" fetch origin main
ROLLOUT_DIR=$(mktemp -d /tmp/simcricketx-rollout.XXXXXX)
git -C "$APP_DIR" show "$RELEASE_COMMIT:scripts/deploy_offline.py" > "$ROLLOUT_DIR/deploy_offline.py"

# Read-only preflight: shows previous/target commit and target version.
python3 "$ROLLOUT_DIR/deploy_offline.py" --app-dir "$APP_DIR" --commit "$RELEASE_COMMIT"

# During the planned maintenance window, apply that exact release.
python3 "$ROLLOUT_DIR/deploy_offline.py" --app-dir "$APP_DIR" --commit "$RELEASE_COMMIT" --apply
```

Use a maintenance window with no ongoing matches: stopping the server clears
in-memory objects, and ordinary innings restoration across every format is not
guaranteed. This rollout does not implement new recovery support.

## What apply does

1. Acquires an exclusive deployment lock, verifies a clean checkout, resolves
   the exact commit and checks service/virtualenv/database paths. SQLite's
   untracked WAL/SHM/journal sidecars are allowed; never delete them to make
   the checkout appear clean.
2. Stops the service and verifies it is inactive/failed with `MainPID=0`.
3. Makes a SQLite backup using the backup API, which includes committed WAL
   data, and checks the backup's integrity. Copies the entire `data` directory,
   including archives and match files, plus local `.env`, configuration,
   requirements and version. Records the old commit and installed packages.
4. Rechecks the checkout, resets tracked code to the chosen commit, installs
   its requirements, and runs `migrate.py --non-interactive`.
5. Starts the service and checks systemd plus direct HTTP health, with retries.
   Keeps the backup after success.

The migration subprocess disables background workers and global app creation
using `SIMCRICKETX_TEST_MODE=1` and `SIMCRICKETX_SKIP_GLOBAL_APP=1`. It explicitly
clears `SIMCRICKETX_TEST_DB_URI` and enables schema precheck, so migration still
targets the checkout's production database. Those subprocess-only settings
**do not change the systemd server environment**.

The backup includes private configuration and user data. Keep its directory
private, allow sufficient disk space for the full data copy, and remove old
successful rollout backups under the existing backup retention policy. This
script deliberately retains them rather than silently deciding retention.

## Failure and recovery

After a failure in the stopped/deploying portion, the script attempts to stop
the service, reports the error and backup location, and exits nonzero. If sudo
or service control fails, it explicitly reports that the service state needs
checking. It never reports successful rollback or restores data automatically.

Inspect `<backup>/manifest.json`: only `backup_complete: true` means the database,
data and configuration snapshot finished. An incomplete backup must not be used
as a complete restore source. Deployment does not update code until backup and
the package inventory have succeeded.

Keep the service stopped while assessing recovery. For a rollback, first preserve
the failed deployment's current database/data, then restore the recorded previous
commit, dependencies and coherent database/data backup using the existing recovery
procedure. Do not copy an old database over a running server or mix it with newer
WAL/SHM files. If startup accepted any user writes, those must be reconciled before
restoring the older snapshot; automatic restore could lose them.

This is rollout tooling, not a substitute for application acceptance tests.
The local tests use disposable Git repositories/SQLite databases and mocked
systemd, sudo, pip, migrations and HTTP. No production rollout has been rehearsed
by these tests.
