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
import yaml
import shutil
import stat
import tempfile
import time
import random
import threading
from contextlib import contextmanager

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AUTH_DIR = os.path.join(PROJECT_ROOT, "auth")
CREDENTIALS_FILE = os.path.join(AUTH_DIR, "credentials.json")

# Add these imports at the top of user_auth.py
try:
    import fcntl
    FCNTL_AVAILABLE = True
except ImportError:
    FCNTL_AVAILABLE = False

def detect_platform_environment():
    """
    Detect the platform and environment to determine locking strategy.
    Returns: (platform_type, use_fcntl_locking)
    """
    try:
        hostname = socket.gethostname()
        ip_address = socket.gethostbyname(hostname)
        
        # Check if we're in a local development environment
        is_local_dev = ('localhost' in hostname.lower() or 
                       ip_address in ['127.0.0.1', '192.168.254.131'] or
                       hostname.lower() in ['localhost', 'local'])
        
        system = platform.system().lower()
        is_windows = system == 'windows'
        is_macos = system == 'darwin'
        is_unix = system in ['linux', 'unix', 'freebsd', 'openbsd', 'netbsd']
        
        # Use fcntl only on Unix/Linux in production (not local dev)
        use_fcntl = (FCNTL_AVAILABLE and is_unix and not is_local_dev)
        
        platform_info = {
            'system': system,
            'hostname': hostname,
            'ip_address': ip_address,
            'is_local_dev': is_local_dev,
            'is_windows': is_windows,
            'is_macos': is_macos,
            'is_unix': is_unix
        }
        
        return platform_info, use_fcntl
        
    except Exception as e:
        return {'system': 'unknown'}, False

@contextmanager
def acquire_file_lock(file_path, use_fcntl=True, timeout=30):
    if use_fcntl and FCNTL_AVAILABLE:
        with _acquire_fcntl_lock(file_path, timeout) as handle:
            yield handle
    else:
        with _acquire_file_semaphore_lock(file_path, timeout) as handle:
            yield handle

@contextmanager
def _acquire_fcntl_lock(file_path, timeout):
    """Unix/Linux fcntl-based locking"""
    file_handle = None
    lock_acquired = False
    
    try:
        # Open file for locking
        file_handle = open(file_path, 'a+')
        # Try to acquire lock with timeout
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_acquired = True
                break
            except BlockingIOError:
                time.sleep(0.1)  # Wait 100ms before retry
        
        if not lock_acquired:
            raise TimeoutError(f"Could not acquire fcntl lock within {timeout} seconds")
        
        yield file_handle
        
    finally:
        if lock_acquired and file_handle:
            try:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                raise
        
        if file_handle:
            try:
                file_handle.close()
            except Exception as e:
                raise

@contextmanager
def _acquire_file_semaphore_lock(file_path, timeout):
    """Windows/Mac/localhost file-based semaphore locking"""
    lock_file = f"{file_path}.lock"
    file_handle = None
    lock_acquired = False
    
    try:
        # Try to acquire lock with timeout
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                # Try to create lock file exclusively
                lock_fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                
                # Write lock info
                lock_info = {
                    'pid': os.getpid(),
                    'timestamp': datetime.now().isoformat(),
                    'thread_id': threading.get_ident()
                }
                os.write(lock_fd, json.dumps(lock_info).encode())
                os.close(lock_fd)
                
                lock_acquired = True
                break
                
            except FileExistsError:
                # Lock file exists, check if it's stale
                if _is_stale_lock_file(lock_file):
                    try:
                        os.remove(lock_file)
                        continue  # Try again
                    except Exception:
                        pass  # Continue waiting
                
                time.sleep(0.1 + random.uniform(0, 0.05))  # Random jitter
        
        if not lock_acquired:
            raise TimeoutError(f"Could not acquire file semaphore lock within {timeout} seconds")
        
        # Open the actual file for reading/writing
        file_handle = open(file_path, 'a+')

        yield file_handle
        
    finally:
        if file_handle:
            try:
                file_handle.close()
            except Exception as e:
                raise
        
        if lock_acquired and os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except Exception as e:
                raise

