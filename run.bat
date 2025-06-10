@echo off
REM ============================================================================
REM  Batch Script to Run the Python Flask Application
REM
REM  This script automates the following steps:
REM  1. Checks for Python and required files.
REM  2. Creates a Python virtual environment (`venv`) if it doesn't exist.
REM  3. Activates the virtual environment.
REM  4. Installs or updates dependencies from `requirements.txt`.
REM  5. Starts the Flask application (`app.py`).
REM ============================================================================

title Run SimCricketX Flask App

REM --- Step 1: Initial Checks ---
echo Checking for Python installation...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Python is not found in your system's PATH.
    echo Please install Python and make sure it's added to your PATH.
    echo.
    pause
    exit /b 1
)

echo Checking for requirements.txt...
if not exist "requirements.txt" (
    echo.
    echo ERROR: requirements.txt not found in the current directory.
    echo Make sure you are running this script from the project root.
    echo.
    pause
    exit /b 1
)

echo Checking for app.py...
if not exist "app.py" (
    echo.
    echo ERROR: app.py not found in the current directory.
    echo Make sure you are running this script from the project root.
    echo.
    pause
    exit /b 1
)

REM --- Step 2: Create and Activate Virtual Environment ---
REM Check if the virtual environment folder exists. If not, create it.
if not exist "venv\" (
    echo.
    echo Creating Python virtual environment (this will only happen once)...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo.
        echo ERROR: Failed to create the virtual environment.
        echo.
        pause
        exit /b 1
    )
)

REM Activate the virtual environment for this script session.
echo Activating virtual environment...
call "venv\Scripts\activate.bat"

REM --- Step 3: Install Dependencies ---
echo.
echo Installing/updating dependencies from requirements.txt...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to install dependencies. Please check requirements.txt and your internet connection.
    echo.
    pause
    exit /b 1
)

REM --- Step 4: Run the Flask Application ---
echo.
echo =======================================================
echo  Starting Flask Application...
echo  Access it at: http://127.0.0.1:7860
echo  Press CTRL+C in this window to stop the server.
echo =======================================================
echo.
python app.py

REM --- Script End ---
echo.
echo Server has been stopped.
pause
