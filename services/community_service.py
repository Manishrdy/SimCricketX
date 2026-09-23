"""Community board domain logic: validation, permissions, posting, voting,
comments, moderation, notifications and serialisation.

Routes stay thin; every rule lives here so the user API, the admin API and
the retention worker agree on it.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

from database import db
from database.models import (
    CommunityComment,
    CommunityImage,
    CommunityNotification,
    CommunityPost,
    CommunityReport,
    CommunityVote,
    MasterPlayer,
    User,
)
from engine.format_catalog import FORMAT_LABELS
from services import community_search
from utils.display_name_rules import is_reserved_display_name
from utils.language_check import check_english

log = logging.getLogger("SimCricketX")

# ── Vocabulary ────────────────────────────────────────────────────────────────

FLAIRS = {
    "bug":          {"label": "Bug",             "vote_label": "Me too",        "icon": "fa-bug"},
    "question":     {"label": "Question",        "vote_label": "Same question", "icon": "fa-circle-question"},
    "feature":      {"label": "Feature request", "vote_label": "I want this",   "icon": "fa-lightbulb"},
    "feedback":     {"label": "Feedback",        "vote_label": "Agree",         "icon": "fa-comment-dots"},
    "announcement": {"label": "Announcement",    "vote_label": "Noted",         "icon": "fa-bullhorn", "admin_only": True},
}

STATUSES = {
    "open":         "Open",
    "answered":     "Answered",
    "under_review": "Under review",
    "planned":      "Planned",
    "in_progress":  "In progress",
    "fixed":        "Fixed",
    "wont_fix":     "Won't fix",
    "duplicate":    "Duplicate",
    "closed":       "Closed",
}
# Terminal statuses: retention clocks run from status_changed_at.
CLOSED_STATUSES = frozenset({"fixed", "wont_fix", "duplicate", "closed"})

BUG_FORMATS = dict(FORMAT_LABELS, none="Not match-related")

REPORT_REASONS = {
    "spam": "Spam or advertising",
    "abuse": "Abusive or offensive",
    "off_topic": "Off-topic",
    "personal_info": "Shares personal information",
    "other": "Other",
}

TITLE_MIN, TITLE_MAX = 10, 120
BODY_MIN, BODY_MAX = 20, 10_000
STEP_MIN, STEP_MAX, STEPS_MIN, STEPS_MAX = 3, 300, 2, 15
EXPECT_MIN, EXPECT_MAX = 5, 1_000
COMMENT_MAX = 2_000
MAX_IMAGES_PER_POST = 3
NEW_ACCOUNT_POSTING_DELAY = timedelta(hours=24)
DELETED_USER_NAME = "[deleted user]"

# A deliberately small list: the goal is to stop casual abuse, not to police
# tone. Matched on whole words after folding common digit swaps.
_PROFANITY = frozenset("""
    fuck fucking fucked fucker motherfucker shit shitty bullshit bitch bastard
    asshole dick cunt wanker twat slut whore retard retarded
    chutiya chutiye madarchod behenchod bhenchod bhosdike gandu randi harami