def _is_stale_lock_file(lock_file, max_age_seconds=300):
    """Check if a lock file is stale (older than max_age_seconds)"""
    try:
        if not os.path.exists(lock_file):
            return False
        
        # Check file age
        file_age = time.time() - os.path.getmtime(lock_file)
        if file_age > max_age_seconds:
            return True
        
        # Try to read lock info and check if process still exists
        try:
            with open(lock_file, 'r') as f:
                lock_info = json.load(f)
            
            lock_pid = lock_info.get('pid')
            if lock_pid:
                # Check if process is still running (Unix/Linux only)
                if hasattr(os, 'kill'):
                    try:
                        os.kill(lock_pid, 0)  # Signal 0 just checks if process exists
                        return False  # Process exists, not stale
                    except (OSError, ProcessLookupError):
                        return True  # Process doesn't exist, stale
        
        except (json.JSONDecodeError, KeyError):
            return True  # Invalid lock file, consider stale
        
        return False
        
    except Exception:
        return False  # Conservative: assume not stale if we can't determine


# Add this function at the top of your file, after the imports
def get_user_group_info():
    """
    Get user and group information based on environment.
    Returns: (username, groupname, uid, gid)
    """
    try:
        hostname = socket.gethostname()
        ip_address = socket.gethostbyname(hostname)
        
        # Check if we're in a local development environment
        is_local_dev = ('localhost' in hostname.lower() or 
                       ip_address in ['127.0.0.1', '192.168.254.131'] or
                       hostname.lower() in ['localhost', 'local'])
        
        is_windows = platform.system().lower() == 'windows'
        is_macos = platform.system().lower() == 'darwin'
        is_unix = platform.system().lower() in ['linux', 'unix', 'freebsd', 'openbsd', 'netbsd']
        
        # Use cross-platform methods for local dev, Windows, or Mac
        if is_local_dev or is_windows or is_macos:
            import getpass
            username = getpass.getuser()
            
            # Try to get UID/GID if available
            try:
                uid = os.getuid() if hasattr(os, 'getuid') else 'unknown'
                gid = os.getgid() if hasattr(os, 'getgid') else 'unknown'
            except AttributeError:
                uid = None
                gid = None
            
            # Group name is harder to get cross-platform
            groupname = "users"  # Default fallback
            
            return username, groupname, uid, gid
            
        # Use Unix-specific methods for production Unix/Linux
        elif is_unix:
            try:
                import pwd
                import grp
                
                uid = os.getuid()
                gid = os.getgid()
                username = pwd.getpwuid(uid).pw_name
                groupname = grp.getgrgid(gid).gr_name
                
                return username, groupname, uid, gid
                
            except ImportError:
                # Fallback if pwd/grp not available even on Unix
                import getpass
                username = getpass.getuser()
                uid = os.getuid() if hasattr(os, 'getuid') else 'unknown'
                gid = os.getgid() if hasattr(os, 'getgid') else 'unknown'
                return username, "users", uid, gid
            
        # Fallback
        else:
            import getpass
            username = getpass.getuser()
            return username, "users", None, None
            
    except Exception as e:
        return "unknown", "unknown", None, None


