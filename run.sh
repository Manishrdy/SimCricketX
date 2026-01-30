#!/bin/bash

# ========== CONFIGURATION ==========
# UPDATE THESE VALUES FOR YOUR REPOSITORY
GITHUB_USER="Manishrdy"
GITHUB_REPO="SimCricketX"
MAIN_BRANCH="main"  # Change to "master" if your default branch is master
# ===================================

# ========== DEFAULT SETTINGS ==========
# These can be overridden in .simcricketx.conf
AUTO_UPDATE_CHECK="${AUTO_UPDATE_CHECK:-true}"
UPDATE_CHECK_INTERVAL="${UPDATE_CHECK_INTERVAL:-86400}"  # 24 hours in seconds
PRESERVE_BACKUPS_DAYS="${PRESERVE_BACKUPS_DAYS:-30}"
DEFAULT_PORT="${DEFAULT_PORT:-7860}"
CHECK_PORT_BEFORE_START="${CHECK_PORT_BEFORE_START:-true}"
ENABLE_UPDATE_LOGGING="${ENABLE_UPDATE_LOGGING:-true}"
LOG_FILE="simcricketx_updates.log"
# ======================================

# ============================================
# LOGGING FUNCTION
# ============================================
log_message() {
    if [[ "$ENABLE_UPDATE_LOGGING" == "true" ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    fi
}

# ============================================
# PRE-FLIGHT DEPENDENCY CHECK
# ============================================
check_dependencies() {
    echo "Checking system dependencies..."
    local missing=()
    
    for cmd in curl unzip python3; do
        if ! command -v $cmd &> /dev/null; then
            # Try python as fallback for python3
            if [[ "$cmd" == "python3" ]] && command -v python &> /dev/null; then
                continue
            fi
            missing+=($cmd)
        fi
    done
    
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "✗ Missing dependencies: ${missing[*]}"
        echo ""
        echo "Install with:"
        echo "  macOS:  brew install ${missing[*]}"
        echo "  Linux:  sudo apt install ${missing[*]}"
        echo ""
        log_message "ERROR: Missing dependencies: ${missing[*]}"
        return 1
    fi
    
    echo "✓ All dependencies available"
    log_message "All dependencies check passed"
    return 0
}

# ============================================
# ROLLBACK FUNCTION
# ============================================
rollback_update() {
    echo ""
    echo "============================================"
    echo "   🔄 ROLLBACK MODE"
    echo "============================================"
    
    if [[ -z "$1" ]]; then
        echo "Available backups:"
        local backups=(backup_*)
        if [[ -d "${backups[0]}" ]]; then
            for backup in backup_*; do
                if [[ -d "$backup" ]]; then
                    local backup_date=${backup#backup_}
                    echo "  - $backup (${backup_date:0:8} ${backup_date:9:2}:${backup_date:11:2}:${backup_date:13:2})"
                fi
            done
        else
            echo "  No backups found"
        fi
        echo ""
        echo "Usage: $0 --rollback <backup_directory>"
        echo "Example: $0 --rollback backup_20260129_180000"
        return
    fi
    
    local backup_dir="$1"
    if [[ ! -d "$backup_dir" ]]; then
        echo "✗ Error: Backup directory not found: $backup_dir"
        log_message "ERROR: Rollback failed - backup not found: $backup_dir"
        return 1
    fi
    
    echo "Rolling back from: $backup_dir"
    log_message "Starting rollback from: $backup_dir"
    
    # Copy files back
    cp -rf "$backup_dir"/* . 2>/dev/null
    
    # If user data was backed up separately, restore it too
    if [[ -d "$backup_dir/user_data_preserve" ]]; then
        echo "Restoring user data from backup..."
        if [[ -d "$backup_dir/user_data_preserve/data" ]]; then
            rm -rf "data" 2>/dev/null
            cp -r "$backup_dir/user_data_preserve/data" "." 2>/dev/null
        fi
        if [[ -d "$backup_dir/user_data_preserve/logs" ]]; then
            rm -rf "logs" 2>/dev/null
            cp -r "$backup_dir/user_data_preserve/logs" "." 2>/dev/null
        fi
    fi
    
    echo "✓ Rollback completed!"
    log_message "Rollback completed successfully from: $backup_dir"
    echo ""
}

# ============================================
# CLEANUP OLD BACKUPS
# ============================================
cleanup_old_backups() {
    echo "Cleaning up old backups (older than $PRESERVE_BACKUPS_DAYS days)..."
    log_message "Starting cleanup of backups older than $PRESERVE_BACKUPS_DAYS days"
    
    local cleaned=0
    for backup in backup_*; do
        if [[ -d "$backup" ]]; then
            # Check if backup is older than specified days
            if [[ $(find "$backup" -maxdepth 0 -mtime +$PRESERVE_BACKUPS_DAYS 2>/dev/null) ]]; then
                echo "  Removing old backup: $backup"
                rm -rf "$backup" 2>/dev/null
                log_message "Removed old backup: $backup"
                ((cleaned++))
            fi
        fi
    done
    
    if [[ $cleaned -gt 0 ]]; then
        echo "  ✓ Cleaned up $cleaned old backup(s)"
    else
        echo "  No old backups to clean up"
    fi
}

# ============================================
# AUTO-UPDATE FUNCTION WITH USER DATA PROTECTION
# ============================================
check_and_update() {
    # Check if update checking is disabled
    if [[ "$AUTO_UPDATE_CHECK" != "true" ]]; then
        echo "Auto-update check is disabled in config."
        log_message "Update check skipped - disabled in config"
        return
    fi
    
    # Check throttling - only check once per interval
    local last_check_file=".last_update_check"
    local current_time=$(date +%s)
    
    if [[ -f "$last_check_file" ]]; then
        local last_check=$(cat "$last_check_file" 2>/dev/null || echo "0")
        local time_diff=$((current_time - last_check))
        
        if [[ $time_diff -lt $UPDATE_CHECK_INTERVAL ]]; then
            local hours_left=$(( (UPDATE_CHECK_INTERVAL - time_diff) / 3600 ))
            echo "Update check performed recently (next check in ~${hours_left}h), skipping..."
            log_message "Update check skipped - last check was $time_diff seconds ago"
            return
        fi
    fi
    
    echo
    echo "============================================"
    echo "Checking for updates..."
    echo "============================================"
    log_message "Starting update check"

    # Check if curl is available
    if ! command -v curl &> /dev/null; then
        echo "curl not available, skipping update check."
        echo "(Install curl with: brew install curl)"
        echo
        sleep 2
        log_message "Update check failed - curl not available"
        return
    fi

    # Check if unzip is available
    if ! command -v unzip &> /dev/null; then
        echo "unzip not available, cannot auto-update."
        echo "(Install unzip with: brew install unzip)"
        echo
        sleep 2
        log_message "Update check failed - unzip not available"
        return
    fi

    # Get current version
    CURRENT_VERSION="unknown"
    if [[ -f "version.txt" ]]; then
        CURRENT_VERSION=$(cat version.txt | tr -d '\n\r')
    fi
    echo "Current version: $CURRENT_VERSION"

    # Create temp file for latest version
    TEMP_VERSION_FILE="/tmp/simcricketx_latest_version.txt"

    # Download latest version from GitHub
    echo "Checking latest version from GitHub..."
    if curl -s -f "https://raw.githubusercontent.com/$GITHUB_USER/$GITHUB_REPO/$MAIN_BRANCH/version.txt" -o "$TEMP_VERSION_FILE" 2>/dev/null; then
        # Read latest version
        if [[ -f "$TEMP_VERSION_FILE" ]]; then
            LATEST_VERSION=$(cat "$TEMP_VERSION_FILE" | tr -d '\n\r')
            rm "$TEMP_VERSION_FILE" 2>/dev/null
        else
            echo "Could not determine latest version."
            echo
            sleep 2
            log_message "Update check failed - could not read version file"
            return
        fi
    else
        echo "Could not check for updates."
        echo "(Check your internet connection or repository settings)"
        echo
        sleep 2
        log_message "Update check failed - network error"
        return
    fi

    if [[ -z "$LATEST_VERSION" ]]; then
        echo "Could not determine latest version."
        echo
        sleep 2
        log_message "Update check failed - empty version"
        return
    fi

    echo "Latest version: $LATEST_VERSION"
    
    # Save the check timestamp
    echo "$current_time" > "$last_check_file"
    log_message "Current: $CURRENT_VERSION, Latest: $LATEST_VERSION"

    # Compare versions
    if [[ "$CURRENT_VERSION" == "$LATEST_VERSION" ]]; then
        echo "✓ You have the latest version!"
        echo
        sleep 2
        log_message "Already on latest version"
    else
        echo
        echo "============================================"
        echo "   🚀 UPDATE AVAILABLE!"
        echo "============================================"
        echo "Current version: $CURRENT_VERSION"
        echo "Latest version:  $LATEST_VERSION"
        echo
        echo "A newer version is available on GitHub!"
        echo
        echo "What would you like to do?"
        echo "[1] Continue with current version"
        echo "[2] AUTO-UPDATE: Download and install latest version"
        echo "[3] Open GitHub page manually"
        echo "[4] Exit to update manually"
        echo
        
        log_message "Update available - prompting user"
        read -p "Enter your choice (1-4) [default: 2]: " choice
        
        if [[ -z "$choice" ]]; then
            choice="2"
        fi
        
        log_message "User choice: $choice"
        case $choice in
            2)
                echo
                echo "============================================"
                echo "   🔄 AUTO-UPDATING..."
                echo "============================================"
                log_message "Starting auto-update"
                
                # Create backup directory with timestamp
                BACKUP_DIR="backup_$(date +%Y%m%d_%H%M%S)"
                echo "Creating backup in: $BACKUP_DIR"
                mkdir -p "$BACKUP_DIR"
                log_message "Created backup directory: $BACKUP_DIR"
                
                # Backup important files (excluding user data)
                echo "Backing up current files..."
                for file in *.py *.txt *.html *.sh *.bat templates/ static/ config/ engine/ utils/; do
                    if [[ -e "$file" ]]; then
                        cp -r "$file" "$BACKUP_DIR/" 2>/dev/null
                        echo "  ✓ Backed up: $file"
                    fi
                done
                
                # Special backup for user data that must be preserved
                echo "Preserving user data..."
                USER_DATA_BACKUP="$BACKUP_DIR/user_data_preserve"
                mkdir -p "$USER_DATA_BACKUP"
                
                # Preserve auth credentials
                if [[ -f "auth/credentials.json" ]]; then
                    mkdir -p "$USER_DATA_BACKUP/auth"
                    cp "auth/credentials.json" "$USER_DATA_BACKUP/auth/" 2>/dev/null
                    echo "  🔒 Preserved: auth/credentials.json"
                fi
                
                if [[ -f "auth/encryption.key" ]]; then
                    mkdir -p "$USER_DATA_BACKUP/auth"
                    cp "auth/encryption.key" "$USER_DATA_BACKUP/auth/" 2>/dev/null
                    echo "  🔒 Preserved: auth/encryption.key"
                fi
                
                # Preserve entire data folder
                if [[ -d "data" ]]; then
                    cp -r "data" "$USER_DATA_BACKUP/" 2>/dev/null
                    echo "  🔒 Preserved: data/ (entire folder)"
                fi
                
                # Preserve entire logs folder
                if [[ -d "logs" ]]; then
                    cp -r "logs" "$USER_DATA_BACKUP/" 2>/dev/null
                    echo "  🔒 Preserved: logs/ (entire folder)"
                fi
                
                # Preserve root log files
                if [[ -f "user_auth.log" ]]; then
                    cp "user_auth.log" "$USER_DATA_BACKUP/" 2>/dev/null
                    echo "  🔒 Preserved: user_auth.log"
                fi
                
                if [[ -f "auth_debug.log" ]]; then
                    cp "auth_debug.log" "$USER_DATA_BACKUP/" 2>/dev/null
                    echo "  🔒 Preserved: auth_debug.log"
                fi
                
                # Download latest ZIP
                TEMP_ZIP="/tmp/simcricketx_latest.zip"
                echo
                echo "Downloading latest version..."
                log_message "Downloading from GitHub"
                if curl -L -o "$TEMP_ZIP" "https://github.com/$GITHUB_USER/$GITHUB_REPO/archive/refs/heads/$MAIN_BRANCH.zip"; then
                    echo "✓ Download completed!"
                    log_message "Download completed"
                else
                    echo "✗ Download failed!"
                    echo "Restore from backup if needed: cp -r $BACKUP_DIR/* ."
                    echo
                    log_message "ERROR: Download failed"
                    read -p "Press Enter to continue..."
                    return
                fi
                
                # Verify download integrity
                echo "Verifying download integrity..."
                if ! unzip -t "$TEMP_ZIP" &> /dev/null; then
                    echo "✗ Downloaded file is corrupted!"
                    rm -f "$TEMP_ZIP" 2>/dev/null
                    echo "Restore from backup if needed: cp -r $BACKUP_DIR/* ."
                    echo
                    log_message "ERROR: Downloaded file is corrupted"
                    read -p "Press Enter to continue..."
                    return
                fi
                echo "✓ Download verified!"
                log_message "Download integrity verified"
                
                # Extract to temporary directory
                TEMP_EXTRACT="/tmp/simcricketx_extract"
                rm -rf "$TEMP_EXTRACT" 2>/dev/null
                mkdir -p "$TEMP_EXTRACT"
                
                echo "Extracting files..."
                if unzip -q "$TEMP_ZIP" -d "$TEMP_EXTRACT"; then
                    echo "✓ Extraction completed!"
                    log_message "Extraction completed"
                else
                    echo "✗ Extraction failed!"
                    rm -f "$TEMP_ZIP" 2>/dev/null
                    echo "Restore from backup if needed: cp -r $BACKUP_DIR/* ."
                    echo
                    log_message "ERROR: Extraction failed"
                    read -p "Press Enter to continue..."
                    return
                fi
                
                # Find the extracted folder (GitHub creates [repo-name]-[branch]/)
                EXTRACTED_FOLDER="$TEMP_EXTRACT/$GITHUB_REPO-$MAIN_BRANCH"
                if [[ ! -d "$EXTRACTED_FOLDER" ]]; then
                    # Try alternative naming
                    EXTRACTED_FOLDER="$TEMP_EXTRACT/$GITHUB_REPO-master"
                    if [[ ! -d "$EXTRACTED_FOLDER" ]]; then
                        echo "✗ Could not find extracted folder!"
                        echo "Contents of extract directory:"
                        ls -la "$TEMP_EXTRACT"
                        rm -f "$TEMP_ZIP" 2>/dev/null
                        rm -rf "$TEMP_EXTRACT" 2>/dev/null
                        echo
                        log_message "ERROR: Could not find extracted folder"
                        read -p "Press Enter to continue..."
                        return
                    fi
                fi
                
                # Copy new files to current directory
                echo "Installing new files..."
                cd "$EXTRACTED_FOLDER"
                for item in *; do
                    if [[ -e "$item" ]]; then
                        cp -rf "$item" "$OLDPWD/"
                        echo "  ✓ Updated: $item"
                    fi
                done
                cd "$OLDPWD"
                log_message "New files installed"
                
                # Restore preserved user data (CRITICAL - overwrite any updated versions)
                echo "Restoring preserved user data..."
                if [[ -d "$USER_DATA_BACKUP" ]]; then
                    # Restore auth files
                    if [[ -f "$USER_DATA_BACKUP/auth/credentials.json" ]]; then
                        mkdir -p "auth"
                        cp "$USER_DATA_BACKUP/auth/credentials.json" "auth/" 2>/dev/null
                        echo "  🔒 Restored: auth/credentials.json"
                    fi
                    
                    if [[ -f "$USER_DATA_BACKUP/auth/encryption.key" ]]; then
                        mkdir -p "auth"
                        cp "$USER_DATA_BACKUP/auth/encryption.key" "auth/" 2>/dev/null
                        echo "  🔒 Restored: auth/encryption.key"
                    fi
                    
                    # Restore entire data folder
                    if [[ -d "$USER_DATA_BACKUP/data" ]]; then
                        rm -rf "data" 2>/dev/null
                        cp -r "$USER_DATA_BACKUP/data" "." 2>/dev/null
                        echo "  🔒 Restored: data/ (entire folder)"
                    fi
                    
                    # Restore entire logs folder
                    if [[ -d "$USER_DATA_BACKUP/logs" ]]; then
                        rm -rf "logs" 2>/dev/null
                        cp -r "$USER_DATA_BACKUP/logs" "." 2>/dev/null
                        echo "  🔒 Restored: logs/ (entire folder)"
                    fi
                    
                    # Restore root log files
                    if [[ -f "$USER_DATA_BACKUP/user_auth.log" ]]; then
                        cp "$USER_DATA_BACKUP/user_auth.log" "." 2>/dev/null
                        echo "  🔒 Restored: user_auth.log"
                    fi
                    
                    if [[ -f "$USER_DATA_BACKUP/auth_debug.log" ]]; then
                        cp "$USER_DATA_BACKUP/auth_debug.log" "." 2>/dev/null
                        echo "  🔒 Restored: auth_debug.log"
                    fi
                fi
                log_message "User data restored"
                
                # Cleanup
                rm -f "$TEMP_ZIP" 2>/dev/null
                rm -rf "$TEMP_EXTRACT" 2>/dev/null
                
                # Cleanup old backups
                cleanup_old_backups
                
                echo
                echo "============================================"
                echo "   ✅ UPDATE COMPLETED!"
                echo "============================================"
                echo "Updated to version: $LATEST_VERSION"
                echo "Backup saved in: $BACKUP_DIR"
                echo
                echo "🔒 USER DATA PROTECTION SUMMARY:"
                echo "  ✅ auth/credentials.json - PRESERVED"
                echo "  ✅ auth/encryption.key - PRESERVED"
                echo "  ✅ data/ folder - PRESERVED"
                echo "  ✅ logs/ folder - PRESERVED"
                echo "  ✅ user_auth.log - PRESERVED"
                echo "  ✅ auth_debug.log - PRESERVED"
                echo
                echo "If anything goes wrong, restore with:"
                echo "  cp -r $BACKUP_DIR/* ."
                echo "  Or use: $0 --rollback $BACKUP_DIR"
                echo
                log_message "Update completed successfully to version: $LATEST_VERSION"
                sleep 3
                ;;
            3)
                echo "Opening GitHub repository..."
                if command -v open &> /dev/null; then
                    open "https://github.com/$GITHUB_USER/$GITHUB_REPO"
                elif command -v xdg-open &> /dev/null; then
                    xdg-open "https://github.com/$GITHUB_USER/$GITHUB_REPO"
                else
                    echo "Please visit: https://github.com/$GITHUB_USER/$GITHUB_REPO"
                fi
                echo
                echo "Please download the latest version from GitHub."
                echo
                log_message "User chose to open GitHub manually"
                read -p "Press Enter to continue..."
                ;;
            4)
                echo
                echo "Please download the latest version from:"
                echo "https://github.com/$GITHUB_USER/$GITHUB_REPO"
                echo
                echo "After updating, run this script again."
                echo
                log_message "User chose to exit and update manually"
                read -p "Press Enter to exit..."
                exit 0
                ;;
            1|*)
                echo "Continuing with current version..."
                echo
                log_message "User chose to continue with current version"
                sleep 2
                ;;
        esac
    fi
}

# ============================================
# FORCE UPDATE FUNCTION (if called with --update)
# ============================================
force_update() {
    echo
    echo "============================================"
    echo "   🔄 FORCE UPDATE MODE"
    echo "============================================"
    log_message "Force update initiated"
    
    # Check requirements
    if ! command -v curl &> /dev/null; then
        echo "ERROR: curl not available. Install with: brew install curl"
        log_message "ERROR: Force update failed - curl not available"
        exit 1
    fi
    
    if ! command -v unzip &> /dev/null; then
        echo "ERROR: unzip not available. Install with: brew install unzip"
        log_message "ERROR: Force update failed - unzip not available"
        exit 1
    fi
    
    echo "Force updating to latest version..."
    
    # Create backup
    BACKUP_DIR="backup_$(date +%Y%m%d_%H%M%S)"
    echo "Creating backup in: $BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    log_message "Created backup: $BACKUP_DIR"
    
    for file in *.py *.txt *.html *.sh *.bat templates/ static/ config/ engine/ utils/; do
        if [[ -e "$file" ]]; then
            cp -r "$file" "$BACKUP_DIR/" 2>/dev/null
        fi
    done
    
    # Special backup for user data that must be preserved
    echo "Preserving user data..."
    USER_DATA_BACKUP="$BACKUP_DIR/user_data_preserve"
    mkdir -p "$USER_DATA_BACKUP"
    
    # Preserve all protected files/folders
    [[ -f "auth/credentials.json" ]] && { mkdir -p "$USER_DATA_BACKUP/auth"; cp "auth/credentials.json" "$USER_DATA_BACKUP/auth/" 2>/dev/null; }
    [[ -f "auth/encryption.key" ]] && { mkdir -p "$USER_DATA_BACKUP/auth"; cp "auth/encryption.key" "$USER_DATA_BACKUP/auth/" 2>/dev/null; }
    [[ -d "data" ]] && cp -r "data" "$USER_DATA_BACKUP/" 2>/dev/null
    [[ -d "logs" ]] && cp -r "logs" "$USER_DATA_BACKUP/" 2>/dev/null
    [[ -f "user_auth.log" ]] && cp "user_auth.log" "$USER_DATA_BACKUP/" 2>/dev/null
    [[ -f "auth_debug.log" ]] && cp "auth_debug.log" "$USER_DATA_BACKUP/" 2>/dev/null
    
    # Download and install
    TEMP_ZIP="/tmp/simcricketx_latest.zip"
    echo "Downloading..."
    curl -L -o "$TEMP_ZIP" "https://github.com/$GITHUB_USER/$GITHUB_REPO/archive/refs/heads/$MAIN_BRANCH.zip"
    
    # Verify download
    echo "Verifying download..."
    if ! unzip -t "$TEMP_ZIP" &> /dev/null; then
        echo "✗ Downloaded file is corrupted!"
        rm -f "$TEMP_ZIP" 2>/dev/null
        log_message "ERROR: Force update - corrupted download"
        exit 1
    fi
    
    TEMP_EXTRACT="/tmp/simcricketx_extract"
    rm -rf "$TEMP_EXTRACT" 2>/dev/null
    mkdir -p "$TEMP_EXTRACT"
    
    echo "Extracting..."
    unzip -q "$TEMP_ZIP" -d "$TEMP_EXTRACT"
    
    EXTRACTED_FOLDER="$TEMP_EXTRACT/$GITHUB_REPO-$MAIN_BRANCH"
    if [[ ! -d "$EXTRACTED_FOLDER" ]]; then
        EXTRACTED_FOLDER="$TEMP_EXTRACT/$GITHUB_REPO-master"
    fi
    
    echo "Installing..."
    cd "$EXTRACTED_FOLDER"
    cp -rf * "$OLDPWD/"
    cd "$OLDPWD"
    
    # Restore preserved user data
    echo "Restoring user data..."
    if [[ -d "$USER_DATA_BACKUP" ]]; then
        [[ -f "$USER_DATA_BACKUP/auth/credentials.json" ]] && { mkdir -p "auth"; cp "$USER_DATA_BACKUP/auth/credentials.json" "auth/" 2>/dev/null; }
        [[ -f "$USER_DATA_BACKUP/auth/encryption.key" ]] && { mkdir -p "auth"; cp "$USER_DATA_BACKUP/auth/encryption.key" "auth/" 2>/dev/null; }
        [[ -d "$USER_DATA_BACKUP/data" ]] && { rm -rf "data" 2>/dev/null; cp -r "$USER_DATA_BACKUP/data" "." 2>/dev/null; }
        [[ -d "$USER_DATA_BACKUP/logs" ]] && { rm -rf "logs" 2>/dev/null; cp -r "$USER_DATA_BACKUP/logs" "." 2>/dev/null; }
        [[ -f "$USER_DATA_BACKUP/user_auth.log" ]] && cp "$USER_DATA_BACKUP/user_auth.log" "." 2>/dev/null
        [[ -f "$USER_DATA_BACKUP/auth_debug.log" ]] && cp "$USER_DATA_BACKUP/auth_debug.log" "." 2>/dev/null
    fi
    
    # Cleanup
    rm -f "$TEMP_ZIP" 2>/dev/null
    rm -rf "$TEMP_EXTRACT" 2>/dev/null
    
    # Cleanup old backups
    cleanup_old_backups
    
    echo "✅ Force update completed!"
    echo "Backup saved in: $BACKUP_DIR"
    echo
    echo "🔒 USER DATA PROTECTED:"
    echo "  ✅ auth/credentials.json"
    echo "  ✅ auth/encryption.key"
    echo "  ✅ data/ folder"
    echo "  ✅ logs/ folder"
    echo "  ✅ user_auth.log"
    echo "  ✅ auth_debug.log"
    echo
    log_message "Force update completed successfully"
}

# ============================================
# CHECK PORT AVAILABILITY
# ============================================
check_port_availability() {
    if [[ "$CHECK_PORT_BEFORE_START" != "true" ]]; then
        return 0
    fi
    
    echo "Checking if port $DEFAULT_PORT is available..."
    
    # Check using lsof if available
    if command -v lsof &> /dev/null; then
        if lsof -i:$DEFAULT_PORT &> /dev/null; then
            echo "⚠️  WARNING: Port $DEFAULT_PORT is already in use!"
            echo "The application may fail to start."
            echo
            log_message "WARNING: Port $DEFAULT_PORT is already in use"
            read -p "Continue anyway? (y/n): " continue
            if [[ "$continue" != "y" && "$continue" != "Y" ]]; then
                echo "Exiting..."
                log_message "User chose to exit due to port conflict"
                exit 1
            fi
        else
            echo "✓ Port $DEFAULT_PORT is available"
        fi
    # Fallback to netstat
    elif command -v netstat &> /dev/null; then
        if netstat -an | grep -w "$DEFAULT_PORT" | grep LISTEN &> /dev/null; then
            echo "⚠️  WARNING: Port $DEFAULT_PORT appears to be in use!"
            echo "The application may fail to start."
            echo
            log_message "WARNING: Port $DEFAULT_PORT appears to be in use"
            read -p "Continue anyway? (y/n): " continue
            if [[ "$continue" != "y" && "$continue" != "Y" ]]; then
                echo "Exiting..."
                log_message "User chose to exit due to port conflict"
                exit 1
            fi
        else
            echo "✓ Port $DEFAULT_PORT appears available"
        fi
    else
        echo "Cannot check port availability (lsof/netstat not found)"
    fi
}

# ============================================
# MAIN SCRIPT LOGIC
# ============================================

# Load config file if it exists
CONFIG_FILE=".simcricketx.conf"
if [[ -f "$CONFIG_FILE" ]]; then
    echo "Loading configuration from $CONFIG_FILE..."
    source "$CONFIG_FILE"
    log_message "Loaded configuration from $CONFIG_FILE"
fi

# Handle command line arguments
if [[ "$1" == "--update" ]]; then
    force_update
    exit 0
elif [[ "$1" == "--rollback" ]]; then
    rollback_update "$2"
    exit 0
elif [[ "$1" == "--help" ]]; then
    echo "Usage: $0 [options]"
    echo "Options:"
    echo "  --update              Force update to latest version"
    echo "  --rollback [backup]   Rollback to a previous backup"
    echo "  --skip-update         Skip update check"
    echo "  --help                Show this help"
    echo ""
    echo "Configuration:"
    echo "  Create a .simcricketx.conf file to customize behavior"
    exit 0
fi

log_message "========== Starting SimCricketX =========="

echo "============================================"
echo "Starting SimCricketX Flask App..."
echo "============================================"
echo

# Change to script directory
cd "$(dirname "$0")"
echo "Current directory: $(pwd)"
echo

# Check dependencies first
if ! check_dependencies; then
    read -p "Press Enter to exit..."
    exit 1
fi
echo

# Check for updates (unless skipped)
if [[ "$1" != "--skip-update" ]]; then
    check_and_update
fi

# Show what files are present
echo "Files in directory:"
ls -la *.py *.txt 2>/dev/null || echo "No .py or .txt files found!"
echo

# Set terminal title
echo -ne "\033]0;SimCricketX Flask App\007"

# Check for Python
echo "Checking for Python..."
if ! command -v python3 &> /dev/null; then
    if ! command -v python &> /dev/null; then
        echo
        echo "ERROR: Python is not found in your system's PATH."
        echo "Please install Python and make sure it's accessible."
        echo
        echo "To fix this:"
        echo "1. Install Python from python.org or use Homebrew: brew install python"
        echo "2. Make sure Python is in your PATH"
        echo "3. Try running 'python3' instead of 'python'"
        echo
        log_message "ERROR: Python not found"
        read -p "Press Enter to exit..."
        exit 1
    else
        PYTHON_CMD="python"
    fi
else
    PYTHON_CMD="python3"
fi

echo "Python found successfully! Using: $PYTHON_CMD"
$PYTHON_CMD --version
echo


# Check for required files
echo "Checking for requirements.txt..."
if [[ ! -f "requirements.txt" ]]; then
    echo
    echo "ERROR: requirements.txt not found in: $(pwd)"
    echo
    echo "Directory contents:"
    ls -la
    echo
    echo "Make sure this script is in the same folder as requirements.txt"
    echo "Or run with --update to download latest files"
    echo
    log_message "ERROR: requirements.txt not found"
    read -p "Press Enter to exit..."
    exit 1
fi
echo "requirements.txt found!"

echo "Checking for app.py..."
if [[ ! -f "app.py" ]]; then
    echo
    echo "ERROR: app.py not found in: $(pwd)"
    echo "Make sure this script is in the same folder as app.py"
    echo "Or run with --update to download latest files"
    echo
    log_message "ERROR: app.py not found"
    read -p "Press Enter to exit..."
    exit 1
fi
echo "app.py found!"

echo
echo "All checks passed! Continuing with setup..."
echo


# Create virtual environment
if [[ ! -d "venv" ]]; then
    echo "Creating Python virtual environment..."
    $PYTHON_CMD -m venv venv
    if [[ $? -ne 0 ]]; then
        echo
        echo "ERROR: Failed to create virtual environment."
        echo "This might be due to:"
        echo "- Insufficient permissions"
        echo "- Missing venv module (try: pip install virtualenv)"
        echo "- Corrupted Python installation"
        echo
        log_message "ERROR: Failed to create virtual environment"
        read -p "Press Enter to exit..."
        exit 1
    fi
    echo "Virtual environment created!"
    log_message "Virtual environment created"
else
    echo "Virtual environment already exists."
fi

echo
echo "Activating virtual environment..."
source venv/bin/activate
if [[ $? -ne 0 ]]; then
    echo
    echo "ERROR: Failed to activate virtual environment."
    echo "Try deleting the 'venv' folder and running again."
    echo
    log_message "ERROR: Failed to activate virtual environment"
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Virtual environment activated!"
echo


echo "Upgrading pip..."
python -m pip install --upgrade pip
echo

# Check if requirements have changed
REQUIREMENTS_HASH_FILE="venv/.requirements_hash"
SKIP_INSTALL=false

if command -v md5sum &> /dev/null; then
    CURRENT_HASH=$(md5sum requirements.txt | cut -d' ' -f1)
elif command -v md5 &> /dev/null; then
    CURRENT_HASH=$(md5 -q requirements.txt)
else
    CURRENT_HASH=""
fi

if [[ -n "$CURRENT_HASH" ]] && [[ -f "$REQUIREMENTS_HASH_FILE" ]]; then
    LAST_HASH=$(cat "$REQUIREMENTS_HASH_FILE" 2>/dev/null)
    if [[ "$CURRENT_HASH" == "$LAST_HASH" ]]; then
        echo "Dependencies unchanged since last install, skipping..."
        echo "(Delete venv/.requirements_hash to force reinstall)"
        SKIP_INSTALL=true
        log_message "Skipped dependency installation - requirements unchanged"
    fi
fi

if [[ "$SKIP_INSTALL" != "true" ]]; then
    echo "Installing dependencies..."
    pip install --no-cache-dir -r requirements.txt
    if [[ $? -ne 0 ]]; then
        echo
        echo "ERROR: Failed to install dependencies."
        echo "Check your internet connection and requirements.txt content."
        echo
        log_message "ERROR: Failed to install dependencies"
        read -p "Press Enter to exit..."
        exit 1
    fi
    
    # Save the hash for next time
    if [[ -n "$CURRENT_HASH" ]]; then
        echo "$CURRENT_HASH" > "$REQUIREMENTS_HASH_FILE"
        log_message "Dependencies installed and hash saved"
    fi
fi

echo

# Check port availability
check_port_availability

echo
echo "======================================================="
echo "  Starting Flask Application..."
echo "  Access it at: http://127.0.0.1:$DEFAULT_PORT"
echo "  Press CTRL+C to stop the server"
echo "======================================================="
echo

log_message "Starting Flask application on port $DEFAULT_PORT"
python app.py

echo
echo "Application stopped."
log_message "Application stopped"
read -p "Press Enter to exit..."