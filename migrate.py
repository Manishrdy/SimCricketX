"""
SimCricketX — Pre-deploy migration script.

Run this ONCE before starting the Flask server after pulling new code:
    python migrate.py

What it does:
  1. Backs up the current database (timestamped copy)
  2. Runs schema migration (adds missing columns/tables, never drops anything)
  3. Checks that an admin user exists — prompts to set one if not
"""

import os
import sys
import shutil
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_NAME = "cricket_sim.db"
DB_PATH = os.path.join(BASE_DIR, DB_NAME)
BACKUP_DIR = os.path.join(BASE_DIR, "backups")


def backup_database():
    """Create a timestamped backup of the database."""
    if not os.path.exists(DB_PATH):
        print(f"  [SKIP] No database found at {DB_PATH} — fresh install.")
        return False

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"pre_migrate_{ts}.db")
    shutil.copy2(DB_PATH, backup_path)
    size_mb = os.path.getsize(backup_path) / (1024 * 1024)
    print(f"  [OK] Backup created: {backup_path} ({size_mb:.2f} MB)")
    return True


def run_schema_migration(app, db):
    """Run ensure_schema to add missing columns and tables."""
    from sqlalchemy import inspect
    from scripts.fix_db_schema import ensure_schema

    with app.app_context():
        inspector = inspect(db.engine)
        tables_before = set(inspector.get_table_names())
        cols_before = {}
        for t in tables_before:
            cols_before[t] = set(c["name"] for c in inspector.get_columns(t))

        # Run migrations
        db.create_all()
        ensure_schema(db.engine, db)

        # Report changes
        inspector = inspect(db.engine)
        tables_after = set(inspector.get_table_names())
        new_tables = tables_after - tables_before
        if new_tables:
            for t in sorted(new_tables):
                cols = [c["name"] for c in inspector.get_columns(t)]
                print(f"  [NEW TABLE] {t} ({len(cols)} columns: {', '.join(cols)})")
        else:
            print("  [OK] No new tables needed.")

        new_cols_found = False
        for t in sorted(tables_before):
            if t in tables_after:
                current_cols = set(c["name"] for c in inspector.get_columns(t))
                added = current_cols - cols_before.get(t, set())
                if added:
                    new_cols_found = True
                    print(f"  [NEW COLUMNS] {t}: {', '.join(sorted(added))}")
        if not new_cols_found:
            print("  [OK] No new columns needed.")


def check_admin(app, db):
    """Ensure at least one admin user exists."""
    from database.models import User

    with app.app_context():
        admins = User.query.filter_by(is_admin=True).all()
        all_users = User.query.all()

        if not all_users:
            print("  [INFO] No users in database yet — admin will be set after first registration.")
            return

        if admins:
            for a in admins:
                print(f"  [OK] Admin: {a.id}")
            return

        # No admin found — prompt to set one
        print("  [WARN] No admin user found!")
        print("  Registered users:")
        for i, u in enumerate(all_users, 1):
            print(f"    {i}. {u.id}")

        while True:
            choice = input("\n  Enter the number of the user to make admin (or 'skip' to skip): ").strip()
            if choice.lower() == "skip":
                print("  [SKIP] No admin set. You can set one later via this script.")
                return
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(all_users):
                    target = all_users[idx]
                    target.is_admin = True
                    db.session.commit()
                    print(f"  [OK] {target.id} is now admin.")
                    return
                else:
                    print("  Invalid number, try again.")
            except ValueError:
                print("  Invalid input, try again.")


def set_admin_direct(email):
    """Directly promote a user to admin by email."""
    from app import create_app
    from database import db
    from database.models import User

    app = create_app()
    with app.app_context():
        user = db.session.get(User, email)
        if not user:
            print(f"  [ERROR] No user found with email: {email}")
            print("  Registered users:")
            for u in User.query.all():
                admin_tag = " (admin)" if u.is_admin else ""
                print(f"    - {u.id}{admin_tag}")
            sys.exit(1)

        if user.is_admin:
            print(f"  [OK] {email} is already admin.")
            return

        user.is_admin = True
        db.session.commit()
        print(f"  [OK] {email} is now admin.")


def main():
    # Handle --set-admin shortcut
    if len(sys.argv) >= 3 and sys.argv[1] == "--set-admin":
        email = sys.argv[2].strip()
        print(f"\nSetting admin: {email}")
        set_admin_direct(email)
        return

    print("=" * 56)
    print("  SimCricketX Migration Script")
    print("=" * 56)

    # Step 1: Backup
    print("\n[1/3] Backing up database...")
    had_db = backup_database()

    # Step 2: Schema migration
    print("\n[2/3] Running schema migration...")
    # Import app factory here so we don't fail on import errors before backup
    from app import create_app
    from database import db

    app = create_app()
    if had_db:
        run_schema_migration(app, db)
    else:
        with app.app_context():
            db.create_all()
        print("  [OK] Fresh database created with all tables.")

    # Step 3: Admin check
    print("\n[3/3] Checking admin user...")
    check_admin(app, db)

    print("\n" + "=" * 56)
    print("  Migration complete. You can now start the server.")
    print("=" * 56)


if __name__ == "__main__":
    main()
