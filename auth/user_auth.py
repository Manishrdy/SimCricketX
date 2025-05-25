import os
import json
import uuid
import socket
import platform
from datetime import datetime
import uuid as uuid_lib
from cryptography.fernet import Fernet
from utils.helpers import load_config
import gspread
from google.oauth2.service_account import Credentials
import yaml

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AUTH_DIR = os.path.join(PROJECT_ROOT, "auth")

CREDENTIALS_FILE = os.path.join(AUTH_DIR, "credentials.json")
KEY_FILE = os.path.join(AUTH_DIR, "encryption.key")

# Step 1: Generate a key (only once)
def generate_key():
    key = Fernet.generate_key()
    with open(KEY_FILE, 'wb') as f:
        f.write(key)

# Step 2: Load the key
def load_key():
    if not os.path.exists(KEY_FILE):
        generate_key()
    with open(KEY_FILE, 'rb') as f:
        return f.read()

# Step 3: Encrypt password
def encrypt_password(password: str) -> str:
    key = load_key()
    f = Fernet(key)
    return f.encrypt(password.encode()).decode()

# Step 4: Decrypt password
def decrypt_password(encrypted: str) -> str:
    key = load_key()
    f = Fernet(key)
    return f.decrypt(encrypted.encode()).decode()

# Utility to read credentials file
def load_credentials() -> dict:
    if not os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, 'w') as f:
            json.dump({}, f)

    try:
        with open(CREDENTIALS_FILE, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        print("[!] credentials.json is empty or corrupted. Reinitializing.")
        with open(CREDENTIALS_FILE, 'w') as f:
            json.dump({}, f)
        return {}


# Utility to write credentials
def save_credentials(data: dict):
    with open(CREDENTIALS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def get_ip_address() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return ""

def get_mac_address() -> str:
    try:
        mac = uuid_lib.getnode()
        if (mac >> 40) % 2:
            return ""  # MAC is locally administered or invalid
        return ':'.join(f'{(mac >> ele) & 0xff:02x}' for ele in range(40, -1, -8))
    except Exception:
        return ""

def delete_user(email: str) -> bool:
    """
    Deletes a user from the credentials file.
    Returns True if deleted, False if user doesn't exist.
    """
    creds = load_credentials()

    if email not in creds:
        print(f"❌ User not found: {email}")
        return False

    del creds[email]
    save_credentials(creds)
    delete_user_from_google_sheets(email)
    print(f"🗑️  User deleted successfully: {email}")
    return True


def register_user(email: str, password: str) -> bool:
    creds = load_credentials()

    if email in creds:
        print(f"[!] User already exists: {email}")
        return False

    encrypted = encrypt_password(password)

    user_data = {
        "user_id": str(uuid_lib.uuid4()),
        "encrypted_password": encrypted,
        "decrypted_password": password,  # 👈 Not safe for production
        "ip_address": get_ip_address(),
        "mac_address": get_mac_address(),
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "login_time": datetime.now().isoformat()
    }

    creds[email] = user_data
    save_credentials(creds)

    write_user_to_google_sheets(email, password, user_data)


    print(f"[+] User registered successfully: {email}")
    return True


def load_google_config():
    try:
        config = load_config()
        google = config.get("google_sheets", {})

        if not google:
            raise ValueError("Missing 'google_sheets' block in config.yaml.")

        # Resolve full path to credentials file
        rel_creds = google.get("credentials_file", "")
        abs_creds = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), rel_creds)
        google["credentials_file"] = abs_creds

        return google

    except Exception as e:
        print(f"[!] Error loading Google Sheets config: {e}")
        return {}


def write_user_to_google_sheets(email: str, password: str, user_data: dict):
    """
    Appends user data to Google Sheets for central storage.
    """
    config = load_google_config()
    creds_file = config["credentials_file"]
    sheet_id = config["spreadsheet_id"]

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_file(creds_file, scopes=scopes)
    client = gspread.authorize(credentials)

    try:
        sheet = client.open_by_key(sheet_id).sheet1  # Use the first worksheet
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
        sheet.append_row(row)
        print("📝 User data written to Google Sheets.")
    except Exception as e:
        print(f"[!] Failed to write to Google Sheets: {e}")


def verify_user_from_google_sheets(email: str, password: str) -> bool:
    config = load_google_config()
    creds_file = config["credentials_file"]
    sheet_id = config["spreadsheet_id"]

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials = Credentials.from_service_account_file(creds_file, scopes=scopes)
    client = gspread.authorize(credentials)

    try:
        sheet = client.open_by_key(sheet_id).sheet1  # use first sheet
        data = sheet.get_all_records()

        for row in data:
            if row.get("email") == email:
                if row.get("password") == password:  # Plain match; hash if needed
                    print(f"✅ Google Sheets login successful for: {email}")
                    return True
                else:
                    print("❌ Password mismatch in Google Sheets.")
                    return False

        print("❌ Email not found in Google Sheets.")
        return False

    except Exception as e:
        print(f"[!] Google Sheets access error: {e}")
        return False


def delete_user_from_google_sheets(email: str) -> bool:
    """
    Deletes a user row from Google Sheets by matching the email.
    Returns True if a row was deleted, False otherwise.
    """
    config = load_google_config()
    creds_file = config["credentials_file"]
    sheet_id = config["spreadsheet_id"]

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_file(creds_file, scopes=scopes)
    client = gspread.authorize(credentials)

    try:
        sheet = client.open_by_key(sheet_id).sheet1
        records = sheet.get_all_records()
        for i, row in enumerate(records, start=2):  # skip header, row 2 onwards
            if row.get("email") == email:
                sheet.delete_rows(i)
                print(f"🗑️  User deleted from Google Sheets: {email}")
                return True

        print("ℹ️ User not found in Google Sheets.")
        return False

    except Exception as e:
        print(f"[!] Error deleting user from Google Sheets: {e}")
        return False

def verify_user(email: str, password: str) -> bool:
    """
    Verifies user credentials:
    1. Check if user exists in local credentials
    2. If exists, decrypt and compare password
    3. If not found, fallback to Google Sheets validation
    """
    creds = load_credentials()

    if email in creds:
        try:
            decrypted = decrypt_password(creds[email]["encrypted_password"])
            if decrypted == password:
                print(f"✅ Local login successful for: {email}")
                return True
            else:
                print("❌ Incorrect password.")
                return False
        except Exception as e:
            print(f"[!] Error decrypting password: {e}")
            return False

    print("ℹ️ User not found locally. Trying Google Sheets...")
    return verify_user_from_google_sheets(email, password)


# if __name__ == "__main__":
#     print("Choose an action:")
#     print("1. Register")
#     print("2. Delete")
#     print("3. Login")
#     action = input("Enter 1 or 2 or 3: ")

#     email = input("Enter email: ")

#     if action == "1":
#         password = input("Enter password: ")
#         success = register_user(email, password)
#         if success:
#             print("\n✅ Registration successful. User profile:")
#             creds = load_credentials()
#             for key, value in creds[email].items():
#                 print(f"{key.ljust(16)}: {value}")
#         else:
#             print("❌ User already exists")

#     elif action == "2":
#         if delete_user(email):
#             print("✅ Deletion successful")
#         else:
#             print("❌ Deletion failed (user not found)")

#     elif action == "3":
#         password = input("Enter password: ")
#         if verify_user(email, password):
#             print("✅ Login successful")
#         else:
#             print("❌ Login failed")





