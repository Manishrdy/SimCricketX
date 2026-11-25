"""Periodic cleanup for the community board.

Each unit of work commits on its own, so a crash mid-run leaves nothing
half-deleted and the next run simply picks up where this one stopped.

Rules (all ages measured against the POST's last_activity_at as the
reference — child rows follow their parent, not their own timestamp):
  * unattached uploads             -> removed after 24 h
  * any post (live or soft-deleted) -> purged 30 days after last_activity_at
                                       pinned posts are exempt
  * soft-deleted comments          -> purged 30 days after deleted_at,
                                       only when the parent post still exists
  * images on purged posts         -> files removed before post deletion
  * notifications                  -> when their post is gone (CASCADE), or
                                       read ones after 30 days, all after 30
  * resolved reports               -> after 30 days
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from database import db
from database.models import (
    CommunityComment,
    CommunityImage,
    CommunityNotification,
    CommunityPost,
    CommunityReport,
)
from services.community_media import delete_image_files
from services.community_service import hard_delete_post

logger = logging.getLogger(__name__)

ORPHAN_UPLOAD_TTL = timedelta(hours=24)
# All community content is anchored to the post's last_activity_at.
POST_TTL = timedelta(days=30)
# Standalone child cleanup (belt-and-suspenders for rows whose post is gone).
NOTIFICATION_TTL = timedelta(days=30)
RESOLVED_REPORT_TTL = timedelta(days=30)
# Soft-deleted comments whose post still exists get a matching window.
DELETED_COMMENT_TTL = timedelta(days=30)
BATCH = 200

_worker_thread = None
_worker_lock = threading.Lock()


def _each(query, fn) -> int:
    done = 0
    for row_id in [r[0] for r in query.limit(BATCH).all()]:
        try:
            fn(row_id)
            db.session.commit()
            done += 1
        except Exception:
            db.session.rollback()
            logger.exception("community_retention: failed on row %s", row_id)
    return done


def _purge_post(post_id):
    post = db.session.get(CommunityPost, post_id)
    if post is not None:
        hard_delete_post(post)


def _purge_orphan(image_id):
    img = db.session.get(CommunityImage, image_id)
    if img is not None and img.post_id is None:
        if img.purged_at is None:
            delete_image_files(img)
        db.session.delete(img)


def _purge_comment(comment_id):
    c = db.session.get(CommunityComment, comment_id)
    if c is None:
        return
    # Only hard-delete when no live replies remain beneath it.
    live_replies = CommunityComment.query.filter(
        CommunityComment.parent_id == c.id,
        CommunityComment.deleted_at.is_(None),
    ).count()
    if not live_replies:
        db.session.delete(c)


def run_cleanup(now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    P = CommunityPost
    cutoff = now - POST_TTL

    # Posts (and all CASCADE children) older than POST_TTL by last_activity_at.
    # Pinned posts are excluded — admins pin things intentionally.
    stats = {
        "aged_posts": _each(
            db.session.query(P.id).filter(
                P.is_pinned.is_(False),
                P.last_activity_at < cutoff,
            ),
            _purge_post,
        ),
        "orphan_uploads": _each(
            db.session.query(CommunityImage.id).filter(
                CommunityImage.post_id.is_(None),
                CommunityImage.created_at < now - ORPHAN_UPLOAD_TTL,
            ),
            _purge_orphan,
        ),
        # Soft-deleted comments whose post is still live but past their window.
        "deleted_comments": _each(
            db.session.query(CommunityComment.id).filter(
                CommunityComment.deleted_at < now - DELETED_COMMENT_TTL,
            ),
            _purge_comment,
        ),
    }

    # Notifications — CASCADE removes them when the post is deleted above;
    # this catches any that outlive their post or hit the age cap first.
    stats["notifications"] = CommunityNotification.query.filter(
        CommunityNotification.created_at < now - NOTIFICATION_TTL,
    ).delete(synchronize_session=False)

    stats["reports"] = CommunityReport.query.filter(
        CommunityReport.resolved_at < now - RESOLVED_REPORT_TTL,
    ).delete(synchronize_session=False)

    db.session.commit()
    if any(stats.values()):
        logger.info("community_retention: %s", stats)
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
                    logger.exception("community_retention: cleanup failed")
                time.sleep(interval_seconds)

        _worker_thread = threading.Thread(
            target=_loop, name="community-retention-worker", daemon=True
        )
        _worker_thread.start()
