"""Periodic cleanup for the community board.

Each unit of work commits on its own, so a crash mid-run leaves nothing
half-deleted and the next run simply picks up where this one stopped.

Rules:
  * unattached uploads             -> removed after 24 h
  * soft-deleted posts / comments  -> purged 30 days after deletion
  * images on closed posts         -> files removed 180 days after closing
                                      (row kept, UI shows "image expired")
  * private posts                  -> purged 90 days after closing, or at
                                      once when the author's account is gone
  * notifications                  -> read ones after 90 days, all after 180
  * resolved reports               -> after 180 days
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
from services.community_service import CLOSED_STATUSES, hard_delete_post

logger = logging.getLogger(__name__)

ORPHAN_UPLOAD_TTL = timedelta(hours=24)
SOFT_DELETE_TTL = timedelta(days=30)
CLOSED_IMAGE_TTL = timedelta(days=180)
CLOSED_PRIVATE_TTL = timedelta(days=90)
READ_NOTIFICATION_TTL = timedelta(days=90)
ANY_NOTIFICATION_TTL = timedelta(days=180)
RESOLVED_REPORT_TTL = timedelta(days=180)
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


def _expire_images(post_id):
    for img in CommunityImage.query.filter_by(post_id=post_id, purged_at=None).all():
        delete_image_files(img)


def _purge_comment(comment_id):
    c = db.session.get(CommunityComment, comment_id)
    if c is None:
        return
    live_replies = CommunityComment.query.filter(CommunityComment.parent_id == c.id,
                                                 CommunityComment.deleted_at.is_(None)).count()
    if not live_replies:
        db.session.delete(c)


def run_cleanup(now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    P = CommunityPost
    stats = {
        "orphan_uploads": _each(
            db.session.query(CommunityImage.id).filter(CommunityImage.post_id.is_(None),
                                                       CommunityImage.created_at < now - ORPHAN_UPLOAD_TTL),
            _purge_orphan),
        "deleted_posts": _each(
            db.session.query(P.id).filter(P.deleted_at < now - SOFT_DELETE_TTL), _purge_post),
        "private_posts": _each(
            db.session.query(P.id).filter(P.visibility == "private", P.deleted_at.is_(None),
                                          (P.author_id.is_(None)) |
                                          (P.status.in_(CLOSED_STATUSES) & (P.status_changed_at < now - CLOSED_PRIVATE_TTL))),
            _purge_post),
        "deleted_comments": _each(
            db.session.query(CommunityComment.id).filter(CommunityComment.deleted_at < now - SOFT_DELETE_TTL),
            _purge_comment),
        "expired_images": _each(
            db.session.query(P.id).join(CommunityImage, CommunityImage.post_id == P.id)
            .filter(CommunityImage.purged_at.is_(None), P.status.in_(CLOSED_STATUSES),
                    P.status_changed_at < now - CLOSED_IMAGE_TTL).distinct(),
            _expire_images),
    }
    stats["notifications"] = CommunityNotification.query.filter(
        ((CommunityNotification.read_at.isnot(None)) & (CommunityNotification.created_at < now - READ_NOTIFICATION_TTL))
        | (CommunityNotification.created_at < now - ANY_NOTIFICATION_TTL)).delete(synchronize_session=False)
    stats["reports"] = CommunityReport.query.filter(
        CommunityReport.resolved_at < now - RESOLVED_REPORT_TTL).delete(synchronize_session=False)
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

        _worker_thread = threading.Thread(target=_loop, name="community-retention-worker", daemon=True)
        _worker_thread.start()
