"""Community board: language check, image pipeline, permissions, API,
moderation and retention."""

import io
import json
from datetime import datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from database import db
from database.models import (
    ActiveSession,
    AdminAuditLog,
    CommunityComment,
    CommunityImage,
    CommunityNotification,
    CommunityPost,
    CommunityReport,
    CommunityVote,
    User,
)
from services import community_media, community_retention, community_search
from services import community_service as cs
from services.community_media import ImageRejected, process_image
from utils.display_name_rules import is_reserved_display_name
from utils.language_check import check_english


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_user(email, *, name="Player One", admin=False, age_days=30, verified=True, **extra):
    user = User(id=email, password_hash=generate_password_hash("Password123!"), display_name=name,
                is_admin=admin, is_banned=False, force_password_reset=False, email_verified=verified,
                created_at=datetime.utcnow() - timedelta(days=age_days), **extra)
    db.session.add(user)
    db.session.commit()
    return user


def login(client, user):
    token = f"tok-{user.id}"
    ActiveSession.query.filter_by(session_token=token).delete()
    db.session.add(ActiveSession(session_token=token, user_id=user.id, ip_address="127.0.0.1", user_agent="pytest",
                                 login_at=datetime.utcnow(), last_active=datetime.utcnow()))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = user.id
        sess["_fresh"] = True
        sess["session_token"] = token
    return client


