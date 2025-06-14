#!/bin/bash

# ========== CONFIGURATION ==========
# UPDATE THESE VALUES FOR YOUR REPOSITORY
GITHUB_USER="Manishrdy"
GITHUB_REPO="SimCricketX"
MAIN_BRANCH="main"  # Change to "master" if your default branch is master
# ===================================

# ============================================
# AUTO-UPDATE FUNCTION WITH DOWNLOAD
# ============================================
check_and_update() {
    echo
    echo "============================================"
    echo "Checking for updates..."
    echo "============================================"

    # Check if curl is available
    if ! command -v curl &> /dev/null; then
        echo "curl not available, skipping update check."
        echo "(Install curl with: brew install curl)"
        echo
        sleep 2
        return
    fi

    # Check if unzip is available
    if ! command -v unzip &> /dev/null; then
        echo "unzip not available, cannot auto-update."
        echo "(Install unzip with: brew install unzip)"
        echo
        sleep 2
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
            return
        fi
    else
        echo "Could not check for updates."
        echo "(Check your internet connection or repository settings)"
        echo
        sleep 2
        return
    fi

    if [[ -z "$LATEST_VERSION" ]]; then
        echo "Could not determine latest version."
        echo
        sleep 2
        return
    fi

    echo "Latest version: $LATEST_VERSION"

    # Compare versions
    if [[ "$CURRENT_VERSION" == "$LATEST_VERSION" ]]; then
        echo "✓ You have the latest version!"
        echo
        sleep 2
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
        
        read -p "Enter your choice (1-4) [default: 2]: " choice
        
        if [[ -z "$choice" ]]; then
            choice="2"
        fi
        
        case $choice in
            2)
                echo
                echo "============================================"
                echo "   🔄 AUTO-UPDATING..."
                echo "============================================"
                
                # Create backup directory with timestamp
                BACKUP_DIR="backup_$(date +%Y%m%d_%H%M%S)"
                echo "Creating backup in: $BACKUP_DIR"
                mkdir -p "$BACKUP_DIR"
                
                # Backup important files
                echo "Backing up current files..."
                for file in *.py *.txt *.html templates/ static/ data/ config/ auth/ engine/ utils/ logs/; do
                    if [[ -e "$file" ]]; then
                        cp -r "$file" "$BACKUP_DIR/" 2>/dev/null
                        echo "  ✓ Backed up: $file"
                    fi
                done
                
                # Download latest ZIP
                TEMP_ZIP="/tmp/simcricketx_latest.zip"
                echo
                echo "Downloading latest version..."
                if curl -L -o "$TEMP_ZIP" "https://github.com/$GITHUB_USER/$GITHUB_REPO/archive/refs/heads/$MAIN_BRANCH.zip"; then
                    echo "✓ Download completed!"
                else
                    echo "✗ Download failed!"
                    echo "Restore from backup if needed: cp -r $BACKUP_DIR/* ."
                    echo
                    read -p "Press Enter to continue..."
                    return
                fi
                
                # Extract to temporary directory
                TEMP_EXTRACT="/tmp/simcricketx_extract"
                rm -rf "$TEMP_EXTRACT" 2>/dev/null
                mkdir -p "$TEMP_EXTRACT"
                
                echo "Extracting files..."
                if unzip -q "$TEMP_ZIP" -d "$TEMP_EXTRACT"; then
                    echo "✓ Extraction completed!"
                else
                    echo "✗ Extraction failed!"
                    rm -f "$TEMP_ZIP" 2>/dev/null
                    echo "Restore from backup if needed: cp -r $BACKUP_DIR/* ."
                    echo
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
                
                # Cleanup
                rm -f "$TEMP_ZIP" 2>/dev/null
                rm -rf "$TEMP_EXTRACT" 2>/dev/null
                
                echo
                echo "============================================"
                echo "   ✅ UPDATE COMPLETED!"
                echo "============================================"
                echo "Updated to version: $LATEST_VERSION"
                echo "Backup saved in: $BACKUP_DIR"
                echo
                echo "If anything goes wrong, restore with:"
                echo "  cp -r $BACKUP_DIR/* ."
                echo
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
                read -p "Press Enter to continue..."
                ;;
            4)
                echo
                echo "Please download the latest version from:"
                echo "https://github.com/$GITHUB_USER/$GITHUB_REPO"
                echo
                echo "After updating, run this script again."
                echo
                read -p "Press Enter to exit..."
                exit 0
                ;;
            1|*)
                echo "Continuing with current version..."
                echo
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
    
    # Check requirements
    if ! command -v curl &> /dev/null; then
        echo "ERROR: curl not available. Install with: brew install curl"
        exit 1
    fi
    
    if ! command -v unzip &> /dev/null; then
        echo "ERROR: unzip not available. Install with: brew install unzip"
        exit 1
    fi
    
    echo "Force updating to latest version..."
    
    # Create backup
    BACKUP_DIR="backup_$(date +%Y%m%d_%H%M%S)"
    echo "Creating backup in: $BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    
    for file in *.py *.txt *.html templates/ static/ data/ config/ auth/ engine/ utils/ logs/; do
        if [[ -e "$file" ]]; then
            cp -r "$file" "$BACKUP_DIR/" 2>/dev/null
        fi
    done
    
    # Download and install
    TEMP_ZIP="/tmp/simcricketx_latest.zip"
    echo "Downloading..."
    curl -L -o "$TEMP_ZIP" "https://github.com/$GITHUB_USER/$GITHUB_REPO/archive/refs/heads/$MAIN_BRANCH.zip"
    
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
    
    # Cleanup
    rm -f "$TEMP_ZIP" 2>/dev/null
    rm -rf "$TEMP_EXTRACT" 2>/dev/null
    
    echo "✅ Force update completed!"
    echo "Backup saved in: $BACKUP_DIR"
    echo
}

# ============================================
# MAIN SCRIPT LOGIC
# ============================================

# Handle command line arguments
if [[ "$1" == "--update" ]]; then
    force_update
    exit 0
elif [[ "$1" == "--help" ]]; then
    echo "Usage: $0 [options]"
    echo "Options:"
    echo "  --update        Force update to latest version"
    echo "  --skip-update   Skip update check"
    echo "  --help          Show this help"
    exit 0
fi

echo "============================================"
echo "Starting SimCricketX Flask App..."
echo "============================================"
echo

# Change to script directory
cd "$(dirname "$0")"
echo "Current directory: $(pwd)"
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
        read -p "Press Enter to exit..."
        exit 1
    fi
    echo "Virtual environment created!"
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
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Virtual environment activated!"
echo

echo "Upgrading pip..."
python -m pip install --upgrade pip
echo

echo "Installing dependencies..."
pip install --no-cache-dir -r requirements.txt
if [[ $? -ne 0 ]]; then
    echo
    echo "ERROR: Failed to install dependencies."
    echo "Check your internet connection and requirements.txt content."
    echo
    read -p "Press Enter to exit..."
    exit 1
fi

echo
echo "======================================================="
echo "  Starting Flask Application..."
echo "  Access it at: http://127.0.0.1:7860"
echo "  Press CTRL+C to stop the server"
echo "======================================================="
echo

python app.py

echo
echo "Application stopped."
read -p "Press Enter to exit..."