#!/bin/bash
# ============================================================
# SimCricketX — server-side deploy script
# Triggered by GitHub Actions on every push to main.
#
# What it does:
#   1. Pulls latest code from origin/main
#   2. Re-installs pip deps if requirements.txt changed
#   3. Snapshots the database, then runs migrations
#   4. Restarts the systemd service
#   5. Runs health checks; rolls back on failure
#
# Configuration (override via environment or edit defaults below):
#   APP_DIR      — absolute path to the app on the server
#   SERVICE_NAME — systemd unit name
# ============================================================
set -euo pipefail

# ── Configuration ────────────────────────────────────────────
APP_DIR="${APP_DIR:-/opt/simcricketx}"
SERVICE_NAME="${SERVICE_NAME:-simcricketx}"
VENV="$APP_DIR/venv"
HEALTH_URL="http://127.0.0.1:5000"
HEALTH_RETRIES=5
HEALTH_WAIT=4        # seconds between health-check retries
LOG_DIR="/var/log/simcricketx"
LOG_FILE="$LOG_DIR/deploy.log"
DB_PATH="$APP_DIR/cricket_sim.db"
DB_SNAPSHOT="$APP_DIR/cricket_sim.db.deploy_snapshot"
# ─────────────────────────────────────────────────────────────

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

rollback() {
    local prev_commit="$1"
    log "ROLLBACK: reverting code to $prev_commit"
    git -C "$APP_DIR" reset --hard "$prev_commit"

    # Restore database snapshot taken before migration
    if [ -f "$DB_SNAPSHOT" ]; then
        log "ROLLBACK: restoring database snapshot"
        cp "$DB_SNAPSHOT" "$DB_PATH"
    fi

    log "ROLLBACK: reinstalling dependencies"
    "$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"

    log "ROLLBACK: restarting service"
    sudo systemctl restart "$SERVICE_NAME"
    sleep 3

    if curl -sf --max-time 10 "$HEALTH_URL" > /dev/null; then
        log "ROLLBACK: SUCCESS — app is running on $prev_commit"
    else
        log "ROLLBACK: FAILED — app is down after rollback! Manual action required."
    fi
    exit 1
}

mkdir -p "$LOG_DIR"
log "================================================================"
log "Deploy started"

# ── 1. Record current state ──────────────────────────────────
PREV_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD)
log "Previous commit: $PREV_COMMIT"

# ── 2. Pull latest code ──────────────────────────────────────
log "Fetching latest code from origin/main..."
git -C "$APP_DIR" fetch origin main
git -C "$APP_DIR" reset --hard origin/main

NEW_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD)
log "New commit: $NEW_COMMIT"

if [ "$PREV_COMMIT" = "$NEW_COMMIT" ]; then
    log "No new commits — nothing to deploy."
    exit 0
fi

# ── 3. Install dependencies if requirements.txt changed ──────
if git -C "$APP_DIR" diff --name-only "$PREV_COMMIT" "$NEW_COMMIT" | grep -q "requirements.txt"; then
    log "requirements.txt changed — installing dependencies..."
    "$VENV/bin/pip" install -q --upgrade -r "$APP_DIR/requirements.txt"
else
    log "requirements.txt unchanged — skipping pip install."
fi

# ── 4. Snapshot database before migration ────────────────────
if [ -f "$DB_PATH" ]; then
    log "Snapshotting database before migration..."
    cp "$DB_PATH" "$DB_SNAPSHOT"
fi

# ── 5. Run database migrations ───────────────────────────────
log "Running database migrations..."
cd "$APP_DIR"
"$VENV/bin/python" migrate.py --non-interactive || {
    log "Migration FAILED — rolling back..."
    rollback "$PREV_COMMIT"
}

# ── 6. Restart service ───────────────────────────────────────
log "Restarting $SERVICE_NAME..."
sudo systemctl restart "$SERVICE_NAME"

# ── 7. Health check ──────────────────────────────────────────
log "Waiting for app to come up..."
sleep 3

for i in $(seq 1 $HEALTH_RETRIES); do
    if curl -sf --max-time 10 "$HEALTH_URL" > /dev/null; then
        log "Health check passed (attempt $i/$HEALTH_RETRIES)"
        rm -f "$DB_SNAPSHOT"   # clean up snapshot on success
        log "================================================================"
        log "Deploy SUCCEEDED: $NEW_COMMIT"
        exit 0
    fi
    log "Health check attempt $i/$HEALTH_RETRIES failed — waiting ${HEALTH_WAIT}s..."
    sleep "$HEALTH_WAIT"
done

# ── 8. Rollback ──────────────────────────────────────────────
log "Health check failed after $HEALTH_RETRIES attempts — rolling back..."
rollback "$PREV_COMMIT"
