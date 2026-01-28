
import sys
import os
import logging
from werkzeug.security import generate_password_hash

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, db
from database.models import User
from auth.user_auth import verify_user, register_user

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_auth():
    app = create_app()
    with app.app_context():
        # Test Case 1: Helper check for migrated user
        migrated_user = "admin@projectx.com"
        logger.info(f"Checking migrated user: {migrated_user}")
        user = db.session.get(User, migrated_user)
        if user:
            logger.info(f"✅ User {migrated_user} found in DB.")
            if user.password_hash:
                logger.info(f"✅ Password hash present: {user.password_hash[:10]}...")
            else:
                logger.error("❌ Password hash missing!")
        else:
            logger.error(f"❌ User {migrated_user} not found in DB!")

        # Test Case 2: Register new user
        new_user_email = "newuser@example.com"
        new_user_pass = "TestPass123"
        logger.info(f"Testing registration for {new_user_email}")
        
        # Cleanup first
        existing = db.session.get(User, new_user_email)
        if existing:
            db.session.delete(existing)
            db.session.commit()
            
        success = register_user(new_user_email, new_user_pass)
        if success:
            logger.info("✅ Registration successful")
        else:
            logger.error("❌ Registration failed")
            
        # Test Case 3: Verify Login
        logger.info("Testing login...")
        if verify_user(new_user_email, new_user_pass):
            logger.info("✅ Login successful")
        else:
            logger.error("❌ Login failed")
            
        # Test Case 4: Verify Login with wrong pass
        if not verify_user(new_user_email, "WrongPass"):
            logger.info("✅ Blocked invalid password")
        else:
            logger.error("❌ Allowed invalid password")

if __name__ == "__main__":
    test_auth()
