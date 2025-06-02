import os
import json
import uuid
import socket
import platform
import logging
import traceback
import sys
from datetime import datetime
import uuid as uuid_lib
from cryptography.fernet import Fernet
from utils.helpers import load_config
import gspread
from google.oauth2.service_account import Credentials
import yaml

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Avoid adding multiple handlers if already configured
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Log module initialization
logger.info("Initializing user_auth module")
logger.debug(f"Python version: {sys.version}")
logger.debug(f"Platform: {platform.platform()}")
logger.debug(f"Current working directory: {os.getcwd()}")

print(f"[DEBUG] Checking existence of /app/auth/credentials.json: {os.path.exists('/app/auth/credentials.json')}")
logger.debug(f"Checking existence of /app/auth/credentials.json: {os.path.exists('/app/auth/credentials.json')}")
print(f"[DEBUG] Checking existence of /app/auth/encryption.key: {os.path.exists('/app/auth/encryption.key')}")
logger.debug(f"Checking existence of /app/auth/encryption.key: {os.path.exists('/app/auth/encryption.key')}")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
logger.debug(f"PROJECT_ROOT determined as: {PROJECT_ROOT}")
AUTH_DIR = os.path.join(PROJECT_ROOT, "auth")
logger.debug(f"AUTH_DIR determined as: {AUTH_DIR}")

CREDENTIALS_FILE = os.path.join(AUTH_DIR, "credentials.json")
logger.debug(f"CREDENTIALS_FILE path: {CREDENTIALS_FILE}")
KEY_FILE = os.path.join(AUTH_DIR, "encryption.key")
logger.debug(f"KEY_FILE path: {KEY_FILE}")

