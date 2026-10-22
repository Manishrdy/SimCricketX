"""Add canonical evidence and reservations without modifying account keys.

SQLite write lock covers DDL, backfill and reservations. Reruns reconcile legacy
writers and are logical no-ops when current. Invalid legacy addresses are kept
with a reason; aliases are reserved as one group without choosing a survivor.
"""
from sqlalchemy import inspect, text
from sqlalchemy.dialects.sqlite import insert

from database.models import IdentityEmailReservation, User
from utils.identity_rules import InvalidEmail, NORMALIZATION_VERSION, normalize_email


def run_migration(db, app):
    with app.app_context(), db.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            if not inspect(conn).has_table("users"):
                raise RuntimeError("Identity foundation requires the users table")
            columns = {c["name"] for c in inspect(conn).get_columns("users")}
            for name, sql_type in (
                ("canonical_email", "VARCHAR(254)"),
                ("email_normalization_version", "INTEGER"),
                ("email_normalization_error", "VARCHAR(40)"),
            ):
                if name not in columns:
                    conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {name} {sql_type}")
            for index in User.__table__.indexes:
                if index.name == "ix_users_canonical_email":
                    index.create(conn, checkfirst=True)
            IdentityEmailReservation.__table__.create(conn, checkfirst=True)

            rows = conn.execute(text(
                "SELECT id, canonical_email, email_normalization_version, "
                "email_normalization_error FROM users"
            )).fetchall()
            for email, previous, version, previous_error in rows:
                try:
                    canonical, error = normalize_email(email).canonical, None
                except InvalidEmail as exc:
                    canonical, error = None, exc.code
                if (previous, version, previous_error) != (canonical, NORMALIZATION_VERSION, error):
                    conn.execute(text(
                        "UPDATE users SET canonical_email=:canonical, "
                        "email_normalization_version=:version, email_normalization_error=:error "
                        "WHERE id=:email"
                    ), {"canonical": canonical, "version": NORMALIZATION_VERSION,
                        "error": error, "email": email})
                if canonical:
                    conn.execute(insert(IdentityEmailReservation.__table__).values(
                        canonical_email=canonical
                    ).on_conflict_do_nothing(index_elements=["canonical_email"]))
            collisions = conn.execute(text(
                "SELECT COUNT(*) FROM (SELECT canonical_email FROM users "
                "WHERE canonical_email IS NOT NULL GROUP BY canonical_email HAVING COUNT(*) > 1)"
            )).scalar_one()
            invalid = conn.execute(text(
                "SELECT COUNT(*) FROM users WHERE email_normalization_error IS NOT NULL"
            )).scalar_one()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    # Counts only: do not emit private addresses into startup logs.
    print(f"[Migration] identity foundation: {collisions} collision groups; {invalid} unnormalizable addresses preserved.")
