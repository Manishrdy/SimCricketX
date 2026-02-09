#!/usr/bin/env python
"""
Migration Script: Add is_admin flag and admin_audit_log table
Sets the first registered user (or admin@projectx.com) as admin if no admin exists.
"""

import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database import db
from database.models import User
from sqlalchemy import inspect, text
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def column_exists(table_name, column_name):
    """Check if a column exists in a table"""
    inspector = inspect(db.engine)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns

def table_exists(table_name):
    """Check if a table exists"""
    inspector = inspect(db.engine)
    return table_name in inspector.get_table_names()

def add_admin_flag_migration():
    """Add is_admin column to users table and ensure one admin exists"""
    try:
        # --- is_admin column ---
        if column_exists('users', 'is_admin'):
            logger.info("Column 'is_admin' already exists in users table")
        else:
            logger.info("Adding 'is_admin' column to users table...")
            with db.engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"
                ))
                conn.commit()
            logger.info("Added 'is_admin' column to users table")

            # Create index
            with db.engine.connect() as conn:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_users_is_admin ON users (is_admin)"
                ))
                conn.commit()

        # --- admin_audit_log table ---
        if table_exists('admin_audit_log'):
            logger.info("Table 'admin_audit_log' already exists")
        else:
            logger.info("Creating 'admin_audit_log' table...")
            with db.engine.connect() as conn:
                conn.execute(text("""
                    CREATE TABLE admin_audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        admin_email VARCHAR(120) NOT NULL,
                        action VARCHAR(50) NOT NULL,
                        target VARCHAR(200),
                        details TEXT,
                        ip_address VARCHAR(50),
                        timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_timestamp ON admin_audit_log (timestamp)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_admin ON admin_audit_log (admin_email)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_admin_action ON admin_audit_log (admin_email, action)"))
                conn.commit()
            logger.info("Created 'admin_audit_log' table")

        # --- Ensure at least one admin exists ---
        admin_count = db.session.query(User).filter_by(is_admin=True).count()
        if admin_count == 0:
            # Try admin@projectx.com first, then first user
            admin_user = db.session.get(User, 'admin@projectx.com')
            if not admin_user:
                admin_user = db.session.query(User).order_by(User.created_at.asc()).first()

            if admin_user:
                admin_user.is_admin = True
                db.session.commit()
                logger.info(f"Set {admin_user.id} as admin")
            else:
                logger.warning("No users found - admin will be set on first registration")
        else:
            admins = db.session.query(User).filter_by(is_admin=True).all()
            logger.info(f"Admin users: {[a.id for a in admins]}")

        # Migration summary
        total_users = db.session.query(User).count()
        admin_count = db.session.query(User).filter_by(is_admin=True).count()
        logger.info(f"Total users: {total_users}, Admins: {admin_count}")
        logger.info("Migration completed successfully!")
        return True

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        db.session.rollback()
        return False

if __name__ == '__main__':
    from app import create_app

    app = create_app()
    with app.app_context():
        success = add_admin_flag_migration()
        sys.exit(0 if success else 1)