def save_credentials(data: dict, max_retries=3, base_delay=0.1):
    import os
    import json
    import shutil
    import tempfile
    import time
    import random
    import threading
    from datetime import datetime
    import traceback

    function_start_time = datetime.now()
    thread_id = threading.get_ident()

    platform_info, use_fcntl = detect_platform_environment()

    for attempt in range(max_retries):
        attempt_start_time = datetime.now()
        temp_file = None
        backup_file = None
        backup_created = False

        try:
            if not isinstance(data, dict):
                raise TypeError("Credentials data must be a dictionary")
            if not data:
                raise ValueError("Credentials data cannot be empty")

            for email, user_data in data.items():
                if not isinstance(email, str):
                    raise TypeError("Email must be a string")
                if not isinstance(user_data, dict):
                    raise TypeError("User data must be a dict")
                required_fields = ["user_id", "encrypted_password"]
                if any(field not in user_data for field in required_fields):
                    raise ValueError(f"Missing required fields for {email}")

            ensure_auth_directory()

            with acquire_file_lock(CREDENTIALS_FILE, use_fcntl=use_fcntl, timeout=30) as file_handle:
                file_handle.seek(0)
                current_content = file_handle.read()

                current_data = {}
                if current_content.strip():
                    try:
                        current_data = json.loads(current_content)
                    except json.JSONDecodeError:
                        pass

                if current_content.strip():
                    backup_file = f"{CREDENTIALS_FILE}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.{thread_id}"
                    try:
                        with open(backup_file, 'w') as backup_f:
                            backup_f.write(current_content)
                        if os.path.exists(backup_file):
                            backup_created = True
                    except:
                        pass

                temp_dir = os.path.dirname(CREDENTIALS_FILE)
                temp_fd, temp_file = tempfile.mkstemp(
                    suffix=f'.tmp.{thread_id}',
                    prefix='credentials_',
                    dir=temp_dir,
                    text=True
                )

                try:
                    with os.fdopen(temp_fd, 'w') as temp_f:
                        temp_fd = None
                        json.dump(data, temp_f, indent=2, ensure_ascii=False, sort_keys=True)
                        temp_f.flush()
                        os.fsync(temp_f.fileno())
                except:
                    if temp_fd:
                        os.close(temp_fd)
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                    raise

                try:
                    with open(temp_file, 'r') as verify_f:
                        verify_data = json.load(verify_f)
                    if len(verify_data) != len(data):
                        raise ValueError("Temporary file verification failed")
                except:
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                    raise

                try:
                    file_handle.seek(0)
                    file_handle.truncate()
                    with open(temp_file, 'r') as temp_read:
                        file_handle.write(temp_read.read())
                        file_handle.flush()
                        os.fsync(file_handle.fileno())
                    os.remove(temp_file)
                    temp_file = None
                except:
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                    if backup_created and backup_file and os.path.exists(backup_file):
                        try:
                            file_handle.seek(0)
                            file_handle.truncate()
                            with open(backup_file, 'r') as backup_read:
                                file_handle.write(backup_read.read())
                            file_handle.flush()
                        except:
                            pass
                    raise

                try:
                    file_handle.seek(0)
                    verify_content = file_handle.read()
                    verify_data = json.loads(verify_content)
                    if len(verify_data) != len(data):
                        raise ValueError("Final verification failed - user count mismatch")
                    for email in data.keys():
                        if email not in verify_data:
                            raise ValueError(f"User missing after write: {email}")
                except:
                    raise

            return

        except Exception as attempt_error:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
            if attempt == max_retries - 1:
                raise RuntimeError(f"save_credentials failed after {max_retries} attempts: {attempt_error}")
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
            time.sleep(delay)
            continue

    raise RuntimeError("Unexpected exit from retry loop")


# Ensure auth directory exists with proper error handling
def ensure_auth_directory():
    """Ensure auth directory exists and is writable"""
    try:
        if not os.path.exists(AUTH_DIR):
            os.makedirs(AUTH_DIR, mode=0o777, exist_ok=True)

        # Test write permissions
        test_file = os.path.join(AUTH_DIR, ".write_test")
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)

    except PermissionError as e:
        raise RuntimeError(f"Cannot create auth directory: {AUTH_DIR}")
    except Exception as e:
        raise

KEY_FILE = os.path.join(AUTH_DIR, "encryption.key")

# Ensure auth directory exists
if not os.path.exists(AUTH_DIR):
    try:
        os.makedirs(AUTH_DIR, exist_ok=True)
    except Exception as e:
        raise

# Step 1: Generate a key (only once)
def generate_key():
    try:
        key = Fernet.generate_key()
        with open(KEY_FILE, 'wb') as f:
            f.write(key)

        # Optionally verify file creation
        if not os.path.exists(KEY_FILE):
            raise FileNotFoundError(f"Key file not found after write: {KEY_FILE}")

    except Exception:
        raise

# Step 2: Load the key
def load_key():
    if not os.path.exists(KEY_FILE):
        generate_key()

    try:
        with open(KEY_FILE, 'rb') as f:
            key = f.read()

        try:
            Fernet(key)  # Validate key format
        except Exception:
            raise

        return key

    except Exception:
        raise


# Step 3: Encrypt password
def encrypt_password(password: str) -> str:
    if not password:
        pass  # Optionally raise or handle empty password
    
    if len(password) > 1000:
        pass  # Optionally warn about long passwords

    try:
        key = load_key()
        f = Fernet(key)
        password_bytes = password.encode()
        encrypted_bytes = f.encrypt(password_bytes)
        encrypted_string = encrypted_bytes.decode()
        return encrypted_string

    except Exception:
        raise

