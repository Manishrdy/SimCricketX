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
import shutil
import stat
import tempfile
import time
import random
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AUTH_DIR = os.path.join(PROJECT_ROOT, "auth")
CREDENTIALS_FILE = os.path.join(AUTH_DIR, "credentials.json")

print(f"[DEBUG] __file__ = {__file__}")
print(f"[DEBUG] os.path.dirname(__file__) = {os.path.dirname(__file__)}")
print(f"[DEBUG] PROJECT_ROOT = {PROJECT_ROOT}")
print(f"[DEBUG] AUTH_DIR = {AUTH_DIR}")
print(f"[DEBUG] CREDENTIALS_FILE = {CREDENTIALS_FILE}")
print(f"[DEBUG] File exists = {os.path.exists(CREDENTIALS_FILE)}")
print(f"[DEBUG] Current working directory = {os.getcwd()}")

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
        
        logger.debug(f"Platform detection: {platform_info}")
        logger.debug(f"Will use fcntl locking: {use_fcntl}")
        
        return platform_info, use_fcntl
        
    except Exception as e:
        logger.warning(f"Platform detection failed: {e}")
        # Default to safe fallback
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
        logger.debug(f"🔐 Using fcntl locking for: {file_path}")
        
        # Open file for locking
        file_handle = open(file_path, 'a+')
        logger.debug(f"🔐 File opened, FD: {file_handle.fileno()}")
        
        # Try to acquire lock with timeout
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_acquired = True
                logger.info(f"🔓 fcntl lock acquired for: {file_path}")
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
                logger.info(f"🔓 fcntl lock released for: {file_path}")
            except Exception as e:
                logger.error(f"❌ Error releasing fcntl lock: {e}")
        
        if file_handle:
            try:
                file_handle.close()
            except Exception as e:
                logger.warning(f"⚠️ Error closing file handle: {e}")

@contextmanager
def _acquire_file_semaphore_lock(file_path, timeout):
    """Windows/Mac/localhost file-based semaphore locking"""
    lock_file = f"{file_path}.lock"
    file_handle = None
    lock_acquired = False
    
    try:
        logger.debug(f"🔐 Using file semaphore locking for: {file_path}")
        logger.debug(f"🔐 Lock file: {lock_file}")
        
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
                logger.info(f"🔓 File semaphore lock acquired: {lock_file}")
                break
                
            except FileExistsError:
                # Lock file exists, check if it's stale
                if _is_stale_lock_file(lock_file):
                    logger.warning(f"⚠️ Removing stale lock file: {lock_file}")
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
        logger.debug(f"🔐 Target file opened: {file_path}")
        
        yield file_handle
        
    finally:
        if file_handle:
            try:
                file_handle.close()
            except Exception as e:
                logger.warning(f"⚠️ Error closing file handle: {e}")
        
        if lock_acquired and os.path.exists(lock_file):
            try:
                os.remove(lock_file)
                logger.info(f"🔓 File semaphore lock released: {lock_file}")
            except Exception as e:
                logger.error(f"❌ Error removing lock file: {e}")

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
                logger.debug(f"👤 Process running as UID: {uid}, GID: {gid}")
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
                logger.debug(f"👤 Process2 running as UID: {uid}, GID: {gid}")
                return username, "users", uid, gid
            
        # Fallback
        else:
            import getpass
            username = getpass.getuser()
            return username, "users", None, None
            
    except Exception as e:
        logger.warning(f"Could not determine user/group info: {e}")
        return "unknown", "unknown", None, None


