"""Feature 2: pure policy, non-destructive migration and SQLite races."""
import subprocess
import sys

from flask import Flask
import pytest
from sqlalchemy import inspect, select, text

from database import db
from database.models import IdentityEmailReservation, User
from migrations.add_identity_foundation import run_migration
from migrations.verify import fingerprint, inventory, model_drift
from migrations.precheck import MIGRATIONS
from utils.identity_reservations import IdentityConflict, reserve_new_email
from utils.identity_rules import (
    InvalidEmail, disposable_domains, normalize_email, stored_address_key,
    validate_new_email,
)


@pytest.mark.parametrize("address,stored,canonical", [
    ("  First.Last@GMAIL.COM  ", "first.last@gmail.com", "firstlast@gmail.com"),
    ("First.Last+legacy@googlemail.com", "first.last+legacy@googlemail.com", "firstlast@gmail.com"),
    ("First.Last+legacy@outlook.com", "first.last+legacy@outlook.com", "first.last+legacy@outlook.com"),
    ("First.Last@work.example", "first.last@work.example", "first.last@work.example"),
    ("USER@bücher.de", "user@xn--bcher-kva.de", "user@xn--bcher-kva.de"),
    ("USER@XN--BCHER-KVA.DE", "user@xn--bcher-kva.de", "user@xn--bcher-kva.de"),
])
def test_normalization(address, stored, canonical):
    identity = normalize_email(address)
    assert identity.address == stored
    assert identity.canonical == canonical
    assert stored_address_key(address) == stored
    assert identity.version == 1


@pytest.mark.parametrize("address", [
    "person@gmail.com", "person@outlook.com", "person@yahoo.com",
    "person@icloud.com", "person@proton.me", "person@school.edu",
    "person@new-custom-company.example", "person@bücher.de",
    "o'brien@company.example", "person@mailinator.com.example",
    "person@ordinary-prefix-mailinator.com",
])
def test_ordinary_and_unknown_domains_allowed(address):
    assert validate_new_email(address).address


@pytest.mark.parametrize("address,code", [
    ("person+tag@gmail.com", "plus_tag_not_allowed"),
    ("person+tag@work.example", "plus_tag_not_allowed"),
    ("person+@outlook.com", "plus_tag_not_allowed"),
    ("person@mailinator.com", "disposable_domain"),
    ("person@sub.mailinator.com", "disposable_domain"),
    ("person@MAILINATOR.COM", "disposable_domain"),
    ("person@yopmail.com", "disposable_domain"),
    ("üser@example.com", "non_ascii_local"),
    ("", "invalid_syntax"), (None, "invalid_syntax"),
    ("a@@example.com", "invalid_syntax"), ("a b@example.com", "invalid_syntax"),
    ("a\nB@example.com", "invalid_syntax"), (".a@example.com", "invalid_syntax"),
    ("a..b@example.com", "invalid_syntax"), ("a.@example.com", "invalid_syntax"),
    ('"a"@example.com', "invalid_syntax"),
    ("a@localhost", "invalid_domain"), ("a@example.com.", "invalid_domain"),
    ("a@-example.com", "invalid_domain"), ("a@exa_mple.com", "invalid_domain"),
    ("a@[127.0.0.1]", "invalid_domain"), ("a@😭.com", "invalid_domain"),
    ("a" * 65 + "@example.com", "invalid_syntax"),
    ("a" * 64 + "@" + ".".join(["b" * 63] * 3), "address_too_long"),
])
def test_new_address_rejections(address, code):
    with pytest.raises(InvalidEmail) as error:
        validate_new_email(address)
    assert error.value.code == code


def test_login_key_does_not_accept_provider_aliases():
    assert stored_address_key("F.Last@googlemail.com") == "f.last@googlemail.com"
    assert stored_address_key("f.last@gmail.com") != stored_address_key("flast@gmail.com")
    # Legacy policy-invalid addresses still normalize for evidence/login lookup.
    assert stored_address_key("FIRST+old@gmail.com") == "first+old@gmail.com"
    assert normalize_email("a@mailinator.com").canonical == "a@mailinator.com"


def test_bundled_blocklist_loaded():
    assert len(disposable_domains()) > 9000
    assert "mailinator.com" in disposable_domains()
    assert "gmail.com" not in disposable_domains()


@pytest.fixture
def identity_db(tmp_path):
    path = tmp_path / "identity.db"
    app = Flask("identity-foundation")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + str(path)
    db.init_app(app)
    with app.app_context():
        yield app, db.engine, path
        db.session.remove()
        db.engine.dispose()


def test_legacy_backfill_preserves_accounts_and_collisions(identity_db):
    app, engine, path = identity_db
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE users (id VARCHAR(120) PRIMARY KEY, stable_id TEXT, password_hash TEXT)")
        conn.exec_driver_sql("CREATE TABLE owned (id INTEGER PRIMARY KEY, user_id TEXT REFERENCES users(id))")
        for i, email in enumerate(["First.Last@gmail.com", "firstlast+old@googlemail.com", "bad-address", "a@mailinator.com"]):
            conn.execute(text("INSERT INTO users VALUES (:email, :stable, :password)"),
                         {"email": email, "stable": f"stable-{i}", "password": f"unchanged-{i}"})
            conn.execute(text("INSERT INTO owned VALUES (:id, :email)"), {"id": i, "email": email})
        original = conn.exec_driver_sql("SELECT id, stable_id, password_hash FROM users ORDER BY id").all()
    run_migration(db, app)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT id, stable_id, password_hash FROM users ORDER BY id").all() == original
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM owned").scalar_one() == 4
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM users WHERE canonical_email='firstlast@gmail.com'").scalar_one() == 2
        assert conn.exec_driver_sql("SELECT email_normalization_error FROM users WHERE id='bad-address'").scalar_one() == "invalid_syntax"
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM identity_email_reservations").scalar_one() == 2
    before = fingerprint(path)
    run_migration(db, app)
    assert fingerprint(path) == before
    with engine.begin() as conn:
        with pytest.raises(IdentityConflict):
            reserve_new_email(conn, "f.i.r.s.t.l.a.s.t@gmail.com")