# Step 4: Decrypt password
def decrypt_password(encrypted: str) -> str:
    if not encrypted:
        raise ValueError("Empty encrypted string provided")
    
    try:
        key = load_key()
        f = Fernet(key)
        encrypted_bytes = encrypted.encode()
        decrypted_bytes = f.decrypt(encrypted_bytes)
        decrypted_string = decrypted_bytes.decode()
        return decrypted_string

    except Exception:
        raise


# Utility to read credentials file
def load_credentials() -> dict:
    if not os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, 'w') as f:
                json.dump({}, f)
        except Exception:
            raise
    try:
        with open(CREDENTIALS_FILE, 'r') as f:
            credentials = json.load(f)

        # Validate credentials structure
        for email, user_data in credentials.items():
            if not isinstance(user_data, dict):
                continue
            required_fields = ["user_id", "encrypted_password"]
            missing_fields = [field for field in required_fields if field not in user_data]
            if missing_fields:
                continue

        return credentials

    except json.JSONDecodeError:
        logging.warning("[!] credentials.json is empty or corrupted. Reinitializing.")
        # Backup corrupted file
        backup_file = f"{CREDENTIALS_FILE}.corrupted.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            if os.path.exists(CREDENTIALS_FILE):
                os.rename(CREDENTIALS_FILE, backup_file)
        except Exception:
            pass
        try:
            with open(CREDENTIALS_FILE, 'w') as f:
                json.dump({}, f)
            return {}
        except Exception:
            raise

    except Exception:
        raise

def get_ip_address() -> str:
    try:
        hostname = socket.gethostname()

        ip_address = socket.gethostbyname(hostname)

        return ip_address
        
    except Exception as e:
        return ""
    
def get_mac_address() -> str:
    try:
        mac = uuid_lib.getnode()

        # Check if MAC is locally administered
        if (mac >> 40) % 2:
            return ""  # MAC is locally administered or invalid
        
        mac_address = ':'.join(f'{(mac >> ele) & 0xff:02x}' for ele in range(40, -1, -8))
    
        # Validate MAC format
        if len(mac_address) == 17 and mac_address.count(':') == 5:
            return mac_address
        else:
            return ""
            
    except Exception as e:
        return ""
    
def delete_user(email: str) -> bool:
    """
    Deletes a user from the credentials file.
    Returns True if deleted, False if user doesn't exist.
    """
    # Input validation
    if not email or not isinstance(email, str):
        return False
    try:
        creds = load_credentials()

        if email not in creds:
            logging.warning(f"User not found: {email}")
            return False

        user_data = creds[email]
        
        del creds[email]
        save_credentials(creds)
        return True
        
    except Exception as e:
        return False

def register_user(email: str, password: str) -> bool:
    
    # Input validation
    if not email or not isinstance(email, str):
        return False
    
    if not password or not isinstance(password, str):
        return False
    
    try:
        creds = load_credentials()

        if email in creds:
            logging.warning(f"[!] User already exists: {email}")
            return False

        encrypted = encrypt_password(password)

        ip_address = get_ip_address()

        mac_address = get_mac_address()

        hostname = socket.gethostname()

        os_system = platform.system()

        os_version = platform.version()

        machine = platform.machine()

        processor = platform.processor()

        login_time = datetime.now().isoformat()

        user_id = str(uuid_lib.uuid4())
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
        
        creds[email] = user_data
        creds[email] = user_data
        save_credentials(creds)
        logging.info(f"[+] User registered successfully: {email}")
        return True
        
    except Exception as e:
        return False

def verify_user(email: str, password: str) -> bool:
    """
    Verifies user credentials against the local credentials file.
    1. Check if user exists in local credentials.
    2. If exists, decrypt and compare password.
    """
    # logging.debug("Inside verify_user from user_auth.py")

    try:
        creds = load_credentials()
        if email in creds:
            # logging.debug("Found user email: {}".format(email))
            
            user_data = creds[email]
            # Validate user data structure
            if "encrypted_password" not in user_data:
                return False
            
            try:
                decrypted = decrypt_password(user_data["encrypted_password"])
                if decrypted == password:
                    logging.info(f"Local login successful for: {email}")
                    return True
                else:
                    logging.warning("Incorrect password.")
                    return False
                    
            except Exception as e:
                logging.error(f"[!] Error decrypting password: {e}")
                return False

        logging.warning(f"User not found: {email}")
        return False
        
    except Exception as e:
        return False

# Log module initialization completion
ensure_auth_directory()