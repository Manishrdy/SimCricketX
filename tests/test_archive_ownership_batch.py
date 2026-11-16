"""Batched archive ownership: same rules as owns_archive(), bounded queries.

/my-matches lists a folder shared by every user's exports. Resolving ownership
per file cost two or three queries for each of them on every page load.
"""
import hashlib
import os
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import event

from database import db
from database.models import IdentityArchiveOwner, IdentityCampaignFile, User
from utils.match_ownership import owned_archives, owns_archive

OTHER = "someone.else@example.com"


def _zip(folder, name):
    path = Path(folder) / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("match.json", "{}")
    return path


def _pin(path, stable_id, content_hash=None):
    db.session.add(IdentityArchiveOwner(
        filename=path.name, user_stable_id=stable_id,
        content_hash=content_hash or hashlib.sha256(path.read_bytes()).hexdigest(),
    ))


def _count_queries(fn):
    statements = []

    def record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        result = fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    return result, len(statements)


def test_batch_matches_per_file_rules(app, regular_user, tmp_path):
    me = regular_user.id
    with app.app_context():
        user = db.session.get(User, me)
        if not user.stable_id:
            user.stable_id = str(uuid.uuid4())
        my_stable = user.stable_id
        stranger = User(id=OTHER, password_hash="x", stable_id=str(uuid.uuid4()))
        db.session.add(stranger)
        db.session.flush()

        mine = _zip(tmp_path, f"playing_A_vs_B_{me}_1_fmt-T20.zip")
        theirs = _zip(tmp_path, f"playing_A_vs_B_{OTHER}_2_fmt-T20.zip")
        # Pinned to me although the filename still carries another address.
        pinned_mine = _zip(tmp_path, f"playing_A_vs_B_old@example.com_3_fmt-T20.zip")
        _pin(pinned_mine, my_stable)
        # My address in the name, but pinned to someone else: the pin wins.
        pinned_theirs = _zip(tmp_path, f"playing_A_vs_B_{me}_4_fmt-T20.zip")
        _pin(pinned_theirs, stranger.stable_id)
        # Pinned to me but the bytes changed since the pin was taken.
        tampered = _zip(tmp_path, f"playing_A_vs_B_{me}_5_fmt-T20.zip")
        _pin(tampered, my_stable, content_hash="0" * 64)
        # Pin whose owner was deleted matches nobody.
        orphan = _zip(tmp_path, f"playing_A_vs_B_{me}_6_fmt-T20.zip")
        _pin(orphan, None)
        # Held by an identity campaign that has not cleaned up yet.
        held = _zip(tmp_path, f"playing_A_vs_B_{me}_7_fmt-T20.zip")
        db.session.add(IdentityCampaignFile(
            member_id=str(uuid.uuid4()), area="archives", name=held.name, content_hash="0" * 64))
        not_zip = tmp_path / f"notes_{me}_.txt"
        not_zip.write_text("x")
        link = tmp_path / f"link_{me}_.zip"
        os.symlink(mine, link)
        db.session.commit()

        everything = [mine, theirs, pinned_mine, pinned_theirs, tampered, orphan, held, not_zip, link]
        assert owned_archives(everything, me) == {mine.name, pinned_mine.name}
        assert owned_archives(everything, OTHER) == {theirs.name, pinned_theirs.name}
        for path in everything:
            for who in (me, OTHER):
                assert owns_archive(path, who) == (path.name in owned_archives(everything, who)), path.name
        assert owned_archives([], me) == set()


def test_query_count_does_not_grow_with_folder_size(app, regular_user, tmp_path):
    me = regular_user.id
    with app.app_context():
        few = [_zip(tmp_path, f"p_{OTHER}_{i}_fmt-T20.zip") for i in range(3)]
        many = few + [_zip(tmp_path, f"q_{OTHER}_{i}_fmt-T20.zip") for i in range(150)]
        many.append(_zip(tmp_path, f"p_{me}_x_fmt-T20.zip"))
        _, few_queries = _count_queries(lambda: owned_archives(few, me))
        owned, many_queries = _count_queries(lambda: owned_archives(many, me))
        assert owned == {f"p_{me}_x_fmt-T20.zip"}
        assert few_queries == many_queries <= 3


def test_my_matches_lists_only_own_archives_in_bounded_queries(app, authenticated_client, regular_user):
    folder = app.config["ARCHIVES_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    mine = f"playing_A_vs_B_{regular_user.id}_20261101000000_fmt-T20.zip"
    created = [_zip(folder, mine)]
    try:
        _, baseline = _count_queries(lambda: authenticated_client.get("/my-matches"))
        created += [_zip(folder, f"playing_A_vs_B_{OTHER}_{i}_fmt-T20.zip") for i in range(60)]
        response, crowded = _count_queries(lambda: authenticated_client.get("/my-matches"))
        body = response.data.decode()
        assert response.status_code == 200
        assert mine in body and OTHER not in body
        assert crowded == baseline
    finally:
        for path in created:
            path.unlink(missing_ok=True)