# Ensure auth directory exists
if not os.path.exists(AUTH_DIR):
    logger.warning(f"AUTH_DIR does not exist, creating: {AUTH_DIR}")
    try:
        os.makedirs(AUTH_DIR, exist_ok=True)
        logger.info(f"Successfully created AUTH_DIR: {AUTH_DIR}")
    except Exception as e:
        logger.error(f"Failed to create AUTH_DIR: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
else:
    logger.debug(f"AUTH_DIR exists: {AUTH_DIR}")

# Step 1: Generate a key (only once)
def generate_key():
    logger.info("Entering generate_key function")
    logger.debug(f"Generating new encryption key to: {KEY_FILE}")
    
    try:
        key = Fernet.generate_key()
        logger.debug(f"Generated key length: {len(key)} bytes")
        
        logger.debug(f"Writing key to file: {KEY_FILE}")
        with open(KEY_FILE, 'wb') as f:
            f.write(key)
        
        logger.info(f"Successfully generated and saved encryption key to: {KEY_FILE}")
        
        # Verify file was written correctly
        if os.path.exists(KEY_FILE):
            file_size = os.path.getsize(KEY_FILE)
            logger.debug(f"Key file size after write: {file_size} bytes")
        else:
            logger.error(f"Key file does not exist after write operation: {KEY_FILE}")
            
    except Exception as e:
        logger.error(f"Error in generate_key: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
    
    logger.info("Exiting generate_key function")

# Step 2: Load the key
def load_key():
    logger.info("Entering load_key function")
    logger.debug(f"Checking if key file exists: {KEY_FILE}")
    
    if not os.path.exists(KEY_FILE):
        logger.warning(f"Key file does not exist, generating new key: {KEY_FILE}")
        generate_key()
        logger.debug("Key generation completed, proceeding to load")
    else:
        logger.debug(f"Key file exists: {KEY_FILE}")
        file_size = os.path.getsize(KEY_FILE)
        logger.debug(f"Key file size: {file_size} bytes")
    
    try:
        logger.debug(f"Reading key from file: {KEY_FILE}")
        with open(KEY_FILE, 'rb') as f:
            key = f.read()
        
        logger.debug(f"Successfully read key, length: {len(key)} bytes")
        
        # Validate key format
        try:
            Fernet(key)  # Test if key is valid
            logger.debug("Key validation successful")
        except Exception as validation_error:
            logger.error(f"Invalid key format: {validation_error}")
            raise
            
        logger.info("Successfully loaded encryption key")
        return key
        
    except Exception as e:
        logger.error(f"Error in load_key: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
    
    logger.info("Exiting load_key function")

# Step 3: Encrypt password
def encrypt_password(password: str) -> str:
    logger.info("Entering encrypt_password function")
    logger.debug(f"Password length to encrypt: {len(password)} characters")
    
    if not password:
        logger.warning("Empty password provided for encryption")
    
    if len(password) > 1000:
        logger.warning(f"Unusually long password provided: {len(password)} characters")
    
    try:
        logger.debug("Loading encryption key")
        key = load_key()
        logger.debug("Encryption key loaded successfully")
        
        logger.debug("Initializing Fernet cipher")
        f = Fernet(key)
        
        logger.debug("Encoding password to bytes")
        password_bytes = password.encode()
        logger.debug(f"Password encoded, byte length: {len(password_bytes)}")
        
        logger.debug("Encrypting password")
        encrypted_bytes = f.encrypt(password_bytes)
        logger.debug(f"Password encrypted, encrypted byte length: {len(encrypted_bytes)}")
        
        logger.debug("Decoding encrypted bytes to string")
        encrypted_string = encrypted_bytes.decode()
        logger.debug(f"Encrypted string length: {len(encrypted_string)}")
        
        logger.info("Password encryption completed successfully")
        return encrypted_string
        
    except Exception as e:
        logger.error(f"Error in encrypt_password: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
    
    logger.info("Exiting encrypt_password function")

# Step 4: Decrypt password
def decrypt_password(encrypted: str) -> str:
    logger.info("Entering decrypt_password function")
    logger.debug(f"Encrypted string length to decrypt: {len(encrypted)} characters")
    
    if not encrypted:
        logger.warning("Empty encrypted string provided for decryption")
        raise ValueError("Empty encrypted string provided")
    
    try:
        logger.debug("Loading encryption key")
        key = load_key()
        logger.debug("Encryption key loaded successfully")
        
        logger.debug("Initializing Fernet cipher")
        f = Fernet(key)
        
        logger.debug("Encoding encrypted string to bytes")
        encrypted_bytes = encrypted.encode()
        logger.debug(f"Encrypted bytes length: {len(encrypted_bytes)}")
        
        logger.debug("Decrypting password")
        decrypted_bytes = f.decrypt(encrypted_bytes)
        logger.debug(f"Decrypted bytes length: {len(decrypted_bytes)}")
        
        logger.debug("Decoding decrypted bytes to string")
        decrypted_string = decrypted_bytes.decode()
        logger.debug(f"Decrypted password length: {len(decrypted_string)} characters")
        
        logger.info("Password decryption completed successfully")
        return decrypted_string
        
    except Exception as e:
        logger.error(f"Error in decrypt_password: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
    
    logger.info("Exiting decrypt_password function")

# Utility to read credentials file
def load_credentials() -> dict:
    logger.info("Entering load_credentials function")
    logger.debug(f"Checking if credentials file exists: {CREDENTIALS_FILE}")
    
    if not os.path.exists(CREDENTIALS_FILE):
        logger.warning(f"Credentials file does not exist, creating empty file: {CREDENTIALS_FILE}")
        try:
            with open(CREDENTIALS_FILE, 'w') as f:
                json.dump({}, f)
            logger.info(f"Created empty credentials file: {CREDENTIALS_FILE}")
        except Exception as e:
            logger.error(f"Failed to create credentials file: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
    else:
        logger.debug(f"Credentials file exists: {CREDENTIALS_FILE}")
        file_size = os.path.getsize(CREDENTIALS_FILE)
        logger.debug(f"Credentials file size: {file_size} bytes")

    try:
        logger.debug(f"Reading credentials from file: {CREDENTIALS_FILE}")
        with open(CREDENTIALS_FILE, 'r') as f:
            credentials = json.load(f)
        
        logger.debug(f"Successfully loaded credentials, user count: {len(credentials)}")
        logger.debug(f"Credential keys: {list(credentials.keys())}")
        
        # Validate credentials structure
        for email, user_data in credentials.items():
            logger.debug(f"Validating user data for: {email}")
            if not isinstance(user_data, dict):
                logger.warning(f"Invalid user data structure for {email}: {type(user_data)}")
            else:
                required_fields = ["user_id", "encrypted_password"]
                missing_fields = [field for field in required_fields if field not in user_data]
                if missing_fields:
                    logger.warning(f"Missing required fields for {email}: {missing_fields}")
                logger.debug(f"User data fields for {email}: {list(user_data.keys())}")
        
        logger.info("Credentials loaded and validated successfully")
        return credentials
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in credentials file: {e}")
        print("[!] credentials.json is empty or corrupted. Reinitializing.")
        logger.warning("Reinitializing corrupted credentials file")
        
        # Backup corrupted file
        backup_file = f"{CREDENTIALS_FILE}.corrupted.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            if os.path.exists(CREDENTIALS_FILE):
                os.rename(CREDENTIALS_FILE, backup_file)
                logger.info(f"Backed up corrupted file to: {backup_file}")
        except Exception as backup_error:
            logger.error(f"Failed to backup corrupted file: {backup_error}")
        
        try:
            with open(CREDENTIALS_FILE, 'w') as f:
                json.dump({}, f)
            logger.info("Created new empty credentials file")
            return {}
        except Exception as recreate_error:
            logger.error(f"Failed to recreate credentials file: {recreate_error}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
            
    except Exception as e:
        logger.error(f"Unexpected error in load_credentials: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise
    
    logger.info("Exiting load_credentials function")

# Utility to write credentials
def save_credentials(data: dict):
    logger.info("Entering save_credentials function")
    logger.debug(f"Saving credentials for {len(data)} users")
    logger.debug(f"User emails: {list(data.keys())}")
    
    # Validate data before saving
    if not isinstance(data, dict):
        logger.error(f"Invalid data type for credentials: {type(data)}")
        raise TypeError("Credentials data must be a dictionary")
    
    # Backup existing credentials before overwriting
    backup_created = False
    if os.path.exists(CREDENTIALS_FILE):
        backup_file = f"{CREDENTIALS_FILE}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            import shutil
            shutil.copy2(CREDENTIALS_FILE, backup_file)
            logger.debug(f"Created backup: {backup_file}")
            backup_created = True
        except Exception as backup_error:
            logger.warning(f"Failed to create backup: {backup_error}")
    
    try:
        logger.debug(f"Writing credentials to file: {CREDENTIALS_FILE}")
        with open(CREDENTIALS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        
        logger.debug("Credentials written successfully")
        
        # Verify write operation
        if os.path.exists(CREDENTIALS_FILE):
            file_size = os.path.getsize(CREDENTIALS_FILE)
            logger.debug(f"Credentials file size after write: {file_size} bytes")
            
            # Verify content integrity
            try:
                with open(CREDENTIALS_FILE, 'r') as f:
                    verification_data = json.load(f)
                if len(verification_data) == len(data):
                    logger.debug("Write verification successful")
                else:
                    logger.error(f"Write verification failed: expected {len(data)} users, got {len(verification_data)}")
            except Exception as verify_error:
                logger.error(f"Failed to verify written data: {verify_error}")
        else:
            logger.error(f"Credentials file does not exist after write: {CREDENTIALS_FILE}")
        
        logger.info("Credentials saved successfully")
        
    except Exception as e:
        logger.error(f"Error in save_credentials: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        # Restore backup if available
        if backup_created:
            try:
                import shutil
                shutil.copy2(backup_file, CREDENTIALS_FILE)
                logger.info(f"Restored backup after save failure: {backup_file}")
            except Exception as restore_error:
                logger.error(f"Failed to restore backup: {restore_error}")
        
        raise
    
    logger.info("Exiting save_credentials function")

def get_ip_address() -> str:
    logger.info("Entering get_ip_address function")
    
    try:
        logger.debug("Getting hostname")
        hostname = socket.gethostname()
        logger.debug(f"Hostname: {hostname}")
        
        logger.debug("Resolving IP address from hostname")
        ip_address = socket.gethostbyname(hostname)
        logger.debug(f"Resolved IP address: {ip_address}")
        
        # Additional validation
        if ip_address.startswith("127."):
            logger.warning(f"Localhost IP detected: {ip_address}")
        
        logger.info(f"Successfully retrieved IP address: {ip_address}")
        return ip_address
        
    except Exception as e:
        logger.error(f"Error getting IP address: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        logger.warning("Returning empty string due to IP address resolution failure")
        return ""
    
    logger.info("Exiting get_ip_address function")

def get_mac_address() -> str:
    logger.info("Entering get_mac_address function")
    
    try:
        logger.debug("Getting MAC address from uuid.getnode()")
        mac = uuid_lib.getnode()
        logger.debug(f"Raw MAC value: {mac} (hex: 0x{mac:012x})")
        
        # Check if MAC is locally administered
        if (mac >> 40) % 2:
            logger.warning("MAC address is locally administered or invalid")
            return ""  # MAC is locally administered or invalid
        
        logger.debug("Converting MAC to standard format")
        mac_address = ':'.join(f'{(mac >> ele) & 0xff:02x}' for ele in range(40, -1, -8))
        logger.debug(f"Formatted MAC address: {mac_address}")
        
        # Validate MAC format
        if len(mac_address) == 17 and mac_address.count(':') == 5:
            logger.info(f"Successfully retrieved MAC address: {mac_address}")
            return mac_address
        else:
            logger.warning(f"Invalid MAC address format: {mac_address}")
            return ""
            
    except Exception as e:
        logger.error(f"Error getting MAC address: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        logger.warning("Returning empty string due to MAC address retrieval failure")
        return ""
    
    logger.info("Exiting get_mac_address function")

def delete_user(email: str) -> bool:
    """
    Deletes a user from the credentials file.
    Returns True if deleted, False if user doesn't exist.
    """
    logger.info("Entering delete_user function")
    logger.debug(f"Attempting to delete user: {email}")
    
    # Input validation
    if not email or not isinstance(email, str):
        logger.error(f"Invalid email provided: {email}")
        return False
    
    if '@' not in email:
        logger.warning(f"Email format appears invalid: {email}")
    
    try:
        logger.debug("Loading current credentials")
        creds = load_credentials()
        logger.debug(f"Current user count: {len(creds)}")

        if email not in creds:
            logger.warning(f"User not found in credentials: {email}")
            print(f"❌ User not found: {email}")
            return False

        logger.debug(f"User found, proceeding with deletion: {email}")
        user_data = creds[email]
        logger.debug(f"User data to be deleted: {list(user_data.keys())}")
        
        del creds[email]
        logger.debug(f"User removed from memory, new user count: {len(creds)}")
        
        logger.debug("Saving updated credentials")
        save_credentials(creds)
        logger.info(f"User deleted from local credentials: {email}")
        
        logger.debug("Deleting user from Google Sheets")
        sheets_result = delete_user_from_google_sheets(email)
        logger.debug(f"Google Sheets deletion result: {sheets_result}")
        
        print(f"🗑️  User deleted successfully: {email}")
        logger.info(f"User deletion completed successfully: {email}")
        return True
        
    except Exception as e:
        logger.error(f"Error in delete_user for {email}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False
    
    logger.info("Exiting delete_user function")

def register_user(email: str, password: str) -> bool:
    logger.info("Entering register_user function")
    logger.debug(f"Attempting to register user: {email}")
    
    # Input validation
    if not email or not isinstance(email, str):
        logger.error(f"Invalid email provided: {email}")
        return False
    
    if not password or not isinstance(password, str):
        logger.error(f"Invalid password provided for user: {email}")
        return False
    
    if '@' not in email:
        logger.warning(f"Email format appears invalid: {email}")
    
    if len(password) < 1:
        logger.warning(f"Very short password provided for user: {email}")
    
    logger.debug(f"Password length: {len(password)} characters")
    
    try:
        logger.debug("Loading current credentials")
        creds = load_credentials()
        logger.debug(f"Current user count: {len(creds)}")

        if email in creds:
            logger.warning(f"User already exists: {email}")
            print(f"[!] User already exists: {email}")
            return False

        logger.info(f"User not found, proceeding with registration: {email}")
        
        logger.debug("Encrypting password")
        encrypted = encrypt_password(password)
        logger.debug(f"Password encrypted successfully, length: {len(encrypted)}")

        logger.debug("Gathering system information")
        ip_address = get_ip_address()
        logger.debug(f"IP address: {ip_address}")
        
        mac_address = get_mac_address()
        logger.debug(f"MAC address: {mac_address}")
        
        hostname = socket.gethostname()
        logger.debug(f"Hostname: {hostname}")
        
        os_system = platform.system()
        logger.debug(f"OS: {os_system}")
        
        os_version = platform.version()
        logger.debug(f"OS version: {os_version}")
        
        machine = platform.machine()
        logger.debug(f"Machine: {machine}")
        
        processor = platform.processor()
        logger.debug(f"Processor: {processor}")
        
        login_time = datetime.now().isoformat()
        logger.debug(f"Login time: {login_time}")
        
        user_id = str(uuid_lib.uuid4())
        logger.debug(f"Generated user ID: {user_id}")

        user_data = {
            "user_id": user_id,
            "encrypted_password": encrypted,
            "decrypted_password": password,  # 👈 Not safe for production
            "ip_address": ip_address,
            "mac_address": mac_address,
            "hostname": hostname,
            "os": os_system,
            "os_version": os_version,
            "machine": machine,
            "processor": processor,
            "login_time": login_time
        }
        
        logger.debug(f"User data structure created with {len(user_data)} fields")
        logger.warning("SECURITY WARNING: Storing plaintext password in user data (not safe for production)")

        creds[email] = user_data
        logger.debug(f"User added to credentials, new user count: {len(creds)}")
        
        logger.debug("Saving credentials to file")
        save_credentials(creds)
        logger.info(f"User saved to local credentials: {email}")

        logger.debug("Writing user to Google Sheets")
        write_user_to_google_sheets(email, password, user_data)
        logger.info(f"User data written to Google Sheets: {email}")

        print(f"[+] User registered successfully: {email}")
        logger.info(f"User registration completed successfully: {email}")
        return True
        
    except Exception as e:
        logger.error(f"Error in register_user for {email}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False
    
    logger.info("Exiting register_user function")

def load_google_config():
    logger.info("Entering load_google_config function")
    
    try:
        logger.debug("Loading application configuration")
        config = load_config()
        logger.debug(f"Configuration loaded, top-level keys: {list(config.keys())}")
        
        logger.debug("Extracting Google Sheets configuration")
        google = config.get("google_sheets", {})
        logger.debug(f"Google Sheets config keys: {list(google.keys())}")

        if not google:
            logger.error("Missing 'google_sheets' block in config.yaml")
            raise ValueError("Missing 'google_sheets' block in config.yaml.")

        logger.debug("Resolving credentials file path")
        # Resolve full path to credentials file
        rel_creds = google.get("credentials_file", "")
        logger.debug(f"Relative credentials path: {rel_creds}")
        
        if not rel_creds:
            logger.error("Missing 'credentials_file' in Google Sheets config")
            raise ValueError("Missing 'credentials_file' in Google Sheets config")
        
        abs_creds = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), rel_creds)
        logger.debug(f"Absolute credentials path: {abs_creds}")
        
        # Verify credentials file exists
        if not os.path.exists(abs_creds):
            logger.error(f"Google Sheets credentials file not found: {abs_creds}")
            raise FileNotFoundError(f"Google Sheets credentials file not found: {abs_creds}")
        
        file_size = os.path.getsize(abs_creds)
        logger.debug(f"Credentials file size: {file_size} bytes")
        
        google["credentials_file"] = abs_creds
        
        # Validate other required config fields
        spreadsheet_id = google.get("spreadsheet_id", "")
        if not spreadsheet_id:
            logger.error("Missing 'spreadsheet_id' in Google Sheets config")
            raise ValueError("Missing 'spreadsheet_id' in Google Sheets config")
        
        logger.debug(f"Spreadsheet ID: {spreadsheet_id}")
        logger.info("Google Sheets configuration loaded successfully")
        return google

    except Exception as e:
        logger.error(f"Error loading Google Sheets config: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        print(f"[!] Error loading Google Sheets config: {e}")
        return {}
    
    logger.info("Exiting load_google_config function")

def write_user_to_google_sheets(email: str, password: str, user_data: dict):
    """
    Appends user data to Google Sheets for central storage.
    """
    logger.info("Entering write_user_to_google_sheets function")
    logger.debug(f"Writing user to Google Sheets: {email}")
    
    # Input validation
    if not email or not isinstance(email, str):
        logger.error(f"Invalid email for Google Sheets write: {email}")
        raise ValueError("Invalid email provided")
    
    if not password or not isinstance(password, str):
        logger.error(f"Invalid password for Google Sheets write: {email}")
        raise ValueError("Invalid password provided")
    
    if not user_data or not isinstance(user_data, dict):
        logger.error(f"Invalid user_data for Google Sheets write: {email}")
        raise ValueError("Invalid user_data provided")
    
    logger.warning("SECURITY WARNING: Writing plaintext password to Google Sheets")
    
    try:
        logger.debug("Loading Google Sheets configuration")
        config = load_google_config()
        
        if not config:
            logger.error("Failed to load Google Sheets configuration")
            raise ValueError("Google Sheets configuration not available")
        
        creds_file = config["credentials_file"]
        sheet_id = config["spreadsheet_id"]
        
        logger.debug(f"Using credentials file: {creds_file}")
        logger.debug(f"Using spreadsheet ID: {sheet_id}")

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        logger.debug(f"Using scopes: {scopes}")
        
        logger.debug("Creating Google Sheets credentials")
        credentials = Credentials.from_service_account_file(creds_file, scopes=scopes)
        logger.debug("Credentials created successfully")
        
        logger.debug("Authorizing Google Sheets client")
        client = gspread.authorize(credentials)
        logger.debug("Client authorized successfully")

        logger.debug(f"Opening spreadsheet: {sheet_id}")
        sheet = client.open_by_key(sheet_id).sheet1  # Use the first worksheet
        logger.debug("Spreadsheet opened successfully")
        
        # Log current sheet information
        try:
            sheet_title = sheet.title
            row_count = sheet.row_count
            col_count = sheet.col_count
            logger.debug(f"Sheet info - Title: {sheet_title}, Rows: {row_count}, Cols: {col_count}")
        except Exception as info_error:
            logger.warning(f"Failed to get sheet info: {info_error}")
        
        logger.debug("Preparing row data")
        row = [
            email,
            user_data["user_id"],
            password,
            user_data["ip_address"],
            user_data["mac_address"],
            user_data["hostname"],
            user_data["os"],
            user_data["os_version"],
            user_data["machine"],
            user_data["processor"],
            user_data["login_time"]
        ]
        
        logger.debug(f"Row data prepared with {len(row)} columns")
        logger.debug(f"Row data: {[str(item)[:50] + '...' if len(str(item)) > 50 else str(item) for item in row]}")
        
        logger.debug("Appending row to Google Sheets")
        result = sheet.append_row(row)
        logger.debug(f"Append result: {result}")
        
        print("📝 User data written to Google Sheets.")
        logger.info(f"User data successfully written to Google Sheets: {email}")
        
    except Exception as e:
        logger.error(f"Failed to write to Google Sheets for {email}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        print(f"[!] Failed to write to Google Sheets: {e}")
        raise
    
    logger.info("Exiting write_user_to_google_sheets function")

def verify_user_from_google_sheets(email: str, password: str) -> bool:
    logger.info("Entering verify_user_from_google_sheets function")
    logger.debug(f"Verifying user from Google Sheets: {email}")
    
    # Input validation
    if not email or not isinstance(email, str):
        logger.error(f"Invalid email for Google Sheets verification: {email}")
        return False
    
    if not password or not isinstance(password, str):
        logger.error(f"Invalid password for Google Sheets verification: {email}")
        return False
    
    try:
        logger.debug("Loading Google Sheets configuration")
        config = load_google_config()
        
        if not config:
            logger.error("Failed to load Google Sheets configuration")
            return False
        
        creds_file = config["credentials_file"]
        sheet_id = config["spreadsheet_id"]
        
        logger.debug(f"Using credentials file: {creds_file}")
        logger.debug(f"Using spreadsheet ID: {sheet_id}")

        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        logger.debug(f"Using read-only scopes: {scopes}")
        
        logger.debug("Creating Google Sheets credentials")
        credentials = Credentials.from_service_account_file(creds_file, scopes=scopes)
        logger.debug("Credentials created successfully")
        
        logger.debug("Authorizing Google Sheets client")
        client = gspread.authorize(credentials)
        logger.debug("Client authorized successfully")

        logger.debug(f"Opening spreadsheet: {sheet_id}")
        sheet = client.open_by_key(sheet_id).sheet1  # use first sheet
        logger.debug("Spreadsheet opened successfully")
        
        logger.debug("Retrieving all records from sheet")
        data = sheet.get_all_records()
        logger.debug(f"Retrieved {len(data)} records from Google Sheets")
        
        if not data:
            logger.warning("No data found in Google Sheets")
            print("❌ No data found in Google Sheets.")
            return False
        
        # Log headers for debugging
        if data:
            headers = list(data[0].keys())
            logger.debug(f"Sheet headers: {headers}")

        logger.debug(f"Searching for user: {email}")
        for i, row in enumerate(data):
            logger.debug(f"Checking row {i+1}: {row.get('email', 'NO_EMAIL_FIELD')}")
            
            if row.get("email") == email:
                logger.debug(f"User found in row {i+1}")
                stored_password = row.get("password")
                
                if stored_password is None:
                    logger.error(f"No password field found for user: {email}")
                    print("❌ Password field not found in Google Sheets.")
                    return False
                
                logger.debug("Comparing passwords")
                if stored_password == password:  # Plain match; hash if needed
                    logger.info(f"Password match successful for user: {email}")
                    print(f"✅ Google Sheets login successful for: {email}")
                    return True
                else:
                    logger.warning(f"Password mismatch for user: {email}")
                    logger.debug(f"Expected length: {len(password)}, Got length: {len(stored_password)}")
                    print("❌ Password mismatch in Google Sheets.")
                    return False

        logger.warning(f"Email not found in Google Sheets: {email}")
        print("❌ Email not found in Google Sheets.")
        return False

    except Exception as e:
        logger.error(f"Google Sheets access error for {email}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        print(f"[!] Google Sheets access error: {e}")
        return False
    
    logger.info("Exiting verify_user_from_google_sheets function")

def delete_user_from_google_sheets(email: str) -> bool:
    """
    Deletes a user row from Google Sheets by matching the email.
    Returns True if a row was deleted, False otherwise.
    """
    logger.info("Entering delete_user_from_google_sheets function")
    logger.debug(f"Deleting user from Google Sheets: {email}")
    
    # Input validation
    if not email or not isinstance(email, str):
        logger.error(f"Invalid email for Google Sheets deletion: {email}")
        return False
    
    try:
        logger.debug("Loading Google Sheets configuration")
        config = load_google_config()
        
        if not config:
            logger.error("Failed to load Google Sheets configuration")
            return False
        
        creds_file = config["credentials_file"]
        sheet_id = config["spreadsheet_id"]
        
        logger.debug(f"Using credentials file: {creds_file}")
        logger.debug(f"Using spreadsheet ID: {sheet_id}")

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        logger.debug(f"Using full access scopes: {scopes}")
        
        logger.debug("Creating Google Sheets credentials")
        credentials = Credentials.from_service_account_file(creds_file, scopes=scopes)
        logger.debug("Credentials created successfully")
        
        logger.debug("Authorizing Google Sheets client")
        client = gspread.authorize(credentials)
        logger.debug("Client authorized successfully")

        logger.debug(f"Opening spreadsheet: {sheet_id}")
        sheet = client.open_by_key(sheet_id).sheet1
        logger.debug("Spreadsheet opened successfully")
        
        logger.debug("Retrieving all records from sheet")
        records = sheet.get_all_records()
        logger.debug(f"Retrieved {len(records)} records from Google Sheets")
        
        if not records:
            logger.warning("No records found in Google Sheets")
            print("ℹ️ No records found in Google Sheets.")
            return False
        
        logger.debug(f"Searching for user to delete: {email}")
        for i, row in enumerate(records, start=2):  # skip header, row 2 onwards
            logger.debug(f"Checking row {i}: {row.get('email', 'NO_EMAIL_FIELD')}")
            
            if row.get("email") == email:
                logger.debug(f"User found in row {i}, proceeding with deletion")
                
                try:
                    delete_result = sheet.delete_rows(i)
                    logger.debug(f"Delete operation result: {delete_result}")
                    
                    print(f"🗑️  User deleted from Google Sheets: {email}")
                    logger.info(f"User successfully deleted from Google Sheets: {email}")
                    return True
                    
                except Exception as delete_error:
                    logger.error(f"Failed to delete row {i}: {delete_error}")
                    raise

        logger.warning(f"User not found in Google Sheets for deletion: {email}")
        print("ℹ️ User not found in Google Sheets.")
        return False

    except Exception as e:
        logger.error(f"Error deleting user from Google Sheets {email}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        print(f"[!] Error deleting user from Google Sheets: {e}")
        return False
    
    logger.info("Exiting delete_user_from_google_sheets function")

def verify_user(email: str, password: str) -> bool:
    """
    Verifies user credentials:
    1. Check if user exists in local credentials
    2. If exists, decrypt and compare password
    3. If not found, fallback to Google Sheets validation
    """
    logger.info("Entering verify_user function")
    print("Inside verify_user from user_auth.py")
    logger.debug(f"Verifying user credentials: {email}")
    
    # Input validation
    if not email or not isinstance(email, str):
        logger.error(f"Invalid email for verification: {email}")
        return False
    
    if not password or not isinstance(password, str):
        logger.error(f"Invalid password for verification: {email}")
        return False
    
    if '@' not in email:
        logger.warning(f"Email format appears invalid: {email}")
    
    try:
        logger.debug("Loading local credentials")
        creds = load_credentials()
        logger.debug(f"Loaded credentials for {len(creds)} users")

        if email in creds:
            logger.debug(f"User found in local credentials: {email}")
            print("Found user email: {}".format(email))
            
            user_data = creds[email]
            logger.debug(f"User data fields: {list(user_data.keys())}")
            
            # Validate user data structure
            if "encrypted_password" not in user_data:
                logger.error(f"Missing encrypted_password field for user: {email}")
                return False
            
            try:
                logger.debug("Attempting to decrypt password")
                decrypted = decrypt_password(user_data["encrypted_password"])
                logger.debug(f"Password decrypted successfully, length: {len(decrypted)}")
                
                logger.debug("Comparing decrypted password with provided password")
                if decrypted == password:
                    logger.info(f"Local password verification successful: {email}")
                    print(f"✅ Local login successful for: {email}")
                    return True
                else:
                    logger.warning(f"Password mismatch for local user: {email}")
                    logger.debug(f"Expected length: {len(password)}, Got length: {len(decrypted)}")
                    print("❌ Incorrect password.")
                    return False
                    
            except Exception as e:
                logger.error(f"Error decrypting password for {email}: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                print(f"[!] Error decrypting password: {e}")
                return False

        logger.debug(f"User not found in local credentials: {email}")
        print("ℹ️ User not found locally. Trying Google Sheets...")
        logger.info("Falling back to Google Sheets verification")
        
        result = verify_user_from_google_sheets(email, password)
        logger.debug(f"Google Sheets verification result: {result}")
        return result
        
    except Exception as e:
        logger.error(f"Unexpected error in verify_user for {email}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False
    
    logger.info("Exiting verify_user function")

# Log module initialization completion
logger.info("user_auth module initialization completed")
logger.debug(f"Module constants - PROJECT_ROOT: {PROJECT_ROOT}")
logger.debug(f"Module constants - AUTH_DIR: {AUTH_DIR}")
logger.debug(f"Module constants - CREDENTIALS_FILE: {CREDENTIALS_FILE}")
logger.debug(f"Module constants - KEY_FILE: {KEY_FILE}")