def save_credentials(data: dict, max_retries=3, base_delay=0.1):
    """
    Cross-platform save credentials with appropriate locking mechanism.
    """
    import os
    import json
    import shutil
    import traceback
    import stat
    import tempfile
    import time
    import random
    import threading
    from datetime import datetime
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # PLATFORM DETECTION AND SETUP
    # ═══════════════════════════════════════════════════════════════════════════════
    function_start_time = datetime.now()
    thread_id = threading.get_ident()
    process_id = os.getpid()
    
    logger.info("🔥 ENTERING save_credentials function (cross-platform)")
    
    # Detect platform and locking strategy
    platform_info, use_fcntl = detect_platform_environment()
    logger.info(f"🖥️ Platform: {platform_info.get('system', 'unknown').upper()}")
    logger.info(f"🔐 Locking strategy: {'fcntl' if use_fcntl else 'file-semaphore'}")
    
    if platform_info.get('is_local_dev'):
        logger.info("🏠 Local development environment detected")
    
    logger.debug(f"🔢 Max retries: {max_retries}")
    logger.debug(f"⏱️ Base delay: {base_delay} seconds")
    logger.debug(f"📊 Users to save: {len(data)}")
    logger.debug(f"👥 User emails: {list(data.keys())}")
    logger.debug(f"🧵 Thread ID: {thread_id}")
    logger.debug(f"🔢 Process ID: {process_id}")

    # ═══════════════════════════════════════════════════════════════════════════════
    # RETRY LOOP WITH CROSS-PLATFORM LOCKING
    # ═══════════════════════════════════════════════════════════════════════════════
    
    for attempt in range(max_retries):
        attempt_start_time = datetime.now()
        logger.info(f"🔄 ATTEMPT {attempt + 1}/{max_retries} - Starting save operation")
        
        # Initialize attempt-specific variables
        temp_file = None
        backup_file = None
        backup_created = False
        
        try:
            # ═══════════════════════════════════════════════════════════════════════════
            # INPUT VALIDATION
            # ═══════════════════════════════════════════════════════════════════════════
            logger.debug(f"✅ [Attempt {attempt + 1}] Starting input validation")
            
            if not isinstance(data, dict):
                logger.error(f"❌ [Attempt {attempt + 1}] Invalid data type: {type(data)}")
                raise TypeError(f"Credentials data must be a dictionary, got {type(data)}")
            
            if not data:
                logger.error(f"❌ [Attempt {attempt + 1}] Empty data dictionary")
                raise ValueError("Credentials data cannot be empty")
            
            # Validate dictionary structure
            total_data_size = 0
            for email, user_data in data.items():
                if not isinstance(email, str):
                    raise TypeError(f"Email must be string, got {type(email)}")
                if not isinstance(user_data, dict):
                    raise TypeError(f"User data must be dict, got {type(user_data)}")
                
                required_fields = ["user_id", "encrypted_password"]
                missing_fields = [field for field in required_fields if field not in user_data]
                if missing_fields:
                    raise ValueError(f"Missing required fields for {email}: {missing_fields}")
                
                user_json = json.dumps(user_data)
                user_size = len(user_json.encode('utf-8'))
                total_data_size += user_size
            
            logger.debug(f"📏 [Attempt {attempt + 1}] Total data size: {total_data_size} bytes")
            logger.debug(f"✅ [Attempt {attempt + 1}] Input validation completed")

            # ═══════════════════════════════════════════════════════════════════════════
            # DIRECTORY VALIDATION
            # ═══════════════════════════════════════════════════════════════════════════
            logger.debug(f"📁 [Attempt {attempt + 1}] Starting directory validation")
            
            try:
                ensure_auth_directory()
                logger.debug(f"✅ [Attempt {attempt + 1}] Auth directory validated")
            except Exception as dir_error:
                logger.error(f"❌ [Attempt {attempt + 1}] Directory validation failed: {dir_error}")
                raise RuntimeError(f"Directory validation failed: {dir_error}")

            # ═══════════════════════════════════════════════════════════════════════════
            # CROSS-PLATFORM FILE LOCKING
            # ═══════════════════════════════════════════════════════════════════════════
            logger.debug(f"🔐 [Attempt {attempt + 1}] Starting cross-platform file locking")
            
            with acquire_file_lock(CREDENTIALS_FILE, use_fcntl=use_fcntl, timeout=30) as file_handle:
                logger.debug(f"🔒 [Attempt {attempt + 1}] Lock acquired, entering locked section")
                locked_section_start = datetime.now()
                
                # Re-read current file content while locked
                logger.debug(f"📖 [Attempt {attempt + 1}] Reading current file content while locked")
                file_handle.seek(0)
                current_content = file_handle.read()
                
                current_data = {}
                if current_content.strip():
                    try:
                        current_data = json.loads(current_content)
                        logger.debug(f"📖 [Attempt {attempt + 1}] Current file contains {len(current_data)} users")
                    except json.JSONDecodeError as json_error:
                        logger.warning(f"⚠️ [Attempt {attempt + 1}] Current file has invalid JSON: {json_error}")
                else:
                    logger.debug(f"📖 [Attempt {attempt + 1}] File is empty")

                # Create backup while holding lock
                if current_content.strip():
                    logger.debug(f"💾 [Attempt {attempt + 1}] Creating backup while locked")
                    backup_file = f"{CREDENTIALS_FILE}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.{thread_id}"
                    
                    try:
                        with open(backup_file, 'w') as backup_f:
                            backup_f.write(current_content)
                        
                        if os.path.exists(backup_file):
                            backup_size = os.path.getsize(backup_file)
                            logger.debug(f"💾 [Attempt {attempt + 1}] Backup created: {backup_file} ({backup_size} bytes)")
                            backup_created = True
                        
                    except Exception as backup_error:
                        logger.warning(f"⚠️ [Attempt {attempt + 1}] Backup creation failed: {backup_error}")

                # Create temporary file for atomic write
                logger.debug(f"💾 [Attempt {attempt + 1}] Creating temporary file")
                temp_dir = os.path.dirname(CREDENTIALS_FILE)
                temp_fd, temp_file = tempfile.mkstemp(
                    suffix=f'.tmp.{thread_id}',
                    prefix='credentials_',
                    dir=temp_dir,
                    text=True
                )
                
                logger.debug(f"💾 [Attempt {attempt + 1}] Temporary file: {temp_file}")
                
                # Write to temporary file
                write_start = datetime.now()
                try:
                    with os.fdopen(temp_fd, 'w') as temp_f:
                        temp_fd = None  # Prevent double close
                        json.dump(data, temp_f, indent=2, ensure_ascii=False, sort_keys=True)
                        temp_f.flush()
                        os.fsync(temp_f.fileno())
                    
                    write_end = datetime.now()
                    write_duration = (write_end - write_start).total_seconds()
                    temp_size = os.path.getsize(temp_file)
                    
                    logger.debug(f"💾 [Attempt {attempt + 1}] Temp file written in {write_duration:.3f}s, size: {temp_size} bytes")
                    
                except Exception as temp_write_error:
                    logger.error(f"❌ [Attempt {attempt + 1}] Temp file write failed: {temp_write_error}")
                    if temp_fd:
                        os.close(temp_fd)
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                    raise

                # Verify temp file
                logger.debug(f"🔍 [Attempt {attempt + 1}] Verifying temporary file")
                try:
                    with open(temp_file, 'r') as verify_f:
                        verify_data = json.load(verify_f)
                    
                    if len(verify_data) == len(data):
                        logger.debug(f"✅ [Attempt {attempt + 1}] Temp file verification successful")
                    else:
                        raise ValueError("Temporary file verification failed")
                        
                except Exception as verify_error:
                    logger.error(f"❌ [Attempt {attempt + 1}] Temp file verification error: {verify_error}")
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                    raise

                # Atomic replacement
                logger.debug(f"🔄 [Attempt {attempt + 1}] Performing atomic file replacement")
                replace_start = datetime.now()
                
                try:
                    # Truncate the locked file and write new content
                    file_handle.seek(0)
                    file_handle.truncate()
                    
                    # Copy content from temp file to locked file
                    with open(temp_file, 'r') as temp_read:
                        new_content = temp_read.read()
                        file_handle.write(new_content)
                        file_handle.flush()
                        os.fsync(file_handle.fileno())
                    
                    replace_end = datetime.now()
                    replace_duration = (replace_end - replace_start).total_seconds()
                    logger.debug(f"🔄 [Attempt {attempt + 1}] File replacement completed in {replace_duration:.3f}s")
                    
                    # Remove temp file
                    os.remove(temp_file)
                    temp_file = None
                    logger.debug(f"🧹 [Attempt {attempt + 1}] Temporary file cleaned up")
                    
                except Exception as replace_error:
                    logger.error(f"❌ [Attempt {attempt + 1}] Atomic replacement failed: {replace_error}")
                    
                    # Cleanup temp file
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                    
                    # Try to restore backup
                    if backup_created and backup_file and os.path.exists(backup_file):
                        try:
                            file_handle.seek(0)
                            file_handle.truncate()
                            with open(backup_file, 'r') as backup_read:
                                file_handle.write(backup_read.read())
                            file_handle.flush()
                            logger.warning(f"🔄 [Attempt {attempt + 1}] Restored backup after replacement failure")
                        except Exception as restore_error:
                            logger.error(f"❌ [Attempt {attempt + 1}] Backup restoration failed: {restore_error}")
                    
                    raise

                # Final verification while still locked
                logger.debug(f"🔍 [Attempt {attempt + 1}] Performing final verification")
                try:
                    file_handle.seek(0)
                    verify_content = file_handle.read()
                    verify_data = json.loads(verify_content)
                    
                    if len(verify_data) == len(data):
                        logger.debug(f"✅ [Attempt {attempt + 1}] Final verification successful - {len(verify_data)} users")
                        
                        # Verify each user
                        for email in data.keys():
                            if email not in verify_data:
                                logger.error(f"❌ [Attempt {attempt + 1}] User missing in final verification: {email}")
                                raise ValueError(f"User missing after write: {email}")
                        
                        logger.info(f"✅ [Attempt {attempt + 1}] All users verified in final file")
                    else:
                        logger.error(f"❌ [Attempt {attempt + 1}] Final verification count mismatch")
                        raise ValueError("Final verification failed - user count mismatch")
                        
                except Exception as final_verify_error:
                    logger.error(f"❌ [Attempt {attempt + 1}] Final verification failed: {final_verify_error}")
                    raise

                locked_section_end = datetime.now()
                locked_section_duration = (locked_section_end - locked_section_start).total_seconds()
                logger.debug(f"🔒 [Attempt {attempt + 1}] Locked section completed in {locked_section_duration:.3f} seconds")

            # Lock is automatically released here by context manager
            logger.debug(f"🔓 [Attempt {attempt + 1}] Lock released by context manager")

            # Success cleanup
            if backup_created and backup_file and os.path.exists(backup_file):
                logger.debug(f"💾 [Attempt {attempt + 1}] Keeping backup file: {backup_file}")

            # SUCCESS
            attempt_end_time = datetime.now()
            attempt_duration = (attempt_end_time - attempt_start_time).total_seconds()
            
            logger.info(f"🎉 [Attempt {attempt + 1}] SAVE OPERATION COMPLETED SUCCESSFULLY!")
            logger.info(f"📊 [Attempt {attempt + 1}] Saved {len(data)} users to {CREDENTIALS_FILE}")
            logger.info(f"⏱️ [Attempt {attempt + 1}] Total attempt duration: {attempt_duration:.3f} seconds")
            logger.info(f"📄 [Attempt {attempt + 1}] Final file size: {os.path.getsize(CREDENTIALS_FILE)} bytes")
            
            # Function success
            function_end_time = datetime.now()
            total_function_duration = (function_end_time - function_start_time).total_seconds()
            
            logger.info(f"🏁 save_credentials COMPLETED SUCCESSFULLY after {attempt + 1} attempts")
            logger.info(f"⏱️ Total function duration: {total_function_duration:.3f} seconds")
            logger.info(f"🔥 EXITING save_credentials function")
            
            return  # Success!

        except Exception as attempt_error:
            # ATTEMPT FAILURE
            attempt_end_time = datetime.now()
            attempt_duration = (attempt_end_time - attempt_start_time).total_seconds()
            
            logger.error(f"❌ [Attempt {attempt + 1}] ATTEMPT FAILED after {attempt_duration:.3f} seconds")
            logger.error(f"❌ [Attempt {attempt + 1}] Error: {attempt_error}")
            logger.error(f"❌ [Attempt {attempt + 1}] Traceback: {traceback.format_exc()}")
            
            # Cleanup on failure
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                    logger.debug(f"🧹 [Attempt {attempt + 1}] Cleaned up temp file on failure")
                except:
                    pass
            
            # Re-raise if this was the last attempt
            if attempt == max_retries - 1:
                logger.error(f"❌ ALL RETRY ATTEMPTS EXHAUSTED - Final failure")
                raise RuntimeError(f"save_credentials failed after {max_retries} attempts: {attempt_error}")
            
            # Calculate retry delay
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
                logger.warning(f"⏳ [Attempt {attempt + 1}] Retrying in {delay:.3f} seconds...")
                time.sleep(delay)
                continue  # Retry

    # This should never be reached
    logger.error(f"❌ UNEXPECTED: Exited retry loop without success or final failure")
    raise RuntimeError("Unexpected exit from retry loop")


