"""Periodic cleanup for security log tables.

Runs in a background daemon thread started once per gunicorn worker.
Each delete commits on its own so a crash mid-run is harmless.

Retention:
  * login_history -> 90 days  (timestamp column)
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from database import db
from database.models import LoginHistory

logger = logging.getLogger(__name__)

LOGIN_HISTORY_TTL = timedelta(days=90)

_worker_thread = None
_worker_lock = threading.Lock()


def run_cleanup(now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    stats = {}

    stats["login_history"] = LoginHistory.query.filter(
        LoginHistory.timestamp < now - LOGIN_HISTORY_TTL,
    ).delete(synchronize_session=False)

    db.session.commit()
    if any(stats.values()):
        logger.info("data_retention: %s", stats)
    return stats


def start_worker(app, *, interval_seconds: int = 6 * 3600) -> None:
    """Start the periodic cleanup thread once per process."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return

        def _loop():
            while True:
                try:
                    with app.app_context():
                        run_cleanup()
                except Exception:
                    logger.exception("data_retention: cleanup failed")
                time.sleep(interval_seconds)

        _worker_thread = threading.Thread(
            target=_loop, name="data-retention-worker", daemon=True
        )
        _worker_thread.start()
