"""Team logo: normaliser, serve/upload/delete routes, create/edit/clone, migration."""

import io

import pytest
from PIL import Image, PngImagePlugin

from database import db
from database.models import Team as DBTeam, TeamLogo, User
from tests.conftest import login_user, logout_user
from utils.team_logo import (
    LogoError, MAX_STORED_BYTES, MAX_UPLOAD_BYTES, STORED_EDGE, normalize_logo,
)


def _png(size=(300, 300), color=(200, 30, 30, 255), fmt="PNG", **save):
    buf = io.BytesIO()
    img = Image.new("RGBA", size, color)
    if fmt == "JPEG":
        img = img.convert("RGB")
    img.save(buf, format=fmt, **save)
    return buf.getvalue()


def _noise_png(size=(600, 600)):
    return _png_from(Image.effect_noise(size, 90).convert("RGBA").resize(size))


def _colour_noise(size=(512, 512)):
    bands = [Image.effect_noise(size, 100) for _ in range(3)]
    return _png_from(Image.merge("RGB", bands).convert("RGBA"))


def _png_from(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _open(png):
    return Image.open(io.BytesIO(png))


# ── normalize_logo ───────────────────────────────────────────────────────────

class TestNormalizeLogo:
    def test_valid_png_becomes_256_square_png(self):
        png, sha = normalize_logo(_png())
        img = _open(png)
        assert img.format == "PNG" and img.size == (STORED_EDGE, STORED_EDGE)
        assert len(sha) == 64 and len(png) <= MAX_STORED_BYTES

    def test_same_input_gives_same_hash(self):
        assert normalize_logo(_png())[1] == normalize_logo(_png())[1]

    def test_jpeg_renamed_to_png_is_rejected(self):
        with pytest.raises(LogoError, match="PNG"):
            normalize_logo(_png(fmt="JPEG"))

    def test_truncated_png_is_rejected(self):
        data = _colour_noise((300, 300))
        with pytest.raises(LogoError, match="could not be read"):
            normalize_logo(data[: len(data) // 2])

    def test_empty_upload_is_rejected(self):
        with pytest.raises(LogoError):
            normalize_logo(b"")

    def test_oversize_file_is_rejected_before_decoding(self):
        with pytest.raises(LogoError, match="1 MB") as exc:
            normalize_logo(b"\x89PNG\r\n\x1a\n" + b"0" * MAX_UPLOAD_BYTES)
        assert exc.value.status == 413

    def test_oversize_dimensions_are_rejected(self):
        with pytest.raises(LogoError, match="2048x2048"):
            normalize_logo(_png(size=(2049, 100)))

    def test_too_small_is_rejected(self):
        with pytest.raises(LogoError, match="at least 64x64"):
            normalize_logo(_png(size=(63, 200)))

    def test_non_square_is_padded_not_cropped(self):
        png, _ = normalize_logo(_png(size=(400, 200)))
        img = _open(png).convert("RGBA")
        assert img.size == (256, 256)
        assert img.getpixel((128, 10))[3] == 0            # transparent band above
        assert img.getpixel((128, 245))[3] == 0           # and below
        assert img.getpixel((2, 128)) == (200, 30, 30, 255)    # full width kept
        assert img.getpixel((253, 128)) == (200, 30, 30, 255)

    def test_small_image_is_scaled_up_to_fill(self):
        png, _ = normalize_logo(_png(size=(64, 64)))
        assert _open(png).convert("RGBA").getpixel((2, 2))[3] == 255

    def test_alpha_is_preserved(self):
        png, _ = normalize_logo(_png(color=(10, 20, 30, 128)))
        assert _open(png).convert("RGBA").getpixel((128, 128))[3] == 128

    def test_detailed_art_takes_the_palette_path_and_keeps_alpha(self):
        src = Image.open(io.BytesIO(_colour_noise((400, 200))))
        png, _ = normalize_logo(_png_from(src))
        img = _open(png)
        assert img.mode == "P" and len(png) <= MAX_STORED_BYTES
        assert img.convert("RGBA").getpixel((128, 10))[3] == 0

    def test_metadata_and_appended_payload_are_dropped(self):
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "secret-marker-text")
        data = _png(pnginfo=info) + b"APPENDED-PAYLOAD"
        png, _ = normalize_logo(data)
        assert b"secret-marker-text" not in png and b"APPENDED-PAYLOAD" not in png
        assert not _open(png).info.get("Comment")


# ── routes ───────────────────────────────────────────────────────────────────

def _upload(client, team_id, data=None, name="logo.png"):
    return client.post(
        f"/team/{team_id}/logo",
        data={"logo": (io.BytesIO(_png() if data is None else data), name)},
        content_type="multipart/form-data",
    )


@pytest.fixture
def other_user(app):
    from werkzeug.security import generate_password_hash
    user = User(id="other@example.com", email="other@example.com",
                password_hash=generate_password_hash("Password123!"),
                display_name="Other User", is_admin=False, is_banned=False,
                force_password_reset=False, email_verified=True)
    db.session.add(user)
    db.session.commit()
    return user


class TestLogoRoutes:
    def test_no_logo_is_404(self, authenticated_client, test_team):
        assert authenticated_client.get(f"/team/{test_team.id}/logo.png").status_code == 404

    def test_upload_then_serve(self, authenticated_client, test_team, app):
        assert test_team.updated_at is None
        res = _upload(authenticated_client, test_team.id)
        assert res.status_code == 200
        body = res.get_json()
        team = db.session.get(DBTeam, test_team.id)
        assert team.logo_version and body["logo_version"] == team.logo_version
        assert team.updated_at is not None
        assert body["url"] == f"/team/{team.id}/logo.png?v={team.logo_version}"

        row = db.session.get(TeamLogo, team.id)
        assert row.byte_size == len(row.png)

        got = authenticated_client.get(body["url"])
        assert got.status_code == 200 and got.mimetype == "image/png"
        assert got.data == row.png
        assert got.headers["ETag"] == f'"{row.sha256}"'
        assert "immutable" in got.headers["Cache-Control"]

    def test_etag_gives_304(self, authenticated_client, test_team):
        _upload(authenticated_client, test_team.id)
        etag = authenticated_client.get(f"/team/{test_team.id}/logo.png").headers["ETag"]
        res = authenticated_client.get(
            f"/team/{test_team.id}/logo.png", headers={"If-None-Match": etag})
        assert res.status_code == 304 and res.data == b""

    def test_replace_changes_version(self, authenticated_client, test_team):
        v1 = _upload(authenticated_client, test_team.id).get_json()["logo_version"]
        v2 = _upload(authenticated_client, test_team.id,
                     _png(color=(0, 0, 255, 255))).get_json()["logo_version"]
        assert v2 > v1
        assert TeamLogo.query.filter_by(team_id=test_team.id).count() == 1

    def test_version_is_not_reused_after_remove(self, authenticated_client, test_team):
        v1 = _upload(authenticated_client, test_team.id).get_json()["logo_version"]
        authenticated_client.delete(f"/team/{test_team.id}/logo")
        v2 = _upload(authenticated_client, test_team.id).get_json()["logo_version"]
        assert v2 != v1

    def test_delete(self, authenticated_client, test_team):
        _upload(authenticated_client, test_team.id)
        res = authenticated_client.delete(f"/team/{test_team.id}/logo")
        assert res.status_code == 200
        db.session.expire_all()
        assert db.session.get(DBTeam, test_team.id).logo_version is None
        assert db.session.get(TeamLogo, test_team.id) is None
        assert authenticated_client.get(f"/team/{test_team.id}/logo.png").status_code == 404

    def test_missing_file_is_400(self, authenticated_client, test_team):
        res = authenticated_client.post(f"/team/{test_team.id}/logo", data={},
                                        content_type="multipart/form-data")
        assert res.status_code == 400

    def test_bad_file_is_rejected_and_nothing_stored(self, authenticated_client, test_team):
        res = _upload(authenticated_client, test_team.id, _png(fmt="JPEG"))
        assert res.status_code == 415 and "PNG" in res.get_json()["error"]
        assert db.session.get(TeamLogo, test_team.id) is None

    def test_oversize_file_is_413(self, authenticated_client, test_team):
        res = _upload(authenticated_client, test_team.id,
                      b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_UPLOAD_BYTES + 10))
        assert res.status_code == 413

    def test_anonymous_cannot_read_or_write(self, client, test_team):
        assert client.get(f"/team/{test_team.id}/logo.png").status_code in (302, 401)
        assert _upload(client, test_team.id).status_code in (302, 401)
        assert db.session.get(TeamLogo, test_team.id) is None

    def test_other_user_can_view_but_not_change(self, client, test_team, regular_user, other_user):
        login_user(client, regular_user.email, "Password123!")
        _upload(client, test_team.id)
        logout_user(client)
        login_user(client, other_user.email, "Password123!")

        assert client.get(f"/team/{test_team.id}/logo.png").status_code == 200
        assert _upload(client, test_team.id).status_code == 404
        assert client.delete(f"/team/{test_team.id}/logo").status_code == 404
        assert db.session.get(TeamLogo, test_team.id) is not None

    def test_team_delete_removes_logo(self, authenticated_client, test_team):
        _upload(authenticated_client, test_team.id)
        team_id = test_team.id
        authenticated_client.post("/team/delete", data={"short_code": test_team.short_code})
        db.session.expire_all()
        assert db.session.get(DBTeam, team_id) is None
        assert TeamLogo.query.filter_by(team_id=team_id).count() == 0

    def test_clone_copies_logo(self, authenticated_client, test_team):
        _upload(authenticated_client, test_team.id)
        authenticated_client.post(f"/team/{test_team.short_code}/clone")
        clone = DBTeam.query.filter(DBTeam.id != test_team.id).one()
        assert clone.logo_version
        assert db.session.get(TeamLogo, clone.id).png == db.session.get(TeamLogo, test_team.id).png

    def test_manage_page_shows_logo_or_placeholder(self, authenticated_client, test_team):
        html = authenticated_client.get("/teams/manage").get_data(as_text=True)
        assert f"/team/{test_team.id}/logo.png" not in html
        v = _upload(authenticated_client, test_team.id).get_json()["logo_version"]
        html = authenticated_client.get("/teams/manage").get_data(as_text=True)
        assert f"/team/{test_team.id}/logo.png?v={v}" in html


# ── create / edit form ───────────────────────────────────────────────────────

def _create_form(**extra):
    from tests.test_team_routes import _valid_team_form
    form = _valid_team_form()
    form.update(extra)
    return form


class TestLogoOnCreateAndEdit:
    def test_create_with_logo(self, authenticated_client, team_stadium_id):
        form = _create_form(stadium_id=team_stadium_id, logo=(io.BytesIO(_png()), "crest.png"))
        res = authenticated_client.post("/team/create", data=form,
                                        content_type="multipart/form-data")
        assert res.status_code == 302
        team = DBTeam.query.one()
        assert team.logo_version and db.session.get(TeamLogo, team.id) is not None

    def test_create_without_logo(self, authenticated_client, team_stadium_id):
        authenticated_client.post("/team/create", data=_create_form(stadium_id=team_stadium_id))
        team = DBTeam.query.one()
        assert team.logo_version is None and db.session.get(TeamLogo, team.id) is None

    def test_bad_logo_still_creates_team_and_warns(self, authenticated_client, team_stadium_id):
        form = _create_form(stadium_id=team_stadium_id,
                            logo=(io.BytesIO(_png(fmt="JPEG")), "crest.png"))
        res = authenticated_client.post("/team/create", data=form,
                                        content_type="multipart/form-data",
                                        follow_redirects=True)
        team = DBTeam.query.one()
        assert team.logo_version is None
        assert "the logo was not" in res.get_data(as_text=True)

    def test_create_and_edit_pages_render(self, authenticated_client, test_team):
        html = authenticated_client.get("/team/create").get_data(as_text=True)
        assert 'name="logo"' in html and 'enctype="multipart/form-data"' in html
        v = _upload(authenticated_client, test_team.id).get_json()["logo_version"]
        html = authenticated_client.get(f"/team/{test_team.short_code}/edit").get_data(as_text=True)
        assert f"/team/{test_team.id}/logo.png?v={v}" in html


# ── migration ────────────────────────────────────────────────────────────────

def test_migration_is_idempotent(app):
    from sqlalchemy import text
    from migrations.add_team_logos import run_migration

    with db.engine.begin() as conn:
        conn.execute(text("DROP TABLE team_logos"))
        conn.execute(text("ALTER TABLE teams DROP COLUMN logo_version"))
    run_migration(db, app)
    run_migration(db, app)
    with db.engine.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(teams)"))]
        logo_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(team_logos)"))]
    assert "logo_version" in cols
    assert logo_cols == ["team_id", "png", "byte_size", "sha256", "updated_at"]