def test_fresh_and_migrated_schema_match(identity_db):
    app, engine, path = identity_db
    db.create_all()
    assert model_drift(engine, db.metadata) == []
    with engine.connect() as conn:
        expected_columns = inspect(conn).get_columns("identity_email_reservations")
        expected_indexes = inspect(conn).get_indexes("users")
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE identity_email_reservations")
        conn.exec_driver_sql("DROP INDEX ix_users_canonical_email")
        for name in ("canonical_email", "email_normalization_version", "email_normalization_error"):
            conn.exec_driver_sql(f"ALTER TABLE users DROP COLUMN {name}")
    run_migration(db, app)
    assert model_drift(engine, db.metadata) == []
    with engine.connect() as conn:
        assert str(inspect(conn).get_columns("identity_email_reservations")) == str(expected_columns)
        assert inspect(conn).get_indexes("users") == expected_indexes
        canonical_index = next(i for i in expected_indexes if i["name"] == "ix_users_canonical_email")
        assert not canonical_index["unique"]
    assert "add_identity_foundation" in dict(MIGRATIONS)
    assert inventory(dict(MIGRATIONS)) == []


def test_migration_failure_rolls_back_ddl_and_data(identity_db, monkeypatch):
    app, engine, path = identity_db
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE users (id TEXT PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO users VALUES ('a@example.com')")
    before = fingerprint(path)
    def fail(_):
        raise RuntimeError("injected backfill failure")
    monkeypatch.setattr("migrations.add_identity_foundation.normalize_email", fail)
    with pytest.raises(RuntimeError, match="injected"):
        run_migration(db, app)
    assert fingerprint(path) == before


def test_reservation_and_account_rollback_together(identity_db):
    app, engine, path = identity_db
    db.create_all()
    with pytest.raises(RuntimeError):
        with engine.begin() as conn:
            identity = reserve_new_email(conn, "rollback@company.example")
            conn.execute(User.__table__.insert().values(id=identity.address, canonical_email=identity.canonical))
            raise RuntimeError("account write failed")
    with engine.connect() as conn:
        assert conn.execute(select(db.func.count()).select_from(User)).scalar_one() == 0
        assert conn.execute(select(db.func.count()).select_from(IdentityEmailReservation)).scalar_one() == 0
    with engine.begin() as conn:
        assert reserve_new_email(conn, "rollback@company.example").address == "rollback@company.example"


@pytest.mark.parametrize("migrated", [False, True])
def test_concurrent_reservations_allow_one_account(identity_db, migrated):
    app, engine, path = identity_db
    db.create_all()
    if migrated:
        run_migration(db, app)
    # Real processes avoid the app's gevent monkey-patched threads. Keep the
    # winning transaction open briefly so the other processes contend for it.
    worker = '''
import sys, time
from sqlalchemy import create_engine
from database.models import User
from utils.identity_reservations import IdentityConflict, reserve_new_email
engine = create_engine('sqlite:///' + sys.argv[1])
print('READY', flush=True)
input()
try:
    with engine.begin() as conn:
        identity = reserve_new_email(conn, sys.argv[2])
        time.sleep(0.1)
        conn.execute(User.__table__.insert().values(
            id=identity.address, canonical_email=identity.canonical,
            email_normalization_version=identity.version))
    print('CREATED')
except IdentityConflict:
    print('CONFLICT')
finally:
    engine.dispose()
'''
    workers = [subprocess.Popen([sys.executable, "-c", worker, str(path), address],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
               for address in ["first.last@gmail.com", "firstlast@gmail.com",
                               "firstlast@googlemail.com", "f.irstlast@gmail.com"]]
    try:
        for process in workers:
            assert process.stdout.readline().strip() == "READY"
        for process in workers:
            process.stdin.write("go\n")
            process.stdin.flush()
        results = []
        for process in workers:
            output, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
            results.append(output.strip())
        assert results.count("CREATED") == 1
        assert results.count("CONFLICT") == 3
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    with engine.connect() as conn:
        assert conn.execute(select(db.func.count()).select_from(User)).scalar_one() == 1
        assert conn.execute(select(db.func.count()).select_from(IdentityEmailReservation)).scalar_one() == 1


def test_legacy_writes_since_backfill_cannot_evade_reservation(identity_db):
    app, engine, path = identity_db
    db.create_all()
    with engine.begin() as conn:
        conn.execute(User.__table__.insert().values(id="old.email@gmail.com", canonical_email="stale@gmail.com"))
    with pytest.raises(IdentityConflict):
        with engine.begin() as conn:
            reserve_new_email(conn, "oldemail@googlemail.com")
    with engine.connect() as conn:
        assert conn.execute(select(db.func.count()).select_from(IdentityEmailReservation)).scalar_one() == 0


def test_invalid_signup_never_reserves(identity_db):
    app, engine, path = identity_db
    db.create_all()
    with pytest.raises(InvalidEmail):
        with engine.begin() as conn:
            reserve_new_email(conn, "a@mailinator.com")
    with engine.connect() as conn:
        assert conn.execute(select(db.func.count()).select_from(IdentityEmailReservation)).scalar_one() == 0