class _FreshUserClient:
    """The app fixture holds one app context open for the whole test, and
    Flask reuses it for every request, so flask-login's cached ``g._login_user``
    would leak from one client to the next. Drop it before each request."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name in ("get", "post", "patch", "delete", "put", "open"):
            def call(*args, **kwargs):
                from flask import g
                g.pop("_login_user", None)
                return attr(*args, **kwargs)
            return call
        return attr


def as_user(app, user):
    return _FreshUserClient(login(app.test_client(), user))


QUESTION = {"flair": "question", "title": "How do I enable rain in a T20 match?",
            "body": "I want to simulate a rain-affected game with DLS but I cannot find the setting."}
BUG = {"flair": "bug", "title": "Scorecard shows the wrong bowler after rain",
       "steps": ["Start a T20 match with rain enabled", "Simulate to the 12th over", "Open the scorecard"],
       "expected": "The bowler of the last over is listed", "actual": "A bowler who did not bowl is listed",
       "match_format": "T20", "body": ""}


def jpeg_bytes(w=800, h=600, exif=None):
    im = Image.new("RGB", (w, h), (30, 120, 200))
    buf = io.BytesIO()
    kw = {"exif": exif.tobytes()} if exif is not None else {}
    im.save(buf, "JPEG", quality=90, **kw)
    return buf.getvalue()


@pytest.fixture
def media_dir(app, tmp_path):
    app.config["COMMUNITY_MEDIA_DIR"] = str(tmp_path / "media")
    app.config["COMMUNITY_MIN_FREE_BYTES"] = 0
    return tmp_path / "media"


@pytest.fixture
def fts(app):
    with db.engine.begin() as conn:
        community_search.ensure_fts(conn)
    yield
    with db.engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS community_posts_fts"))


@pytest.fixture
def alice(app):
    return make_user("alice@example.com", name="Alice")


@pytest.fixture
def bob(app):
    return make_user("bob@example.com", name="Bob")


@pytest.fixture
def boss(app):
    return make_user("boss@example.com", name="Boss", admin=True)


def create(app, user, payload=None, **overrides):
    client = as_user(app, user)
    resp = client.post("/api/community/posts", json={**(payload or QUESTION), **overrides})
    return resp


# ── Language check ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "The match crashed after the 15th over when I clicked next ball.",
    "Kohli got out LBW but the scorecard showed caught by Starc at Wankhede.",
    "pls fix the powerplay bug, dont know why it happens",
    "Error: `KeyError: 'batting_rating'` shows on /match/setup when I pick FC",
    "App crashes",
])
def test_english_accepted(text):
    assert check_english(text).ok


@pytest.mark.parametrize("text,reason", [
    ("bhai match nahi chal raha hai, kya karu", "vocabulary"),
    ("Bhai Match Nahi Chal Raha Hai Kya Karu", "vocabulary"),
    ("BHAI MATCH NAHI CHAL RAHA", "vocabulary"),
    ("मैच नहीं चल रहा है भाई", "script"),
    ("మ్యాచ్ పని చేయడం లేదు", "script"),
    ("La app no funciona cuando juego el partido", "vocabulary"),
])
def test_non_english_rejected(text, reason):
    result = check_english(text)
    assert not result.ok
    assert result.reason == reason
    assert result.message()


def test_lowercase_player_names_accepted_via_extra_words():
    assert not check_english("kohli bumrah jadeja pant").ok
    assert check_english("kohli bumrah jadeja pant", extra_words={"kohli", "bumrah", "jadeja", "pant"}).ok


@pytest.mark.parametrize("name,reserved", [
    ("Admin", True), ("S1mCricketX Official", True), ("@dm1n", True), ("Moderator Joe", True),
    ("CSKSupporter", False), ("Virat Fan", False),
])
def test_reserved_display_names(name, reserved):
    assert is_reserved_display_name(name) is reserved


# ── Image pipeline ───────────────────────────────────────────────────────────

def test_image_exif_stripped_and_orientation_applied():
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"   # Make
    exif[0x0112] = 6              # Orientation: rotate 90
    out = process_image(jpeg_bytes(800, 600, exif))
    img = Image.open(io.BytesIO(out["full"]))
    assert img.format == "WEBP"
    assert dict(img.getexif()) == {}
    assert (out["width"], out["height"]) == (600, 800)


def test_image_resized_and_under_target():
    out = process_image(jpeg_bytes(4000, 3000))
    assert max(out["width"], out["height"]) <= community_media.MAX_EDGE
    assert len(out["full"]) <= community_media.TARGET_BYTES
    assert len(out["thumb"]) <= community_media.THUMB_TARGET_BYTES


@pytest.mark.parametrize("data,needle", [
    (b"<html><script>alert(1)</script></html>", "supported image"),
    (b"", "Empty"),
])
def test_image_rejects_non_images(data, needle):
    with pytest.raises(ImageRejected) as exc:
        process_image(data)
    assert needle in exc.value.message


def test_image_rejects_gif_and_bombs():
    buf = io.BytesIO()
    Image.new("RGB", (40, 40)).save(buf, "GIF")
    with pytest.raises(ImageRejected):
        process_image(buf.getvalue())
    buf = io.BytesIO()
    Image.new("L", (10000, 10000)).save(buf, "PNG")
    with pytest.raises(ImageRejected) as exc:
        process_image(buf.getvalue())
    assert "too large" in exc.value.message


def test_image_alpha_only_kept_when_used():
    buf = io.BytesIO()
    im = Image.new("RGBA", (200, 200), (255, 0, 0, 0))
    im.paste((0, 0, 255, 255), (50, 50, 150, 150))
    im.save(buf, "PNG")
    assert Image.open(io.BytesIO(process_image(buf.getvalue())["full"])).mode == "RGBA"
    buf = io.BytesIO()
    Image.new("RGBA", (200, 200), (255, 0, 0, 255)).save(buf, "PNG")
    assert Image.open(io.BytesIO(process_image(buf.getvalue())["full"])).mode == "RGB"


# ── Posting rules ────────────────────────────────────────────────────────────

def test_create_question_and_view(app, alice, bob):
    resp = create(app, alice)
    assert resp.status_code == 201, resp.get_json()
    pid = resp.get_json()["post"]["id"]
    page = as_user(app, bob).get(f"/community/p/{pid}")
    assert page.status_code == 200
    assert b"How do I enable rain" in page.data
    assert b"alice@example.com" not in page.data  # never leak emails to users


def test_bug_requires_steps_expected_actual_format(app, alice):
    resp = create(app, alice, BUG, steps=["only one step"], expected="", match_format="")
    assert resp.status_code == 400
    fields = resp.get_json()["fields"]
    assert {"steps", "expected", "match_format"} <= set(fields)
    ok = create(app, alice, BUG)
    assert ok.status_code == 201
    post = CommunityPost.query.filter_by(public_id=ok.get_json()["post"]["id"]).one()
    assert json.loads(post.steps_json)[0] == "Start a T20 match with rain enabled"


def test_non_english_post_rejected(app, alice):
    resp = create(app, alice, title="bhai match nahi chal raha hai",
                  body="kya karu bhai, match nahi chal raha hai yaar, jaldi fix karo")
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "not_english"


def test_profanity_rejected(app, alice):
    resp = create(app, alice, body="This fucking match engine keeps crashing on me every time.")
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "profanity"


def test_announcement_is_admin_only(app, alice, boss):
    assert create(app, alice, flair="announcement").status_code == 400
    resp = create(app, boss, flair="announcement", title="Scheduled maintenance tonight",
                  body="The site will be down for thirty minutes for an upgrade.")
    assert resp.status_code == 201
    assert resp.get_json()["post"]["is_pinned"] is True


@pytest.mark.parametrize("attrs,code", [
    ({"age_days": 0}, "new_account"),
    ({"verified": False}, "unverified"),
    ({"name": None}, "no_display_name"),
    ({"name": "Admin Helper"}, "reserved_name"),
    ({"community_muted_until": datetime.utcnow() + timedelta(hours=5)}, "muted"),
])
def test_posting_blocks(app, attrs, code):
    user = make_user("blocked@example.com", **attrs)
    resp = create(app, user)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == code


def test_new_account_can_still_vote(app, alice):
    pid = create(app, alice).get_json()["post"]["id"]
    newbie = make_user("newbie@example.com", name="Newbie", age_days=0)
    resp = as_user(app, newbie).post(f"/api/community/posts/{pid}/vote")
    assert resp.status_code == 200 and resp.get_json()["vote"] == 1


# ── Visibility ───────────────────────────────────────────────────────────────

def test_private_post_hidden_from_others(app, alice, bob, boss, media_dir):
    alice_client = as_user(app, alice)
    up = alice_client.post("/api/community/images", data={"image": (io.BytesIO(jpeg_bytes()), "shot.jpg")},
                           content_type="multipart/form-data")
    assert up.status_code == 201, up.get_json()
    image = up.get_json()["image"]
    resp = alice_client.post("/api/community/posts", json={**QUESTION, "visibility": "private", "image_ids": [image["id"]]})
    assert resp.status_code == 201
    pid = resp.get_json()["post"]["id"]

    bob_client = as_user(app, bob)
    assert bob_client.get(f"/community/p/{pid}").status_code == 404
    assert bob_client.post(f"/api/community/posts/{pid}/vote").status_code == 404
    assert bob_client.get(image["url"]).status_code == 404
    assert pid.encode() not in bob_client.get("/community").data

    assert alice_client.get(image["url"]).status_code == 200
    admin = as_user(app, boss)
    assert admin.get(f"/community/p/{pid}").status_code == 200
    assert admin.get(image["url"]).headers["Content-Type"] == "image/webp"


def test_unattached_upload_only_visible_to_uploader(app, alice, bob, media_dir):
    up = as_user(app, alice).post("/api/community/images", data={"image": (io.BytesIO(jpeg_bytes()), "a.jpg")},
                                  content_type="multipart/form-data")
    url = up.get_json()["image"]["url"]
    assert as_user(app, bob).get(url).status_code == 404


def test_cannot_attach_someone_elses_upload(app, alice, bob, media_dir):
    up = as_user(app, bob).post("/api/community/images", data={"image": (io.BytesIO(jpeg_bytes()), "b.jpg")},
                                content_type="multipart/form-data")
    image_id = up.get_json()["image"]["id"]
    create(app, alice, image_ids=[image_id])
    assert CommunityImage.query.filter_by(public_id=image_id).one().post_id is None


def test_upload_rejects_fake_image(app, alice, media_dir):
    resp = as_user(app, alice).post("/api/community/images",
                                    data={"image": (io.BytesIO(b"<svg onload=alert(1)>"), "x.jpg")},
                                    content_type="multipart/form-data")
    assert resp.status_code == 400


def test_non_admin_cannot_edit_others_post(app, alice, bob):
    pid = create(app, alice).get_json()["post"]["id"]
    resp = as_user(app, bob).patch(f"/api/community/posts/{pid}", json={**QUESTION, "title": "Hijacked title here"})
    assert resp.status_code == 403
    assert as_user(app, bob).delete(f"/api/community/posts/{pid}").status_code == 403


# ── Votes ────────────────────────────────────────────────────────────────────

def test_vote_toggle_and_own_post(app, alice, bob):
    pid = create(app, alice, BUG).get_json()["post"]["id"]
    bob_client = as_user(app, bob)
    assert bob_client.post(f"/api/community/posts/{pid}/vote").get_json() == {"vote": 1, "up": 1, "down": 0, "score": 1}
    assert bob_client.post(f"/api/community/posts/{pid}/vote").get_json() == {"vote": 0, "up": 0, "down": 0, "score": 0}
    own = as_user(app, alice).post(f"/api/community/posts/{pid}/vote")
    assert own.status_code == 400


def test_downvote_switch_and_clear(app, alice, bob):
    pid = create(app, alice, BUG).get_json()["post"]["id"]
    carol = make_user("carol@example.com", name="Carol")
    as_user(app, carol).post(f"/api/community/posts/{pid}/vote", json={"value": 1})
    bob_client = as_user(app, bob)
    url = f"/api/community/posts/{pid}/vote"
    assert bob_client.post(url, json={"value": -1}).get_json() == {"vote": -1, "up": 1, "down": 1, "score": 0}
    assert bob_client.post(url, json={"value": 1}).get_json() == {"vote": 1, "up": 2, "down": 0, "score": 2}
    assert bob_client.post(url, json={"value": -1}).get_json()["score"] == 0
    assert bob_client.post(url, json={"value": -1}).get_json() == {"vote": 0, "up": 1, "down": 0, "score": 1}
    assert bob_client.post(url, json={"value": 5}).status_code == 400
    assert CommunityVote.query.count() == 1


def test_top_sort_uses_score(app, alice, bob):
    carol = make_user("carol@example.com", name="Carol")
    liked = create(app, alice, title="A post everyone agrees with here").get_json()["post"]["id"]
    disliked = create(app, alice, title="A post everyone disagrees with here").get_json()["post"]["id"]
    for user in (bob, carol):
        c = as_user(app, user)
        c.post(f"/api/community/posts/{disliked}/vote", json={"value": -1})
        c.post(f"/api/community/posts/{liked}/vote", json={"value": 1})
    page = as_user(app, bob).get("/community?sort=top").get_data(as_text=True)
    assert page.index("agrees with") < page.index("disagrees with")
    assert 'aria-pressed="true"' in page  # the viewer's own votes are highlighted in the list


def test_duplicate_merge_ignores_downvotes_and_fixed_notifies_upvoters_only(app, alice, bob, boss):
    carol = make_user("carol@example.com", name="Carol")
    original = create(app, alice, BUG).get_json()["post"]["id"]
    dupe = create(app, alice, BUG, title="Wrong bowler listed after the rain break").get_json()["post"]["id"]
    as_user(app, carol).post(f"/api/community/posts/{dupe}/vote", json={"value": -1})
    as_user(app, bob).post(f"/api/community/posts/{original}/vote", json={"value": -1})
    admin = as_user(app, boss)
    admin.post(f"/api/admin/community/posts/{dupe}/moderate", json={"action": "duplicate", "value": original})
    orig = CommunityPost.query.filter_by(public_id=original).one()
    assert {(v.user_id, v.value) for v in CommunityVote.query.filter_by(post_id=orig.id)} == {("bob@example.com", -1)}
    admin.post(f"/api/admin/community/posts/{original}/moderate", json={"action": "status", "value": "fixed"})
    assert CommunityNotification.query.filter_by(user_id="bob@example.com", kind="status_change").count() == 0


# ── Comments ─────────────────────────────────────────────────────────────────

def test_nested_replies_flatten_to_one_level(app, alice, bob):
    pid = create(app, alice).get_json()["post"]["id"]
    bob_client = as_user(app, bob)
    top = bob_client.post(f"/api/community/posts/{pid}/comments", json={"body": "Open the match settings page first."})
    assert top.status_code == 201
    top_id = top.get_json()["comment"]["id"]
    alice_client = as_user(app, alice)
    reply = alice_client.post(f"/api/community/posts/{pid}/comments",
                              json={"body": "Thanks, where exactly is that?", "parent_id": top_id}).get_json()["comment"]
    deeper = bob_client.post(f"/api/community/posts/{pid}/comments",
                             json={"body": "It is under the gear icon.", "parent_id": reply["id"]}).get_json()["comment"]
    assert reply["parent_id"] == top_id
    assert deeper["parent_id"] == top_id  # reply-to-reply joins the top thread
    assert deeper["body"].startswith("@Alice ")
    kinds = {n.kind for n in CommunityNotification.query.filter_by(user_id="bob@example.com").all()}
    assert "reply" in kinds or "comment" in kinds
    assert CommunityPost.query.filter_by(public_id=pid).one().comment_count == 3


def test_locked_post_rejects_comments(app, alice, bob, boss):
    pid = create(app, alice).get_json()["post"]["id"]
    admin = as_user(app, boss)
    assert admin.post(f"/api/admin/community/posts/{pid}/moderate", json={"action": "lock"}).status_code == 200
    resp = as_user(app, bob).post(f"/api/community/posts/{pid}/comments", json={"body": "Can I still add this comment?"})
    assert resp.status_code == 403
    # Admins can still answer on a locked thread.
    assert admin.post(f"/api/community/posts/{pid}/comments", json={"body": "Locked, see the FAQ."}).status_code == 201


def test_deleted_comment_with_replies_becomes_tombstone(app, alice, bob):
    pid = create(app, alice).get_json()["post"]["id"]
    bob_client = as_user(app, bob)
    top_id = bob_client.post(f"/api/community/posts/{pid}/comments",
                             json={"body": "First answer that will be deleted."}).get_json()["comment"]["id"]
    as_user(app, alice).post(f"/api/community/posts/{pid}/comments", json={"body": "Replying to that answer.", "parent_id": top_id})
    assert bob_client.delete(f"/api/community/comments/{top_id}").status_code == 200
    page = as_user(app, alice).get(f"/community/p/{pid}").data
    assert b"[deleted]" in page
    assert b"First answer that will be deleted" not in page
    assert b"Replying to that answer" in page


# ── Moderation ───────────────────────────────────────────────────────────────

def test_admin_status_change_notifies_author_and_voters(app, alice, bob, boss):
    pid = create(app, alice, BUG).get_json()["post"]["id"]
    as_user(app, bob).post(f"/api/community/posts/{pid}/vote")
    resp = as_user(app, boss).post(f"/api/admin/community/posts/{pid}/moderate", json={"action": "status", "value": "fixed"})
    assert resp.status_code == 200
    for uid in ("alice@example.com", "bob@example.com"):
        assert CommunityNotification.query.filter_by(user_id=uid, kind="status_change").count() == 1
    assert AdminAuditLog.query.filter_by(action="community_status", target=pid).count() == 1
    # Fixed is terminal: comments close for users.
    bob_resp = as_user(app, bob).post(f"/api/community/posts/{pid}/comments", json={"body": "Still broken for me today."})
    assert bob_resp.status_code == 403


def test_admin_reply_email_not_swallowed_by_other_comments(app, alice, bob, boss, monkeypatch):
    sent = []
    monkeypatch.setattr("utils.email_service.send_email", lambda to, subject, html: sent.append((to, subject)) or True)
    pid = create(app, alice).get_json()["post"]["id"]
    as_user(app, bob).post(f"/api/community/posts/{pid}/comments", json={"body": "I have the same question as you."})
    admin = as_user(app, boss)
    admin.post(f"/api/community/posts/{pid}/comments", json={"body": "Here is the answer to that."})
    assert sent == [(alice.id, "An admin replied to your post")]
    admin.post(f"/api/community/posts/{pid}/comments", json={"body": "One more detail for you."})
    assert len(sent) == 1  # at most one email per post per hour


def test_mark_duplicate_moves_votes(app, alice, bob, boss):
    original = create(app, alice, BUG).get_json()["post"]["id"]
    carol = make_user("carol@example.com", name="Carol")
    dupe = create(app, bob, BUG, title="Wrong bowler in scorecard after rain delay").get_json()["post"]["id"]
    as_user(app, carol).post(f"/api/community/posts/{dupe}/vote")
    resp = as_user(app, boss).post(f"/api/admin/community/posts/{dupe}/moderate", json={"action": "duplicate", "value": original})
    assert resp.status_code == 200, resp.get_json()
    orig = CommunityPost.query.filter_by(public_id=original).one()
    voters = {v.user_id for v in CommunityVote.query.filter_by(post_id=orig.id)}
    assert voters == {"carol@example.com", "bob@example.com"}
    assert orig.vote_count == 2
    d = CommunityPost.query.filter_by(public_id=dupe).one()
    assert d.status == "duplicate" and d.duplicate_of_id == orig.id


def test_admin_delete_restore_purge(app, alice, boss, media_dir):
    alice_client = as_user(app, alice)
    image_id = alice_client.post("/api/community/images", data={"image": (io.BytesIO(jpeg_bytes()), "a.jpg")},
                                 content_type="multipart/form-data").get_json()["image"]["id"]
    pid = alice_client.post("/api/community/posts", json={**QUESTION, "image_ids": [image_id]}).get_json()["post"]["id"]
    admin = as_user(app, boss)
    assert admin.delete(f"/api/community/posts/{pid}", json={"reason": "Spam"}).status_code == 200
    assert alice_client.get(f"/community/p/{pid}").status_code == 404
    assert admin.post(f"/api/admin/community/posts/{pid}/moderate", json={"action": "restore"}).status_code == 200
    assert alice_client.get(f"/community/p/{pid}").status_code == 200
    # Purge requires a soft delete first.
    assert admin.post(f"/api/admin/community/posts/{pid}/moderate", json={"action": "purge"}).status_code == 400
    admin.delete(f"/api/community/posts/{pid}")
    img = CommunityImage.query.filter_by(public_id=image_id).one()
    path = community_media.file_path_for(img, "full")
    assert admin.post(f"/api/admin/community/posts/{pid}/moderate", json={"action": "purge"}).status_code == 200
    assert CommunityPost.query.filter_by(public_id=pid).first() is None
    import os
    assert not os.path.exists(path)


def test_mute_blocks_posting(app, alice, boss):
    admin = as_user(app, boss)
    assert admin.post("/api/admin/community/users/mute", json={"email": alice.id, "hours": 24}).status_code == 200
    assert create(app, alice).get_json()["code"] == "muted"
    admin.post("/api/admin/community/users/mute", json={"email": alice.id, "hours": 0})
    assert create(app, alice).status_code == 201


def test_non_admin_cannot_moderate(app, alice, bob):
    pid = create(app, alice).get_json()["post"]["id"]
    resp = as_user(app, bob).post(f"/api/admin/community/posts/{pid}/moderate", json={"action": "pin"})
    assert resp.status_code == 403


def test_reports_and_pending_count(app, alice, bob, boss):
    pid = create(app, alice, visibility="private").get_json()["post"]["id"]
    public = create(app, alice, title="Another question about the scorecard page").get_json()["post"]["id"]
    assert as_user(app, bob).post("/api/community/reports", json={"post_id": public, "reason": "spam"}).status_code == 200
    as_user(app, bob).post("/api/community/reports", json={"post_id": public, "reason": "spam"})  # deduped
    assert CommunityReport.query.count() == 1
    admin = as_user(app, boss)
    assert admin.get("/api/admin/community/pending-count").get_json()["count"] == 2  # 1 private + 1 report
    admin.post(f"/api/community/posts/{pid}/comments", json={"body": "Looking into your account now."})
    assert admin.get("/api/admin/community/pending-count").get_json()["count"] == 1
    assert admin.get("/admin/community").status_code == 200


# ── Search ───────────────────────────────────────────────────────────────────

def test_similar_posts_via_fts(app, alice, bob, fts):
    create(app, alice, BUG)
    create(app, alice, visibility="private", title="Private rain scorecard problem here")
    resp = as_user(app, bob).get("/api/community/similar?q=wrong bowler scorecard")
    titles = [p["title"] for p in resp.get_json()["posts"]]
    assert titles == ["Scorecard shows the wrong bowler after rain"]


def test_search_falls_back_without_fts(app, alice, bob):
    create(app, alice, BUG)
    page = as_user(app, bob).get("/community?q=bowler")
    assert b"Scorecard shows the wrong bowler" in page.data


# ── Retention ────────────────────────────────────────────────────────────────

def test_retention_rules(app, alice, boss, media_dir):
    alice_client = as_user(app, alice)
    orphan_id = alice_client.post("/api/community/images", data={"image": (io.BytesIO(jpeg_bytes()), "o.jpg")},
                                  content_type="multipart/form-data").get_json()["image"]["id"]
    private = create(app, alice, visibility="private").get_json()["post"]["id"]
    deleted = create(app, alice, title="This post will be deleted soon").get_json()["post"]["id"]
    alice_client.delete(f"/api/community/posts/{deleted}")
    as_user(app, boss).post(f"/api/admin/community/posts/{private}/moderate", json={"action": "status", "value": "closed"})

    # Nothing is due yet.
    community_retention.run_cleanup()
    assert CommunityImage.query.filter_by(public_id=orphan_id).count() == 1
    assert CommunityPost.query.count() == 2

    later = datetime.utcnow() + timedelta(days=91)
    stats = community_retention.run_cleanup(now=later)
    assert stats["orphan_uploads"] == 1 and stats["deleted_posts"] == 1 and stats["private_posts"] == 1
    assert CommunityPost.query.count() == 0
    assert CommunityImage.query.count() == 0


def test_private_posts_of_deleted_accounts_are_purged(app, boss):
    temp = make_user("temp@example.com", name="Temp")
    create(app, temp, visibility="private")
    public = create(app, temp, title="Public question from a leaving user").get_json()["post"]["id"]
    db.session.delete(db.session.get(User, "temp@example.com"))
    db.session.commit()
    community_retention.run_cleanup()
    remaining = CommunityPost.query.all()
    assert [p.public_id for p in remaining] == [public]
    assert remaining[0].author_id is None
    page = as_user(app, boss).get(f"/community/p/{public}")
    assert b"[deleted user]" in page.data


# ── Migrations ───────────────────────────────────────────────────────────────

def test_add_community_migration_idempotent(app):
    from migrations.add_community import run_migration
    run_migration(db, app)
    run_migration(db, app)
    with db.engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master"))}
        conn.execute(text("DROP TABLE IF EXISTS community_posts_fts"))
        conn.commit()
    assert {"community_posts", "community_posts_fts", "community_posts_fts_ai"} <= names


def test_drop_support_migration(app):
    from migrations.drop_support_messaging import run_migration
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE support_conversation (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE support_message (id INTEGER PRIMARY KEY)"))
    run_migration(db, app)
    run_migration(db, app)
    with db.engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert not {n for n in names if n.startswith("support_")}


def test_precheck_registry_no_longer_recreates_support():
    from migrations.precheck import MIGRATIONS
    names = [n for n, _ in MIGRATIONS]
    assert "add_support_messaging" not in names
    assert names.index("drop_support_messaging") > names.index("add_community")


def test_status_change_counts_as_triage(app, alice, boss):
    pid = create(app, alice, BUG).get_json()["post"]["id"]
    admin = as_user(app, boss)
    admin.post(f"/api/admin/community/posts/{pid}/moderate", json={"action": "status", "value": "under_review"})
    assert CommunityPost.query.filter_by(public_id=pid).one().needs_admin is False


def test_private_posts_cannot_be_voted_on(app, alice, boss):
    pid = create(app, alice, visibility="private").get_json()["post"]["id"]
    resp = as_user(app, boss).post(f"/api/community/posts/{pid}/vote")
    assert resp.status_code == 400 and resp.get_json()["code"] == "private"


# ── Live search / autocomplete ───────────────────────────────────────────────

def test_autocomplete_matches_half_typed_words_and_ranks_all_terms_first(app, alice, bob, fts):
    create(app, alice, BUG)  # "Scorecard shows the wrong bowler after rain"
    create(app, alice, title="Wrong team name shown on the home page",
           body="The home page greets me with the wrong team name after I rename a team.")
    create(app, alice, title="Private scorecard wrong bowler question", visibility="private")
    data = as_user(app, bob).get("/api/community/search?q=wrong bowl").get_json()
    titles = [s["title"] for s in data["suggestions"]]
    assert titles[0] == "Scorecard shows the wrong bowler after rain"   # both terms, "bowl" as a prefix
    assert "Wrong team name shown on the home page" in titles          # any-term match tops up
    assert all("Private" not in t for t in titles)                     # other people's private posts never leak
    assert data["terms"] == ["wrong", "bowl"]
    assert as_user(app, bob).get("/api/community/search?q=w").get_json()["suggestions"] == []


def test_autocomplete_snippet_and_own_private_post(app, alice, fts):
    create(app, alice, title="Cannot change my account email", visibility="private",
           body="Every time I try to verify the new address the link has already expired, even when I click at once.")
    data = as_user(app, alice).get("/api/community/search?q=expired link").get_json()
    assert len(data["suggestions"]) == 1
    hit = data["suggestions"][0]
    assert hit["visibility"] == "private" and "expired" in hit["snippet"]


def test_did_you_mean_for_typos(app, alice, bob, fts, monkeypatch):
    monkeypatch.setattr(cs, "_vocab_cache", (0.0, frozenset()))
    create(app, alice, BUG)
    data = as_user(app, bob).get("/api/community/search?q=scorcard").get_json()
    # prefix search misses the typo, so the dropdown offers a correction
    assert data["suggestions"] == [] and data["did_you_mean"] == "scorecard"
    page = as_user(app, bob).get("/community?q=scorcard").get_data(as_text=True)
    assert "Did you mean" in page and 'data-dym="scorecard"' in page


def test_partial_results_fragment(app, alice, bob, fts):
    create(app, alice, BUG)
    frag = as_user(app, bob).get("/community?q=bowler&partial=1").get_data(as_text=True)
    assert "Scorecard shows the wrong bowler" in frag
    assert "<html" not in frag and "cm-hero" not in frag  # just the results block
    full = as_user(app, bob).get("/community?q=bowler").get_data(as_text=True)
    assert 'role="combobox"' in full and 'id="cm-results"' in full


def test_search_ranks_duplicates_last(app, alice, bob, boss, fts):
    original = create(app, alice, BUG, title="Bowler credited wrongly after a rain break").get_json()["post"]["id"]
    dupe = create(app, bob, BUG, title="Wrong bowler after rain").get_json()["post"]["id"]
    as_user(app, boss).post(f"/api/admin/community/posts/{dupe}/moderate", json={"action": "duplicate", "value": original})
    ids = [s["id"] for s in as_user(app, bob).get("/api/community/search?q=wrong bowler rain").get_json()["suggestions"]]
    assert ids == [original, dupe]
    similar = as_user(app, bob).get("/api/community/similar?q=wrong bowler after rain").get_json()["posts"]
    assert [p["id"] for p in similar] == [original]
