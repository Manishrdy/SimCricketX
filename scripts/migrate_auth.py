
import sys
import os
import json
import logging
from werkzeug.security import generate_password_hash
from sqlalchemy.exc import IntegrityError

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, db
from database.models import User
from auth.user_auth import decrypt_password, load_credentials, CREDENTIALS_FILE

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def migrate_users():
    app = create_app()
    
    with app.app_context():
        # 1. Update Schema (create new columns if missing)
        # Check if columns exist, if not, add them via raw SQL (SQLite support)
        from sqlalchemy import text, inspect
        inspector = inspect(db.engine)
        existing_columns = [c['name'] for c in inspector.get_columns('users')]
        
        with db.engine.connect() as conn:
            if 'last_login' not in existing_columns:
                logger.info("Adding column last_login to users")
                conn.execute(text("ALTER TABLE users ADD COLUMN last_login DATETIME"))
            if 'ip_address' not in existing_columns:
                logger.info("Adding column ip_address to users")
                conn.execute(text("ALTER TABLE users ADD COLUMN ip_address VARCHAR(50)"))
            if 'mac_address' not in existing_columns:
                logger.info("Adding column mac_address to users")
                conn.execute(text("ALTER TABLE users ADD COLUMN mac_address VARCHAR(50)"))
            if 'hostname' not in existing_columns:
                logger.info("Adding column hostname to users")
                conn.execute(text("ALTER TABLE users ADD COLUMN hostname VARCHAR(100)"))
            if 'display_name' not in existing_columns:
                logger.info("Adding column display_name to users")
                conn.execute(text("ALTER TABLE users ADD COLUMN display_name VARCHAR(100)"))
            conn.commit()

        logger.info("Checking for users in credentials.json...")
        
        if not os.path.exists(CREDENTIALS_FILE):
            logger.warning(f"No credentials file found at {CREDENTIALS_FILE}")
            return

        try:
            creds = load_credentials()
        except Exception as e:
            logger.error(f"Failed to load credentials: {e}")
            return

        migrated_count = 0
        skipped_count = 0
        
        for email, data in creds.items():
            user_id = email.lower().strip() # Use email as ID
            
            # Check if user already exists
            existing_user = db.session.get(User, user_id)
            if existing_user:
                logger.info(f"User {user_id} already in DB. Skipping.")
                skipped_count += 1
                continue
                
            try:
                # Decrypt old password
                encrypted_pwd = data.get("encrypted_password")
                if not encrypted_pwd:
                    logger.warning(f"Skipping {email}: No password found")
                    continue
                    
                plaintext_pwd = decrypt_password(encrypted_pwd)
                
                # Hash for DB
                password_hash = generate_password_hash(plaintext_pwd)
                
                # Create User
                new_user = User(
                    id=user_id,
                    password_hash=password_hash,
                    ip_address=data.get("ip_address"),
                    mac_address=data.get("mac_address"),
                    hostname=data.get("hostname"),
                    last_login=None # We don't have this in old JSON usually, or it's "login_time"
                )
                
                # Try to parse login_time if exists
                if "login_time" in data:
                    try:
                        from datetime import datetime
                        # ISO format
                        new_user.last_login = datetime.fromisoformat(data["login_time"])
                    except:
                        pass

                db.session.add(new_user)
                migrated_count += 1
                logger.info(f"Prepared migration for: {user_id}")
                
            except Exception as e:
                logger.error(f"Error migrating {email}: {e}")
        
        if migrated_count > 0:
            try:
                db.session.commit()
                logger.info(f"Successfully migrated {migrated_count} users.")
                
                # Rename credentials file
                # backup_path = CREDENTIALS_FILE + ".migrated"
                # os.rename(CREDENTIALS_FILE, backup_path)
                # logger.info(f"Renamed credentials.json to {backup_path}")
                
            except Exception as e:
                db.session.rollback()
                logger.error(f"Database commit failed: {e}")
        else:
            logger.info("No new users to migrate.")

if __name__ == "__main__":
    print("Starting Auth Migration...")
    migrate_users()
    print("Migration Check Complete.")