""".split())
_FOLD = str.maketrans("013457@$", "oieastas")


class CommunityError(Exception):
    """Validation / permission failure with a user-facing message."""

    def __init__(self, message: str, *, code: str = "invalid", status: int = 400, fields: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.fields = fields or {}


# ── Small helpers ─────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.utcnow()


def _naive(dt):
    """DB datetimes come back naive-UTC; some callers hold aware ones."""
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def new_public_id() -> str:
    return secrets.token_urlsafe(8)[:10]


def _clean_text(value, *, single_line=False) -> str:
    text = (value or "") if isinstance(value, str) else ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if single_line:
        return re.sub(r"\s+", " ", text).strip()
    # Trim trailing space per line and cap blank-line runs.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def contains_profanity(text: str) -> bool:
    words = re.findall(r"[a-z0-9@$]+", (text or "").lower())
    return any(w.translate(_FOLD) in _PROFANITY or w in _PROFANITY for w in words)


_player_words_cache: tuple[float, frozenset[str]] = (0.0, frozenset())


def player_pool_words() -> frozenset[str]:
    """Lowercase name parts from the global player pool, refreshed every 10
    minutes — so 'kohli got out' written in lower case still reads as English."""
    global _player_words_cache
    loaded_at, words = _player_words_cache
    if time.monotonic() - loaded_at < 600 and words:
        return words
    try:
        names = [r[0] for r in db.session.query(MasterPlayer.name).all()]
        words = frozenset(
            part for name in names for part in re.findall(r"[a-z]+", (name or "").lower()) if len(part) > 2)
    except Exception:
        words = frozenset()
    _player_words_cache = (time.monotonic(), words)
    return words


def author_name(user: User | None) -> str:
    if user is None:
        return DELETED_USER_NAME
    if user.display_name:
        return user.display_name
    return f"Player-{(user.stable_id or '000000')[:6]}"


def is_admin(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", True) and getattr(user, "is_admin", False))


# ── Permissions ───────────────────────────────────────────────────────────────

def posting_block_reason(user) -> tuple[str, str] | None:
    """(code, message) explaining why ``user`` cannot write, else None."""
    if is_admin(user):
        return None
    now = utcnow()
    if user.is_banned and (user.banned_until is None or _naive(user.banned_until) > now):
        return "banned", "Your account is suspended, so you can read the board but not post."
    muted_until = _naive(user.community_muted_until)
    if muted_until and muted_until > now:
        until = muted_until.strftime("%d %b %Y %H:%M UTC")
        return "muted", f"You are muted on the community board until {until}."
    if not user.email_verified:
        return "unverified", "Verify your email address before posting."
    if not user.display_name:
        return "no_display_name", "Set a display name before posting."
    if is_reserved_display_name(user.display_name):
        return "reserved_name", "Your display name looks like a staff name. Please change it in Account settings before posting."
    return None


def new_account_block(user) -> str | None:
    if is_admin(user) or not user.created_at:
        return None
    if utcnow() - _naive(user.created_at) < NEW_ACCOUNT_POSTING_DELAY:
        return "New accounts can read and vote for their first 24 hours before posting or commenting."
    return None


def require_can_write(user, *, voting=False):
    block = posting_block_reason(user)
    if block:
        raise CommunityError(block[1], code=block[0], status=403)
    if not voting:
        msg = new_account_block(user)
        if msg:
            raise CommunityError(msg, code="new_account", status=403)


def can_view_post(post: CommunityPost, user) -> bool:
    if post is None:
        return False
    if is_admin(user):
        return True
    if post.deleted_at is not None:
        return False
    return post.visibility == "public" or post.author_id == user.id


def can_edit_post(post: CommunityPost, user) -> bool:
    if is_admin(user):
        return True
    return (post.author_id == user.id and post.deleted_at is None and not post.is_locked
            and post.status not in CLOSED_STATUSES)


def can_comment(post: CommunityPost, user) -> bool:
    if is_admin(user):
        return post.deleted_at is None
    return (post.deleted_at is None and not post.is_locked and post.status not in CLOSED_STATUSES
            and posting_block_reason(user) is None and new_account_block(user) is None)


def get_visible_post(public_id: str, user) -> CommunityPost:
    post = CommunityPost.query.filter_by(public_id=public_id).first()
    if not can_view_post(post, user):
        raise CommunityError("Post not found.", code="not_found", status=404)
    return post


# ── Validation ────────────────────────────────────────────────────────────────

def _check_language(texts: list[str], user):
    if is_admin(user):
        return
    joined = "\n".join(t for t in texts if t)
    result = check_english(joined, extra_words=player_pool_words())
    if not result.ok:
        raise CommunityError(result.message(), code="not_english", fields={"_language": result.unrecognised[:8]})


def _check_profanity(texts: list[str], user):
    if is_admin(user):
        return
    if any(contains_profanity(t) for t in texts):
        raise CommunityError("Please remove the offensive language and try again.", code="profanity")


def validate_post_payload(data: dict, user, *, existing: CommunityPost | None = None) -> dict:
    """Return a clean dict of post fields or raise CommunityError with
    per-field messages."""
    errors: dict[str, str] = {}
    flair = (data.get("flair") or (existing.flair if existing else "")).strip().lower()
    if flair not in FLAIRS:
        errors["flair"] = "Choose a flair."
    elif FLAIRS[flair].get("admin_only") and not is_admin(user):
        errors["flair"] = "Only admins can post announcements."

    title = _clean_text(data.get("title"), single_line=True)
    if not TITLE_MIN <= len(title) <= TITLE_MAX:
        errors["title"] = f"Title must be {TITLE_MIN}-{TITLE_MAX} characters."

    body = _clean_text(data.get("body"))
    clean = {"flair": flair, "title": title, "body": body,
             "steps_json": None, "expected": None, "actual": None, "match_format": None}

    if flair == "bug":
        raw_steps = data.get("steps") or []
        if not isinstance(raw_steps, list):
            raw_steps = []
        steps = [_clean_text(s, single_line=True) for s in raw_steps if isinstance(s, str)]
        steps = [s for s in steps if s]
        if len(steps) < STEPS_MIN:
            errors["steps"] = f"Add at least {STEPS_MIN} steps to reproduce the bug."
        elif len(steps) > STEPS_MAX:
            errors["steps"] = f"Keep it to {STEPS_MAX} steps or fewer."
        elif any(not STEP_MIN <= len(s) <= STEP_MAX for s in steps):
            errors["steps"] = f"Each step must be {STEP_MIN}-{STEP_MAX} characters."
        expected = _clean_text(data.get("expected"))
        actual = _clean_text(data.get("actual"))
        if not EXPECT_MIN <= len(expected) <= EXPECT_MAX:
            errors["expected"] = f"Describe what you expected ({EXPECT_MIN}-{EXPECT_MAX} characters)."
        if not EXPECT_MIN <= len(actual) <= EXPECT_MAX:
            errors["actual"] = f"Describe what actually happened ({EXPECT_MIN}-{EXPECT_MAX} characters)."
        match_format = (data.get("match_format") or "").strip()
        if match_format not in BUG_FORMATS:
            errors["match_format"] = "Pick the format this happened in."
        if len(body) > BODY_MAX:
            errors["body"] = f"Details must be under {BODY_MAX} characters."
        clean.update(steps_json=json.dumps(steps), expected=expected, actual=actual, match_format=match_format)
    elif flair:
        if not BODY_MIN <= len(body) <= BODY_MAX:
            errors["body"] = f"Body must be {BODY_MIN}-{BODY_MAX} characters."

    visibility = (data.get("visibility") or (existing.visibility if existing else "public")).strip().lower()
    if visibility not in ("public", "private") or flair == "announcement":
        visibility = "public"
    clean["visibility"] = visibility

    if errors:
        raise CommunityError("Please fix the highlighted fields.", code="invalid", fields=errors)

    texts = [title, body]
    if flair == "bug":
        texts += json.loads(clean["steps_json"]) + [clean["expected"], clean["actual"]]
    _check_profanity(texts, user)
    _check_language(texts, user)
    return clean


def validate_comment_body(body, user) -> str:
    body = _clean_text(body)
    if not body:
        raise CommunityError("Comment cannot be empty.", fields={"body": "Write something first."})
    if len(body) > COMMENT_MAX:
        raise CommunityError(f"Comments are limited to {COMMENT_MAX} characters.", fields={"body": "Too long."})
    _check_profanity([body], user)
    _check_language([body], user)
    return body


# ── Posts ─────────────────────────────────────────────────────────────────────

def _attach_images(post: CommunityPost, user, image_ids) -> None:
    ids = [i for i in (image_ids or []) if isinstance(i, str)][:MAX_IMAGES_PER_POST + 1]
    current = [img for img in post.images if img.purged_at is None] if post.id else []
    keep = [img for img in current if img.public_id in ids]
    for img in current:
        if img not in keep:
            img.post_id = None  # orphaned again; retention removes the file
    wanted = [i for i in ids if i not in {img.public_id for img in keep}]
    if wanted:
        q = CommunityImage.query.filter(CommunityImage.public_id.in_(wanted), CommunityImage.post_id.is_(None),
                                        CommunityImage.purged_at.is_(None))
        if not is_admin(user):
            q = q.filter(CommunityImage.uploader_id == user.id)
        keep += q.all()
    if len(keep) > MAX_IMAGES_PER_POST:
        raise CommunityError(f"A post can have at most {MAX_IMAGES_PER_POST} images.", fields={"images": "Too many images."})
    for img in keep:
        img.post = post


def create_post(user, data: dict, *, request_meta: dict | None = None) -> CommunityPost:
    require_can_write(user)
    clean = validate_post_payload(data, user)
    now = utcnow()
    meta = request_meta or {}
    post = CommunityPost(
        public_id=new_public_id(), author_id=user.id, created_at=now, last_activity_at=now,
        status="open", status_changed_at=now, needs_admin=not is_admin(user),
        is_pinned=clean["flair"] == "announcement",
        page_url=(meta.get("page_url") or "")[:500] or None,
        app_version=(meta.get("app_version") or "")[:50] or None,
        user_agent=(meta.get("user_agent") or "")[:300] or None,
        **clean,
    )
    db.session.add(post)
    db.session.flush()
    _attach_images(post, user, data.get("image_ids"))
    db.session.commit()
    return post


def update_post(post: CommunityPost, user, data: dict) -> CommunityPost:
    if not can_edit_post(post, user):
        raise CommunityError("You can't edit this post.", code="forbidden", status=403)
    if post.author_id == user.id:
        require_can_write(user)
    clean = validate_post_payload(data, user, existing=post)
    if not is_admin(user) and clean["flair"] != post.flair and post.flair == "announcement":
        raise CommunityError("You can't change this flair.", code="forbidden", status=403)
    for key, value in clean.items():
        setattr(post, key, value)
    if "image_ids" in data:  # absent = leave attachments alone
        _attach_images(post, user, data.get("image_ids"))
    post.edited_at = utcnow()
    post.edited_by = user.id
    db.session.commit()
    return post


def soft_delete_post(post: CommunityPost, actor, reason: str = "") -> None:
    if not (is_admin(actor) or post.author_id == actor.id):
        raise CommunityError("You can't delete this post.", code="forbidden", status=403)
    if post.deleted_at is None:
        post.deleted_at = utcnow()
        post.deleted_by = actor.id
        post.delete_reason = (reason or ("Removed by author" if post.author_id == actor.id else "Removed by admin"))[:200]
        db.session.commit()


def restore_post(post: CommunityPost) -> None:
    post.deleted_at = post.deleted_by = post.delete_reason = None
    db.session.commit()


# ── Votes ─────────────────────────────────────────────────────────────────────

def _recount_votes(post: CommunityPost) -> int:
    post.vote_count = db.session.query(func.count(CommunityVote.id)).filter_by(post_id=post.id).scalar() or 0
    return post.vote_count


def toggle_vote(post: CommunityPost, user) -> tuple[bool, int]:
    require_can_write(user, voting=True)
    if post.author_id == user.id:
        raise CommunityError("You can't vote on your own post.", code="own_post")
    if post.visibility != "public":
        raise CommunityError("Private posts can't be voted on.", code="private")
    if post.deleted_at is not None or post.status == "duplicate":
        raise CommunityError("Voting is closed on this post.", code="closed")
    existing = CommunityVote.query.filter_by(post_id=post.id, user_id=user.id).first()
    if existing:
        db.session.delete(existing)
        voted = False
    else:
        db.session.add(CommunityVote(post_id=post.id, user_id=user.id))
        voted = True
    try:
        db.session.flush()
    except IntegrityError:  # double-click race: the other request won
        db.session.rollback()
        voted = True
    count = _recount_votes(post)
    db.session.commit()
    return voted, count


def has_voted(post: CommunityPost, user) -> bool:
    return CommunityVote.query.filter_by(post_id=post.id, user_id=user.id).first() is not None


# ── Comments ──────────────────────────────────────────────────────────────────

def _notify(user_id, kind, post, *, comment=None, actor=None, detail=None):
    if not user_id or (actor is not None and user_id == actor.id):
        return None
    note = CommunityNotification(
        user_id=user_id, kind=kind, post_id=post.id, comment_id=comment.id if comment else None,
        actor_name=author_name(actor) if actor is not None else None, detail=(detail or "")[:120] or None)
    db.session.add(note)
    return note


def _recount_comments(post: CommunityPost) -> None:
    post.comment_count = (db.session.query(func.count(CommunityComment.id))
                          .filter(CommunityComment.post_id == post.id, CommunityComment.deleted_at.is_(None))
                          .scalar() or 0)


def add_comment(post: CommunityPost, user, body, parent_id=None) -> CommunityComment:
    require_can_write(user)
    if not can_comment(post, user):
        raise CommunityError("Comments are closed on this post.", code="locked", status=403)
    body = validate_comment_body(body, user)

    parent = None
    if parent_id:
        parent = db.session.get(CommunityComment, int(parent_id))
        if parent is None or parent.post_id != post.id or parent.deleted_at is not None:
            raise CommunityError("The comment you replied to is gone.", code="not_found", status=404)
        if parent.parent_id:
            # Instagram-style: replies to replies join the top-level thread
            # with an @mention of who they answered.
            mention = f"@{author_name(parent.author)}"
            if not body.startswith(mention):
                body = f"{mention} {body}"
            parent = db.session.get(CommunityComment, parent.parent_id)

    now = utcnow()
    comment = CommunityComment(post_id=post.id, author_id=user.id, body=body,
                               parent_id=parent.id if parent else None, created_at=now)
    db.session.add(comment)
    db.session.flush()

    post.last_activity_at = now
    if is_admin(user):
        post.needs_admin = False
    elif user.id == post.author_id:
        post.needs_admin = True
    _recount_comments(post)

    if is_admin(user):
        _notify(post.author_id, "admin_response", post, comment=comment, actor=user)
    else:
        _notify(post.author_id, "comment", post, comment=comment, actor=user)
    if parent is not None and parent.author_id != post.author_id:
        _notify(parent.author_id, "reply", post, comment=comment, actor=user)
    db.session.commit()

    if is_admin(user) and post.author_id and post.author_id != user.id:
        send_notification_email(post, "admin_response")
    return comment


def edit_comment(comment: CommunityComment, user, body) -> CommunityComment:
    if not (is_admin(user) or comment.author_id == user.id):
        raise CommunityError("You can't edit this comment.", code="forbidden", status=403)
    if comment.deleted_at is not None:
        raise CommunityError("This comment was deleted.", code="gone", status=404)
    if not is_admin(user):
        require_can_write(user)
        if not can_comment(comment.post, user):
            raise CommunityError("Comments are closed on this post.", code="locked", status=403)
    comment.body = validate_comment_body(body, user)
    comment.edited_at = utcnow()
    comment.edited_by = user.id
    db.session.commit()
    return comment


def soft_delete_comment(comment: CommunityComment, actor, reason: str = "") -> None:
    if not (is_admin(actor) or comment.author_id == actor.id):
        raise CommunityError("You can't delete this comment.", code="forbidden", status=403)
    if comment.deleted_at is None:
        comment.deleted_at = utcnow()
        comment.deleted_by = actor.id
        comment.delete_reason = (reason or "")[:200] or None
        comment.is_official = False
        _recount_comments(comment.post)
        db.session.commit()


def restore_comment(comment: CommunityComment) -> None:
    comment.deleted_at = comment.deleted_by = comment.delete_reason = None
    _recount_comments(comment.post)
    db.session.commit()


def visible_comments(post: CommunityPost, viewer) -> list[CommunityComment]:
    """Top-level comments in order, each followed by its replies. Deleted
    comments stay as tombstones only when replies hang off them."""
    rows = (CommunityComment.query.filter_by(post_id=post.id)
            .order_by(CommunityComment.created_at.asc(), CommunityComment.id.asc()).all())
    admin = is_admin(viewer)
    replies: dict[int, list] = {}
    for c in rows:
        if c.parent_id:
            replies.setdefault(c.parent_id, []).append(c)
    tops = [c for c in rows if not c.parent_id]
    # Official answers float to the top.
    tops.sort(key=lambda c: (not c.is_official or c.deleted_at is not None))
    out = []
    for top in tops:
        kids = [r for r in replies.get(top.id, []) if admin or r.deleted_at is None]
        if top.deleted_at is not None and not admin and not kids:
            continue
        out.append((top, kids))
    return out


# ── Moderation (admin) ────────────────────────────────────────────────────────

def set_status(post: CommunityPost, admin, status: str) -> None:
    if status not in STATUSES:
        raise CommunityError("Unknown status.")
    if status == post.status:
        return
    post.status = status
    post.status_changed_at = utcnow()
    post.last_activity_at = post.status_changed_at
    post.needs_admin = False  # a status change is an admin triaging the thread
    label = STATUSES[status]
    _notify(post.author_id, "status_change", post, actor=admin, detail=label)
    if status == "fixed":
        voter_ids = [v.user_id for v in CommunityVote.query.filter_by(post_id=post.id).all()]
        for uid in voter_ids:
            if uid != post.author_id:
                _notify(uid, "status_change", post, actor=admin, detail=label)
    db.session.commit()
    if post.author_id and post.author_id != admin.id:
        send_notification_email(post, "status_change", detail=label)


def set_flag(post: CommunityPost, field: str, value: bool) -> None:
    if field not in ("is_locked", "is_pinned"):
        raise CommunityError("Unknown flag.")
    setattr(post, field, bool(value))
    db.session.commit()


def set_flair(post: CommunityPost, flair: str) -> None:
    if flair not in FLAIRS:
        raise CommunityError("Unknown flair.")
    # Bug fields are kept (not shown for other flairs) so flipping back is lossless.
    post.flair = flair
    db.session.commit()


def mark_duplicate(post: CommunityPost, admin, target_public_id: str) -> CommunityPost:
    target = CommunityPost.query.filter_by(public_id=(target_public_id or "").strip()).first()
    if target is None or target.id == post.id or target.deleted_at is not None:
        raise CommunityError("Pick an existing post to merge into.")
    if target.duplicate_of_id == post.id:
        raise CommunityError("That post is already a duplicate of this one.")
    # Move votes (and the duplicate's author, who clearly has the issue too).
    carriers = {v.user_id for v in CommunityVote.query.filter_by(post_id=post.id).all()}
    if post.author_id:
        carriers.add(post.author_id)
    already = {v.user_id for v in CommunityVote.query.filter_by(post_id=target.id).all()}
    for uid in carriers - already - {target.author_id}:
        db.session.add(CommunityVote(post_id=target.id, user_id=uid))
    CommunityVote.query.filter_by(post_id=post.id).delete()
    post.duplicate_of_id = target.id
    post.vote_count = 0
    db.session.flush()
    _recount_votes(target)
    set_status(post, admin, "duplicate")  # commits
    return target


def set_official(comment: CommunityComment, value: bool) -> None:
    if comment.deleted_at is not None or comment.parent_id:
        raise CommunityError("Only a live top-level comment can be the official answer.")
    if value:
        CommunityComment.query.filter(CommunityComment.post_id == comment.post_id,
                                      CommunityComment.id != comment.id).update({"is_official": False})
    comment.is_official = bool(value)
    db.session.commit()


def hard_delete_post(post: CommunityPost) -> None:
    from services.community_media import delete_image_files
    for img in list(post.images):
        delete_image_files(img)
        db.session.delete(img)
    db.session.delete(post)
    db.session.commit()


def mute_user(user: User, hours: int | None) -> None:
    user.community_muted_until = utcnow() + timedelta(hours=int(hours)) if hours else None
    db.session.commit()


# ── Reports ───────────────────────────────────────────────────────────────────

def create_report(user, post: CommunityPost, comment: CommunityComment | None, reason: str, details: str = ""):
    if reason not in REPORT_REASONS:
        raise CommunityError("Pick a reason.")
    dupe = CommunityReport.query.filter_by(reporter_id=user.id, post_id=post.id,
                                           comment_id=comment.id if comment else None,
                                           resolved_at=None).first()
    if dupe:
        return dupe
    report = CommunityReport(reporter_id=user.id, post_id=post.id, comment_id=comment.id if comment else None,
                             reason=reason, details=_clean_text(details)[:500] or None)
    db.session.add(report)
    db.session.commit()
    return report


def resolve_report(report: CommunityReport, admin, resolution: str) -> None:
    report.resolved_at = utcnow()
    report.resolved_by = admin.id
    report.resolution = (resolution or "dismissed")[:30]
    db.session.commit()


# ── Listing ───────────────────────────────────────────────────────────────────

SORTS = {"active": CommunityPost.last_activity_at, "new": CommunityPost.created_at, "top": CommunityPost.vote_count}


def _visible_query(viewer, *, include_deleted=False):
    # Cards show author + image count; load both in two queries, not 2 per row.
    q = CommunityPost.query.options(selectinload(CommunityPost.author), selectinload(CommunityPost.images))
    if not (is_admin(viewer) and include_deleted):
        q = q.filter(CommunityPost.deleted_at.is_(None))
    if not is_admin(viewer):
        q = q.filter(or_(CommunityPost.visibility == "public", CommunityPost.author_id == viewer.id))
    return q


def _encode_cursor(post, sort) -> str:
    key = getattr(post, SORTS[sort].key)
    if isinstance(key, datetime):
        key = key.isoformat()
    return f"{key}|{post.id}"


def list_posts(viewer, *, flair=None, status=None, sort="active", mine=False, visibility=None,
               q=None, cursor=None, limit=20):
    """Return (pinned, posts, next_cursor). Search results are ranked and
    unpaginated (capped at 50)."""
    sort = sort if sort in SORTS else "active"
    base = _visible_query(viewer)
    if flair in FLAIRS:
        base = base.filter(CommunityPost.flair == flair)
    if status in STATUSES:
        base = base.filter(CommunityPost.status == status)
    if mine:
        base = base.filter(CommunityPost.author_id == viewer.id)
    if visibility in ("public", "private"):
        base = base.filter(CommunityPost.visibility == visibility)

    if q and q.strip():
        ids = community_search.search_post_ids(db.session, q, limit=50)
        if not ids:
            return [], [], None
        rows = {p.id: p for p in base.filter(CommunityPost.id.in_(ids)).all()}
        return [], [rows[i] for i in ids if i in rows], None

    pinned = []
    filtered = bool(flair or status or mine or visibility)
    if not cursor and not filtered:
        pinned = base.filter(CommunityPost.is_pinned.is_(True)).order_by(CommunityPost.created_at.desc()).all()
    if not filtered:
        base = base.filter(CommunityPost.is_pinned.is_(False))

    col = SORTS[sort]
    if cursor:
        try:
            raw_key, raw_id = cursor.rsplit("|", 1)
            cid = int(raw_id)
            key = int(raw_key) if sort == "top" else datetime.fromisoformat(raw_key)
            base = base.filter(or_(col < key, (col == key) & (CommunityPost.id < cid)))
        except (ValueError, TypeError):
            pass
    limit = max(1, min(int(limit or 20), 50))
    rows = base.order_by(col.desc(), CommunityPost.id.desc()).limit(limit + 1).all()
    next_cursor = _encode_cursor(rows[limit - 1], sort) if len(rows) > limit else None
    return pinned, rows[:limit], next_cursor


def similar_posts(viewer, title: str, limit=5, exclude_id=None):
    ids = community_search.search_post_ids(db.session, title, limit=25)
    if not ids:
        return []
    q = (CommunityPost.query.filter(CommunityPost.id.in_(ids), CommunityPost.deleted_at.is_(None),
                                    CommunityPost.visibility == "public"))
    rows = {p.id: p for p in q.all()}
    out = [rows[i] for i in ids if i in rows and i != exclude_id]
    return out[:limit]


# ── Notifications ─────────────────────────────────────────────────────────────

def unread_count(user) -> int:
    return CommunityNotification.query.filter_by(user_id=user.id, read_at=None).count()


def recent_notifications(user, limit=30):
    return (CommunityNotification.query.filter_by(user_id=user.id)
            .order_by(CommunityNotification.created_at.desc()).limit(limit).all())


def mark_notifications_read(user, ids=None) -> None:
    q = CommunityNotification.query.filter_by(user_id=user.id, read_at=None)
    if ids:
        q = q.filter(CommunityNotification.id.in_([int(i) for i in ids if str(i).isdigit()]))
    q.update({"read_at": utcnow()}, synchronize_session=False)
    db.session.commit()


def send_notification_email(post: CommunityPost, kind: str, detail: str = "") -> None:
    """Email the post author about an admin response or status change, at most
    once per post per hour. Never raises."""
    try:
        from flask import url_for
        from utils.email_service import send_email

        author = post.author
        if author is None or not author.email_verified:
            return
        # Only emailable kinds count: a user's comment landing in the same hour
        # must not swallow the admin's reply email.
        since = utcnow() - timedelta(hours=1)
        recent = (CommunityNotification.query
                  .filter(CommunityNotification.user_id == author.id, CommunityNotification.post_id == post.id,
                          CommunityNotification.kind.in_(("admin_response", "status_change")),
                          CommunityNotification.created_at >= since)
                  .count())
        if recent > 1:  # the notification just written is one of them
            return
        link = url_for("community_post", public_id=post.public_id, _external=True)
        from markupsafe import escape
        title = escape(post.title)
        if kind == "status_change":
            subject = f"Your post is now {detail}"
            line = f"An admin marked your post <strong>{title}</strong> as <strong>{escape(detail)}</strong>."
        else:
            subject = "An admin replied to your post"
            line = f"An admin replied to your post <strong>{title}</strong>."
        html = (f"<p>{line}</p><p><a href=\"{link}\">View the thread</a></p>"
                "<p style=\"color:#888;font-size:12px\">SimCricketX community board</p>")
        send_email(author.id, subject, html)
    except Exception:
        log.exception("[Community] notification email failed")


# ── Serialisation ─────────────────────────────────────────────────────────────

def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def serialize_author(user: User | None, viewer) -> dict:
    data = {"name": author_name(user), "is_admin": bool(user and user.is_admin), "deleted": user is None}
    if is_admin(viewer) and user is not None:
        data["email"] = user.id
        data["muted_until"] = _iso(user.community_muted_until)
    return data


def image_urls(img: CommunityImage) -> dict:
    from flask import url_for
    return {
        "id": img.public_id,
        "url": url_for("community_media", public_id=img.public_id, variant="full"),
        "thumb_url": url_for("community_media", public_id=img.public_id, variant="thumb"),
        "width": img.width, "height": img.height, "expired": img.purged_at is not None,
    }


def serialize_post(post: CommunityPost, viewer, *, detail=False) -> dict:
    flair = FLAIRS.get(post.flair, FLAIRS["question"])
    admin = is_admin(viewer)
    data = {
        "id": post.public_id,
        "flair": post.flair, "flair_label": flair["label"], "flair_icon": flair["icon"],
        "vote_label": flair["vote_label"],
        "title": post.title,
        "status": post.status, "status_label": STATUSES.get(post.status, post.status),
        "visibility": post.visibility,
        "is_locked": post.is_locked, "is_pinned": post.is_pinned,
        "vote_count": post.vote_count, "comment_count": post.comment_count,
        "created_at": _iso(post.created_at), "last_activity_at": _iso(post.last_activity_at),
        "edited_at": _iso(post.edited_at),
        "deleted": post.deleted_at is not None,
        "author": serialize_author(post.author, viewer),
        "is_mine": post.author_id == viewer.id,
        "image_count": sum(1 for i in post.images if i.purged_at is None),
    }
    if detail:
        data.update({
            "body": post.body,
            "steps": json.loads(post.steps_json) if post.steps_json else [],
            "expected": post.expected, "actual": post.actual,
            "match_format": post.match_format,
            "match_format_label": BUG_FORMATS.get(post.match_format or "", None),
            "images": [image_urls(i) for i in post.images if i.purged_at is None or admin],
            "voted": has_voted(post, viewer),
            "duplicate_of": None,
            "can": {
                "edit": can_edit_post(post, viewer),
                "delete": admin or post.author_id == viewer.id,
                "comment": can_comment(post, viewer),
                "vote": (post.author_id != viewer.id and post.deleted_at is None and post.status != "duplicate"
                         and post.visibility == "public"),
                "moderate": admin,
            },
        })
        if post.duplicate_of_id:
            dup = db.session.get(CommunityPost, post.duplicate_of_id)
            if dup is not None and can_view_post(dup, viewer):
                data["duplicate_of"] = {"id": dup.public_id, "title": dup.title}
        if admin:
            data.update({"page_url": post.page_url, "app_version": post.app_version, "user_agent": post.user_agent,
                         "delete_reason": post.delete_reason, "needs_admin": post.needs_admin})
    return data


def serialize_comment(comment: CommunityComment, viewer) -> dict:
    admin = is_admin(viewer)
    deleted = comment.deleted_at is not None
    return {
        "id": comment.id,
        "parent_id": comment.parent_id,
        "body": comment.body if (not deleted or admin) else "",
        "deleted": deleted,
        "delete_reason": comment.delete_reason if admin else None,
        "is_official": comment.is_official,
        "author": serialize_author(comment.author, viewer) if (not deleted or admin) else
        {"name": "[deleted]", "is_admin": False, "deleted": True},
        "created_at": _iso(comment.created_at),
        "edited_at": _iso(comment.edited_at),
        "can": {
            "edit": not deleted and (admin or comment.author_id == viewer.id),
            "delete": not deleted and (admin or comment.author_id == viewer.id),
            "moderate": admin,
        },
    }


def serialize_notification(n: CommunityNotification) -> dict:
    post = n.post
    text = {
        "comment": f"{n.actor_name or 'Someone'} commented on your post",
        "reply": f"{n.actor_name or 'Someone'} replied to your comment",
        "admin_response": "An admin replied to your post",
        "status_change": f"Status changed to {n.detail}" if n.detail else "Status changed",
    }.get(n.kind, "Update on a post")
    return {
        "id": n.id, "kind": n.kind, "text": text,
        "post_id": post.public_id if post else None,
        "post_title": post.title if post else "",
        "comment_id": n.comment_id,
        "created_at": _iso(n.created_at), "read": n.read_at is not None,
    }


# ── Admin queues ──────────────────────────────────────────────────────────────

def admin_pending_count() -> int:
    private_waiting = (CommunityPost.query.filter(CommunityPost.deleted_at.is_(None), CommunityPost.needs_admin.is_(True),
                                                  CommunityPost.visibility == "private",
                                                  CommunityPost.status.notin_(CLOSED_STATUSES)).count())
    reports = CommunityReport.query.filter(CommunityReport.resolved_at.is_(None)).count()
    return private_waiting + reports


def admin_queues(limit=50) -> dict:
    live = CommunityPost.query.filter(CommunityPost.deleted_at.is_(None), CommunityPost.status.notin_(CLOSED_STATUSES))
    return {
        "private": live.filter(CommunityPost.visibility == "private", CommunityPost.needs_admin.is_(True))
                       .order_by(CommunityPost.last_activity_at.asc()).limit(limit).all(),
        "unanswered": live.filter(CommunityPost.visibility == "public", CommunityPost.needs_admin.is_(True))
                          .order_by(CommunityPost.vote_count.desc(), CommunityPost.created_at.asc()).limit(limit).all(),
        "reports": CommunityReport.query.filter(CommunityReport.resolved_at.is_(None))
                                        .order_by(CommunityReport.created_at.asc()).limit(limit).all(),
        "deleted": CommunityPost.query.filter(CommunityPost.deleted_at.isnot(None))
                                      .order_by(CommunityPost.deleted_at.desc()).limit(limit).all(),
        "muted": User.query.filter(User.community_muted_until > utcnow()).order_by(User.community_muted_until.desc()).all(),
    }


def admin_stats() -> dict:
    total = CommunityPost.query.filter(CommunityPost.deleted_at.is_(None)).count()
    open_bugs = CommunityPost.query.filter(CommunityPost.deleted_at.is_(None), CommunityPost.flair == "bug",
                                           CommunityPost.status.notin_(CLOSED_STATUSES)).count()
    since = utcnow() - timedelta(days=7)
    week = CommunityPost.query.filter(CommunityPost.created_at >= since).count()
    return {"total": total, "open_bugs": open_bugs, "this_week": week}
