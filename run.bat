@echo off
echo ============================================
echo Starting SimCricketX Flask App...
echo ============================================
echo.

REM ========== CONFIGURATION ==========
REM UPDATE THESE VALUES FOR YOUR REPOSITORY
set "GITHUB_USER=Manishrdy"
set "GITHUB_REPO=SimCricketX"
REM ===================================

REM Force the window to stay open on any error
set "PAUSE_ON_ERROR=1"

REM Change to script directory
cd /d "%~dp0"
echo Current directory: %CD%
echo.

REM Check for updates (can be skipped with --skip-update argument)
if not "%1"=="--skip-update" (
    call :check_for_updates
)

REM Show what files are present
echo Files in directory:
dir /b *.py *.txt 2>nul
if %errorlevel% neq 0 (
    echo No .py or .txt files found!
)
echo.

title Run SimCricketX Flask App

REM Test Python with detailed output
echo Checking for Python...
python --version 2>&1
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Python is not found in your system's PATH.
    echo Please install Python and make sure it's added to your PATH.
    echo.
    echo To fix this:
    echo 1. Download Python from python.org
    echo 2. During installation, check "Add Python to PATH"
    echo 3. Restart your computer
    echo.
    pause
    exit /b 1
)

echo Python found successfully!
echo.

REM Check for required files with detailed feedback
echo Checking for requirements.txt...
if not exist "requirements.txt" (
    echo.
    echo ERROR: requirements.txt not found in: %CD%
    echo.
    echo Directory contents:
    dir /b
    echo.
    echo Make sure this batch file is in the same folder as requirements.txt
    echo.
    pause
    exit /b 1
)
echo requirements.txt found!

echo Checking for app.py...
if not exist "app.py" (
    echo.
    echo ERROR: app.py not found in: %CD%
    echo Make sure this batch file is in the same folder as app.py
    echo.
    pause
    exit /b 1
)
echo app.py found!

echo.
echo All checks passed! Continuing with setup...
echo.

REM Create virtual environment
if not exist "venv\" (
    echo Creating Python virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo.
        echo ERROR: Failed to create virtual environment.
        echo This might be due to:
        echo - Insufficient permissions
        echo - Antivirus blocking the operation
        echo - Corrupted Python installation
        echo.
        pause
        exit /b 1
    )
    echo Virtual environment created!
) else (
    echo Virtual environment already exists.
)

echo.
echo Activating virtual environment...
call "venv\Scripts\activate.bat"
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to activate virtual environment.
    echo Try deleting the 'venv' folder and running again.
    echo.
    pause
    exit /b 1
)

echo Virtual environment activated!
echo.

echo Upgrading pip...
python -m pip install --upgrade pip
echo.

echo Installing dependencies...
pip install --no-cache-dir -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to install dependencies.
    echo Check your internet connection and requirements.txt content.
    echo.
    pause
    exit /b 1
)

echo.
echo =======================================================
echo  Starting Flask Application...
echo  Access it at: http://127.0.0.1:7860
echo  Press CTRL+C to stop the server
echo =======================================================
echo.

python app.py

echo.
echo Application stopped.
pause
exit /b 0

REM ============================================
REM UPDATE CHECKER FUNCTION
REM ============================================
:check_for_updates
echo.
echo ============================================
echo Checking for updates...
echo ============================================

REM Check if we can access the internet and curl is available
curl --version >nul 2>&1
if %errorlevel% neq 0 (
    echo curl not available, skipping update check.
    echo (Update checking requires Windows 10 or newer)
    echo.
    timeout /t 2 >nul
    goto :eof
)

REM Get current version
set "CURRENT_VERSION=unknown"
if exist "version.txt" (
    set /p CURRENT_VERSION=<"version.txt"
)
echo Current version: %CURRENT_VERSION%

REM Create temp file for latest version
set "TEMP_VERSION_FILE=%TEMP%\simcricketx_latest_version.txt"

REM Download latest version from GitHub
echo Checking latest version from GitHub...
curl -s -f "https://raw.githubusercontent.com/%GITHUB_USER%/%GITHUB_REPO%/main/version.txt" -o "%TEMP_VERSION_FILE%" 2>nul

if %errorlevel% neq 0 (
    echo Could not check for updates.
    echo (Check your internet connection or repository settings)
    echo.
    timeout /t 2 >nul
    goto :eof
)

REM Read latest version
set "LATEST_VERSION="
if exist "%TEMP_VERSION_FILE%" (
    set /p LATEST_VERSION=<"%TEMP_VERSION_FILE%"
    del "%TEMP_VERSION_FILE%" 2>nul
)

if "%LATEST_VERSION%"=="" (
    echo Could not determine latest version.
    echo.
    timeout /t 2 >nul
    goto :eof
)

echo Latest version: %LATEST_VERSION%

REM Compare versions
if "%CURRENT_VERSION%"=="%LATEST_VERSION%" (
    echo ✓ You have the latest version!
    echo.
    timeout /t 2 >nul
) else (
    echo.
    echo ============================================
    echo   🚀 UPDATE AVAILABLE!
    echo ============================================
    echo Current version: %CURRENT_VERSION%
    echo Latest version:  %LATEST_VERSION%
    echo.
    echo A newer version is available on GitHub!
    echo.
    echo What would you like to do?
    echo [1] Continue with current version
    echo [2] Open GitHub page to download latest
    echo [3] Exit to update manually
    echo.
    
    set /p "choice=Enter your choice (1-3) [default: 1]: "
    
    if "%choice%"=="" set "choice=1"
    
    if "%choice%"=="2" (
        echo Opening GitHub repository...
        start https://github.com/%GITHUB_USER%/%GITHUB_REPO%
        echo.
        echo Please download the latest version from GitHub.
        echo After downloading, extract and replace your current files.
        echo.
        pause
    )
    
    if "%choice%"=="3" (
        echo.
        echo Please download the latest version from:
        echo https://github.com/%GITHUB_USER%/%GITHUB_REPO%
        echo.
        echo After updating, run this script again.
        echo.
        pause
        exit /b 0
    )
    
    if "%choice%"=="1" (
        echo Continuing with current version...
        echo.
        timeout /t 2 >nul
    )
)
goto :eof