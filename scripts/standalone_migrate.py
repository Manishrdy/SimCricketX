
import sys
import os
import json
import logging
import platform
import socket
from datetime import datetime
from cryptography.fernet import Fernet # Requires cryptography package
from werkzeug.security import generate_password_hash
from sqlalchemy.exc import IntegrityError

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, db
from database.models import User

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- LEGACY CONSTANTS ---
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AUTH_DIR = os.path.join(PROJECT_ROOT, "auth")
CREDENTIALS_FILE = os.path.join(AUTH_DIR, "credentials.json")
KEY_FILE = os.path.join(AUTH_DIR, "encryption.key")

# --- LEGACY HELPERS (Embedded to avoid import errors) ---

def load_key():
    if not os.path.exists(KEY_FILE):
        return None
    try:
        with open(KEY_FILE, 'rb') as f:
            return f.read()
    except Exception:
        return None

def decrypt_password(encrypted: str) -> str:
    if not encrypted:
        return ""
    try:
        key = load_key()
        if not key:
            raise ValueError("Encryption key not found")
        f = Fernet(key)
        return f.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        return None

def load_legacy_credentials() -> dict:
    if not os.path.exists(CREDENTIALS_FILE):
        return {}
    try:
        with open(CREDENTIALS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read credentials.json: {e}")
        return {}

# --- MIGRATION LOGIC ---

def run_migration():
    app = create_app()
    with app.app_context():
        # Ensure schema
        # (This relies on models.py being up to date, which it is)
        db.create_all()
        
        logger.info(f"Reading legacy credentials from: {CREDENTIALS_FILE}")
        creds = load_legacy_credentials()
        
        if not creds:
            logger.warning("No credentials found or file is empty.")
            return

        success_count = 0
        skip_count = 0
        
        for email, data in creds.items():
            user_id = email.lower().strip()
            
            # Check if user exists
            if db.session.get(User, user_id):
                logger.info(f"Skipping {user_id} (already in DB)")
                skip_count += 1
                continue
                
            # Decrypt password
            enc_pass = data.get("encrypted_password")
            if not enc_pass:
                logger.warning(f"Skipping {user_id}: No password found")
                continue
                
            plain_pass = decrypt_password(enc_pass)
            if not plain_pass:
                logger.warning(f"Skipping {user_id}: Could not decrypt password")
                continue
                
            # Create User
            try:
                new_user = User(
                    id=user_id,
                    password_hash=generate_password_hash(plain_pass),
                    ip_address=data.get("ip_address"),
                    mac_address=data.get("mac_address"),
                    hostname=data.get("hostname"),
                    # Try to parse login_time
                    last_login=None
                )
                
                if data.get("login_time"):
                    try:
                        new_user.last_login = datetime.fromisoformat(data.get("login_time"))
                    except:
                        pass
                        
                db.session.add(new_user)
                try:
                    db.session.commit()
                    success_count += 1
                    logger.info(f"✅ Migrated: {user_id}")
                except IntegrityError:
                    db.session.rollback()
                    logger.warning(f"Failed to commit {user_id} (IntegrityError)")
                except Exception as e:
                    db.session.rollback()
                    logger.error(f"Failed to commit {user_id}: {e}")
                    
            except Exception as e:
                logger.error(f"Error processing {user_id}: {e}")
                
        logger.info("-" * 30)
        logger.info(f"Migration Complete.")
        logger.info(f"New Users Migrated: {success_count}")
        logger.info(f"Skipped (Already Existed): {skip_count}")

if __name__ == "__main__":
    run_migration()
