@echo off
setlocal enabledelayedexpansion

REM ========== CONFIGURATION ==========
REM UPDATE THESE VALUES FOR YOUR REPOSITORY
set "GITHUB_USER=Manishrdy"
set "GITHUB_REPO=SimCricketX"
set "MAIN_BRANCH=main"
REM Change to "master" if your default branch is master
REM ===================================

REM ============================================
REM AUTO-UPDATE FUNCTION WITH USER DATA PROTECTION
REM ============================================
goto :main_script

:check_and_update
echo.
echo ============================================
echo Checking for updates...
echo ============================================

REM Check if curl is available
where curl >nul 2>nul
if errorlevel 1 (
    echo curl not available, skipping update check.
    echo ^(Install curl from https://curl.se/windows/ or use Windows 10/11 built-in^)
    echo.
    timeout /t 2 >nul
    goto :eof
)

REM Check if tar is available (Windows 10/11 has built-in tar that can handle zip)
where tar >nul 2>nul
if errorlevel 1 (
    echo tar not available, cannot auto-update.
    echo ^(Should be built-in on Windows 10/11^)
    echo.
    timeout /t 2 >nul
    goto :eof
)

REM Get current version
set "CURRENT_VERSION=unknown"
if exist "version.txt" (
    set /p CURRENT_VERSION=<version.txt
)
echo Current version: %CURRENT_VERSION%

REM Create temp file for latest version
set "TEMP_VERSION_FILE=%TEMP%\simcricketx_latest_version.txt"

REM Download latest version from GitHub
echo Checking latest version from GitHub...
curl -s -f "https://raw.githubusercontent.com/%GITHUB_USER%/%GITHUB_REPO%/%MAIN_BRANCH%/version.txt" -o "%TEMP_VERSION_FILE%" 2>nul
if errorlevel 1 (
    echo Could not check for updates.
    echo ^(Check your internet connection or repository settings^)
    echo.
    timeout /t 2 >nul
    goto :eof
)

REM Read latest version
if exist "%TEMP_VERSION_FILE%" (
    set /p LATEST_VERSION=<"%TEMP_VERSION_FILE%"
    del "%TEMP_VERSION_FILE%" 2>nul
) else (
    echo Could not determine latest version.
    echo.
    timeout /t 2 >nul
    goto :eof
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
    echo √ You have the latest version!
    echo.
    timeout /t 2 >nul
) else (
    echo.
    echo ============================================
    echo    UPDATE AVAILABLE!
    echo ============================================
    echo Current version: %CURRENT_VERSION%
    echo Latest version:  %LATEST_VERSION%
    echo.
    echo A newer version is available on GitHub!
    echo.
    echo What would you like to do?
    echo [1] Continue with current version
    echo [2] AUTO-UPDATE: Download and install latest version
    echo [3] Open GitHub page manually
    echo [4] Exit to update manually
    echo.
    
    set /p choice="Enter your choice (1-4) [default: 2]: "
    
    if "!choice!"=="" set "choice=2"
    
    if "!choice!"=="2" (
        echo.
        echo ============================================
        echo    AUTO-UPDATING...
        echo ============================================
        
        REM Create backup directory with timestamp
        for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set datetime=%%I
        set "BACKUP_DIR=backup_!datetime:~0,8!_!datetime:~8,6!"
        echo Creating backup in: !BACKUP_DIR!
        mkdir "!BACKUP_DIR!" 2>nul
        
        REM Backup important files (excluding user data)
        echo Backing up current files...
        for %%F in (*.py *.txt *.html) do (
            if exist "%%F" (
                copy "%%F" "!BACKUP_DIR!\" >nul 2>&1
                echo   √ Backed up: %%F
            )
        )
        for %%D in (templates static config engine utils) do (
            if exist "%%D" (
                xcopy "%%D" "!BACKUP_DIR!\%%D\" /E /I /Q >nul 2>&1
                echo   √ Backed up: %%D/
            )
        )
        
        REM Special backup for user data that must be preserved
        echo Preserving user data...
        set "USER_DATA_BACKUP=!BACKUP_DIR!\user_data_preserve"
        mkdir "!USER_DATA_BACKUP!" 2>nul
        
        REM Preserve auth credentials
        if exist "auth\credentials.json" (
            mkdir "!USER_DATA_BACKUP!\auth" 2>nul
            copy "auth\credentials.json" "!USER_DATA_BACKUP!\auth\" >nul 2>&1
            echo   Preserved: auth\credentials.json
        )
        
        if exist "auth\encryption.key" (
            mkdir "!USER_DATA_BACKUP!\auth" 2>nul
            copy "auth\encryption.key" "!USER_DATA_BACKUP!\auth\" >nul 2>&1
            echo   Preserved: auth\encryption.key
        )
        
        REM Preserve entire data folder
        if exist "data" (
            xcopy "data" "!USER_DATA_BACKUP!\data\" /E /I /Q >nul 2>&1
            echo   Preserved: data\ ^(entire folder^)
        )
        
        REM Preserve entire logs folder
        if exist "logs" (
            xcopy "logs" "!USER_DATA_BACKUP!\logs\" /E /I /Q >nul 2>&1
            echo   Preserved: logs\ ^(entire folder^)
        )
        
        REM Preserve root log files
        if exist "user_auth.log" (
            copy "user_auth.log" "!USER_DATA_BACKUP!\" >nul 2>&1
            echo   Preserved: user_auth.log
        )
        
        if exist "auth_debug.log" (
            copy "auth_debug.log" "!USER_DATA_BACKUP!\" >nul 2>&1
            echo   Preserved: auth_debug.log
        )
        
        REM Download latest ZIP
        set "TEMP_ZIP=%TEMP%\simcricketx_latest.zip"
        echo.
        echo Downloading latest version...
        curl -L -o "!TEMP_ZIP!" "https://github.com/%GITHUB_USER%/%GITHUB_REPO%/archive/refs/heads/%MAIN_BRANCH%.zip"
        if errorlevel 1 (
            echo × Download failed!
            echo Restore from backup if needed: xcopy "!BACKUP_DIR!\*" . /E /Y
            echo.
            pause
            goto :eof
        )
        echo √ Download completed!
        
        REM Extract to temporary directory
        set "TEMP_EXTRACT=%TEMP%\simcricketx_extract"
        if exist "!TEMP_EXTRACT!" rd /s /q "!TEMP_EXTRACT!" 2>nul
        mkdir "!TEMP_EXTRACT!" 2>nul
        
        echo Extracting files...
        tar -xf "!TEMP_ZIP!" -C "!TEMP_EXTRACT!"
        if errorlevel 1 (
            echo × Extraction failed!
            del "!TEMP_ZIP!" 2>nul
            echo Restore from backup if needed: xcopy "!BACKUP_DIR!\*" . /E /Y
            echo.
            pause
            goto :eof
        )
        echo √ Extraction completed!
        
        REM Find the extracted folder (GitHub creates [repo-name]-[branch]/)
        set "EXTRACTED_FOLDER=!TEMP_EXTRACT!\%GITHUB_REPO%-%MAIN_BRANCH%"
        if not exist "!EXTRACTED_FOLDER!" (
            REM Try alternative naming
            set "EXTRACTED_FOLDER=!TEMP_EXTRACT!\%GITHUB_REPO%-master"
            if not exist "!EXTRACTED_FOLDER!" (
                echo × Could not find extracted folder!
                echo Contents of extract directory:
                dir "!TEMP_EXTRACT!"
                del "!TEMP_ZIP!" 2>nul
                rd /s /q "!TEMP_EXTRACT!" 2>nul
                echo.
                pause
                goto :eof
            )
        )
        
        REM Copy new files to current directory
        echo Installing new files...
        pushd "!EXTRACTED_FOLDER!"
        for /f "delims=" %%I in ('dir /b') do (
            if exist "%%I\*" (
                xcopy "%%I" "%~dp0%%I\" /E /I /Y /Q >nul 2>&1
                echo   √ Updated: %%I
            ) else (
                copy "%%I" "%~dp0" /Y >nul 2>&1
                echo   √ Updated: %%I
            )
        )
        popd
        
        REM Restore preserved user data (CRITICAL - overwrite any updated versions)
        echo Restoring preserved user data...
        if exist "!USER_DATA_BACKUP!" (
            REM Restore auth files
            if exist "!USER_DATA_BACKUP!\auth\credentials.json" (
                if not exist "auth" mkdir "auth" 2>nul
                copy "!USER_DATA_BACKUP!\auth\credentials.json" "auth\" /Y >nul 2>&1
                echo   Restored: auth\credentials.json
            )
            
            if exist "!USER_DATA_BACKUP!\auth\encryption.key" (
                if not exist "auth" mkdir "auth" 2>nul
                copy "!USER_DATA_BACKUP!\auth\encryption.key" "auth\" /Y >nul 2>&1
                echo   Restored: auth\encryption.key
            )
            
            REM Restore entire data folder
            if exist "!USER_DATA_BACKUP!\data" (
                if exist "data" rd /s /q "data" 2>nul
                xcopy "!USER_DATA_BACKUP!\data" "data\" /E /I /Y /Q >nul 2>&1
                echo   Restored: data\ ^(entire folder^)
            )
            
            REM Restore entire logs folder
            if exist "!USER_DATA_BACKUP!\logs" (
                if exist "logs" rd /s /q "logs" 2>nul
                xcopy "!USER_DATA_BACKUP!\logs" "logs\" /E /I /Y /Q >nul 2>&1
                echo   Restored: logs\ ^(entire folder^)
            )
            
            REM Restore root log files
            if exist "!USER_DATA_BACKUP!\user_auth.log" (
                copy "!USER_DATA_BACKUP!\user_auth.log" . /Y >nul 2>&1
                echo   Restored: user_auth.log
            )
            
            if exist "!USER_DATA_BACKUP!\auth_debug.log" (
                copy "!USER_DATA_BACKUP!\auth_debug.log" . /Y >nul 2>&1
                echo   Restored: auth_debug.log
            )
        )
        
        REM Cleanup
        del "!TEMP_ZIP!" 2>nul
        rd /s /q "!TEMP_EXTRACT!" 2>nul
        
        echo.
        echo ============================================
        echo    UPDATE COMPLETED!
        echo ============================================
        echo Updated to version: %LATEST_VERSION%
        echo Backup saved in: !BACKUP_DIR!
        echo.
        echo USER DATA PROTECTION SUMMARY:
        echo   auth\credentials.json - PRESERVED
        echo   auth\encryption.key - PRESERVED
        echo   data\ folder - PRESERVED
        echo   logs\ folder - PRESERVED
        echo   user_auth.log - PRESERVED
        echo   auth_debug.log - PRESERVED
        echo.
        echo If anything goes wrong, restore with:
        echo   xcopy "!BACKUP_DIR!\*" . /E /Y
        echo.
        timeout /t 3 >nul
    ) else if "!choice!"=="3" (
        echo Opening GitHub repository...
        start "" "https://github.com/%GITHUB_USER%/%GITHUB_REPO%"
        echo.
        echo Please download the latest version from GitHub.
        echo.
        pause
    ) else if "!choice!"=="4" (
        echo.
        echo Please download the latest version from:
        echo https://github.com/%GITHUB_USER%/%GITHUB_REPO%
        echo.
        echo After updating, run this script again.
        echo.
        pause
        exit /b 0
    ) else (
        echo Continuing with current version...
        echo.
        timeout /t 2 >nul
    )
)
goto :eof

REM ============================================
REM FORCE UPDATE FUNCTION (if called with --update)
REM ============================================
:force_update
echo.
echo ============================================
echo    FORCE UPDATE MODE
echo ============================================

REM Check requirements
where curl >nul 2>nul
if errorlevel 1 (
    echo ERROR: curl not available. Install from https://curl.se/windows/
    exit /b 1
)

where tar >nul 2>nul
if errorlevel 1 (
    echo ERROR: tar not available. Should be built-in on Windows 10/11
    exit /b 1
)

echo Force updating to latest version...

REM Create backup
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set datetime=%%I
set "BACKUP_DIR=backup_%datetime:~0,8%_%datetime:~8,6%"
echo Creating backup in: %BACKUP_DIR%
mkdir "%BACKUP_DIR%" 2>nul

for %%F in (*.py *.txt *.html) do (
    if exist "%%F" copy "%%F" "%BACKUP_DIR%\" >nul 2>&1
)
for %%D in (templates static config engine utils) do (
    if exist "%%D" xcopy "%%D" "%BACKUP_DIR%\%%D\" /E /I /Q >nul 2>&1
)

REM Special backup for user data that must be preserved
echo Preserving user data...
set "USER_DATA_BACKUP=%BACKUP_DIR%\user_data_preserve"
mkdir "%USER_DATA_BACKUP%" 2>nul

REM Preserve all protected files/folders
if exist "auth\credentials.json" (
    mkdir "%USER_DATA_BACKUP%\auth" 2>nul
    copy "auth\credentials.json" "%USER_DATA_BACKUP%\auth\" >nul 2>&1
)
if exist "auth\encryption.key" (
    mkdir "%USER_DATA_BACKUP%\auth" 2>nul
    copy "auth\encryption.key" "%USER_DATA_BACKUP%\auth\" >nul 2>&1
)
if exist "data" xcopy "data" "%USER_DATA_BACKUP%\data\" /E /I /Q >nul 2>&1
if exist "logs" xcopy "logs" "%USER_DATA_BACKUP%\logs\" /E /I /Q >nul 2>&1
if exist "user_auth.log" copy "user_auth.log" "%USER_DATA_BACKUP%\" >nul 2>&1
if exist "auth_debug.log" copy "auth_debug.log" "%USER_DATA_BACKUP%\" >nul 2>&1

REM Download and install
set "TEMP_ZIP=%TEMP%\simcricketx_latest.zip"
echo Downloading...
curl -L -o "%TEMP_ZIP%" "https://github.com/%GITHUB_USER%/%GITHUB_REPO%/archive/refs/heads/%MAIN_BRANCH%.zip"

set "TEMP_EXTRACT=%TEMP%\simcricketx_extract"
if exist "%TEMP_EXTRACT%" rd /s /q "%TEMP_EXTRACT%" 2>nul
mkdir "%TEMP_EXTRACT%" 2>nul

echo Extracting...
tar -xf "%TEMP_ZIP%" -C "%TEMP_EXTRACT%"

set "EXTRACTED_FOLDER=%TEMP_EXTRACT%\%GITHUB_REPO%-%MAIN_BRANCH%"
if not exist "%EXTRACTED_FOLDER%" (
    set "EXTRACTED_FOLDER=%TEMP_EXTRACT%\%GITHUB_REPO%-master"
)

echo Installing...
pushd "%EXTRACTED_FOLDER%"
xcopy * "%~dp0" /E /Y /Q >nul
popd

REM Restore preserved user data
echo Restoring user data...
if exist "%USER_DATA_BACKUP%" (
    if exist "%USER_DATA_BACKUP%\auth\credentials.json" (
        if not exist "auth" mkdir "auth" 2>nul
        copy "%USER_DATA_BACKUP%\auth\credentials.json" "auth\" /Y >nul 2>&1
    )
    if exist "%USER_DATA_BACKUP%\auth\encryption.key" (
        if not exist "auth" mkdir "auth" 2>nul
        copy "%USER_DATA_BACKUP%\auth\encryption.key" "auth\" /Y >nul 2>&1
    )
    if exist "%USER_DATA_BACKUP%\data" (
        if exist "data" rd /s /q "data" 2>nul
        xcopy "%USER_DATA_BACKUP%\data" "data\" /E /I /Y /Q >nul 2>&1
    )
    if exist "%USER_DATA_BACKUP%\logs" (
        if exist "logs" rd /s /q "logs" 2>nul
        xcopy "%USER_DATA_BACKUP%\logs" "logs\" /E /I /Y /Q >nul 2>&1
    )
    if exist "%USER_DATA_BACKUP%\user_auth.log" copy "%USER_DATA_BACKUP%\user_auth.log" . /Y >nul 2>&1
    if exist "%USER_DATA_BACKUP%\auth_debug.log" copy "%USER_DATA_BACKUP%\auth_debug.log" . /Y >nul 2>&1
)

REM Cleanup
del "%TEMP_ZIP%" 2>nul
rd /s /q "%TEMP_EXTRACT%" 2>nul

echo √ Force update completed!
echo Backup saved in: %BACKUP_DIR%
echo.
echo USER DATA PROTECTED:
echo   auth\credentials.json
echo   auth\encryption.key
echo   data\ folder
echo   logs\ folder
echo   user_auth.log
echo   auth_debug.log
echo.
goto :eof

REM ============================================
REM MAIN SCRIPT LOGIC
REM ============================================
:main_script

REM Handle command line arguments
if /i "%~1"=="--update" (
    call :force_update
    exit /b 0
)
if /i "%~1"=="--help" (
    echo Usage: %~nx0 [options]
    echo Options:
    echo   --update        Force update to latest version
    echo   --skip-update   Skip update check
    echo   --help          Show this help
    exit /b 0
)

echo ============================================
echo Starting SimCricketX Flask App...
echo ============================================
echo.

REM Change to script directory
cd /d "%~dp0"
echo Current directory: %CD%
echo.

REM Check for updates (unless skipped)
if /i not "%~1"=="--skip-update" (
    call :check_and_update
)

REM Show what files are present
echo Files in directory:
dir /b *.py *.txt 2>nul || echo No .py or .txt files found!
echo.

REM Set terminal title
title SimCricketX Flask App

REM Check for Python
echo Checking for Python...
set "PYTHON_CMD="
where python3 >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python3"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD=python"
    )
)

