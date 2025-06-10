@echo off
echo ============================================
echo Starting SimCricketX Flask App...
echo ============================================
echo.

REM Force the window to stay open on any error
set "PAUSE_ON_ERROR=1"

REM Change to script directory
cd /d "%~dp0"
echo Current directory: %CD%
echo.

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