# Ensure auth directory exists with proper error handling
def ensure_auth_directory():
    """Ensure auth directory exists and is writable"""
    try:
        if not os.path.exists(AUTH_DIR):
            os.makedirs(AUTH_DIR, mode=0o777, exist_ok=True)
            logger.info(f"Created AUTH_DIR: {AUTH_DIR}")
        
        # Test write permissions
        test_file = os.path.join(AUTH_DIR, ".write_test")
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
        logger.debug("AUTH_DIR write permissions verified")
        
    except PermissionError as e:
        logger.error(f"Permission denied creating AUTH_DIR: {e}")
        raise RuntimeError(f"Cannot create auth directory: {AUTH_DIR}")
    except Exception as e:
        logger.error(f"Error setting up AUTH_DIR: {e}")
        raise

# Avoid adding multiple handlers if already configured
if not logger.handlers:
    # Console handler with UTF-8 encoding
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    
    # File handler with UTF-8 encoding
    file_handler = logging.FileHandler('user_auth.log', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    
    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    console_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)
    
    # Add both handlers
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)  # ← This creates the log file!

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

        try:
            logger.debug("Writing user to Google Sheets")
            write_user_to_google_sheets(email, password, user_data)
            logger.info(f"User data written to Google Sheets: {email}")
        except Exception as sheets_error:
            # Log the error but don't fail registration
            logger.warning(f"Google Sheets backup failed for {email}: {sheets_error}")
            print(f"⚠️ Registration successful locally, Google Sheets backup failed: {sheets_error}")

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

        # 🚨 NEW: Find ALL matching records and get the latest one
        logger.debug(f"Searching for all records with email: {email}")
        matching_records = []
        
        for i, row in enumerate(data):
            logger.debug(f"Checking row {i+1}: {row.get('email', 'NO_EMAIL_FIELD')}")
            
            if row.get("email") == email:
                logger.debug(f"Found matching record in row {i+1}")
                matching_records.append({
                    'row_number': i+1,
                    'data': row
                })
        
        if not matching_records:
            logger.warning(f"Email not found in Google Sheets: {email}")
            print("❌ Email not found in Google Sheets.")
            return False
        
        logger.info(f"Found {len(matching_records)} matching records for email: {email}")
        
        # If multiple records, get the latest one based on login_time
        if len(matching_records) > 1:
            logger.info(f"Multiple records found for {email}, selecting latest...")
            
            # Sort by login_time (latest first)
            try:
                # Try to parse login_time as datetime for proper sorting
                from datetime import datetime
                
                def parse_login_time(record):
                    try:
                        login_time_str = record['data'].get('login_time', '')
                        if login_time_str:
                            # Try ISO format first
                            return datetime.fromisoformat(login_time_str.replace('Z', '+00:00'))
                        else:
                            return datetime.min  # Default to earliest time if no login_time
                    except:
                        # If parsing fails, use row number as fallback (higher = later)
                        return datetime.fromtimestamp(record['row_number'])
                
                matching_records.sort(key=parse_login_time, reverse=True)
                logger.debug(f"Sorted {len(matching_records)} records by login_time")
                
            except Exception as sort_error:
                logger.warning(f"Could not sort by login_time: {sort_error}")
                # Fallback: use the last row (highest row number)
                matching_records.sort(key=lambda x: x['row_number'], reverse=True)
                logger.debug(f"Fallback: sorted by row number (latest = row {matching_records[0]['row_number']})")
        
        # Use the latest/first record
        selected_record = matching_records[0]
        row_data = selected_record['data']
        row_number = selected_record['row_number']
        
        logger.info(f"Using record from row {row_number} for verification")
        if len(matching_records) > 1:
            logger.info(f"Selected latest record from {len(matching_records)} duplicates")
        
        # Verify password
        stored_password = row_data.get("password")
        
        if stored_password is None:
            logger.error(f"No password field found for user: {email}")
            print("❌ Password field not found in Google Sheets.")
            return False
        
        logger.debug("Comparing passwords")
        if stored_password == password:  # Plain match; hash if needed
            logger.info(f"Password match successful for user: {email} (row {row_number})")
            print(f"✅ Google Sheets login successful for: {email}")
            
            # Log which record was used
            login_time = row_data.get('login_time', 'Unknown')
            logger.info(f"Used record with login_time: {login_time}")
            
            return True
        else:
            logger.warning(f"Password mismatch for user: {email}")
            logger.debug(f"Expected length: {len(password)}, Got length: {len(stored_password)}")
            print("❌ Password mismatch in Google Sheets.")
            return False

    except Exception as e:
        logger.error(f"Google Sheets access error for {email}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        print(f"[!] Google Sheets access error: {e}")
        return False


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
ensure_auth_directory()