if "%PYTHON_CMD%"=="" (
    echo.
    echo ERROR: Python is not found in your system's PATH.
    echo Please install Python and make sure it's accessible.
    echo.
    echo To fix this:
    echo 1. Install Python from python.org
    echo 2. Make sure to check "Add Python to PATH" during installation
    echo 3. Restart this command prompt after installation
    echo.
    pause
    exit /b 1
)

echo Python found successfully! Using: %PYTHON_CMD%
%PYTHON_CMD% --version
echo.

REM Check for required files
echo Checking for requirements.txt...
if not exist "requirements.txt" (
    echo.
    echo ERROR: requirements.txt not found in: %CD%
    echo.
    echo Directory contents:
    dir
    echo.
    echo Make sure this script is in the same folder as requirements.txt
    echo Or run with --update to download latest files
    echo.
    pause
    exit /b 1
)
echo requirements.txt found!

echo Checking for app.py...
if not exist "app.py" (
    echo.
    echo ERROR: app.py not found in: %CD%
    echo Make sure this script is in the same folder as app.py
    echo Or run with --update to download latest files
    echo.
    pause
    exit /b 1
)
echo app.py found!

echo.
echo All checks passed! Continuing with setup...
echo.

REM Create virtual environment
if not exist "venv" (
    echo Creating Python virtual environment...
    %PYTHON_CMD% -m venv venv
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to create virtual environment.
        echo This might be due to:
        echo - Insufficient permissions
        echo - Missing venv module ^(try: pip install virtualenv^)
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
call venv\Scripts\activate.bat
if errorlevel 1 (
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
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies.
    echo Check your internet connection and requirements.txt content.
    echo.
    pause
    exit /b 1
)

echo.
echo =======================================================
echo   Starting Flask Application...
echo   Access it at: http://127.0.0.1:7860
echo   Press CTRL+C to stop the server
echo =======================================================
echo.

python app.py

echo.
echo Application stopped.
pause