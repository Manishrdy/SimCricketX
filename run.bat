@echo off
setlocal enabledelayedexpansion

REM ========== CONFIGURATION ==========
REM UPDATE THESE VALUES FOR YOUR REPOSITORY
set "GITHUB_USER=Manishrdy"
set "GITHUB_REPO=SimCricketX"
set "MAIN_BRANCH=main"
REM ===================================

REM Handle command line arguments
if "%1"=="--update" (
    call :force_update
    exit /b 0
)
if "%1"=="--help" (
    echo Usage: %0 [options]
    echo Options:
    echo   --update        Force update to latest version
    echo   --skip-update   Skip update check
    echo   --help          Show this help
    pause
    exit /b 0
)

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

REM Check for updates (unless skipped)
if not "%1"=="--skip-update" (
    call :check_and_update
)

REM Show what files are present
echo Files in directory:
dir /b *.py *.txt 2>nul
if %errorlevel% neq 0 (
    echo No .py or .txt files found!
)
echo.

title SimCricketX Flask App

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
    echo Make sure this batch file is in the same folder as app.py
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
REM AUTO-UPDATE FUNCTION WITH DOWNLOAD
REM ============================================
:check_and_update
echo.
echo ============================================
echo Checking for updates...
echo ============================================

REM Check if curl is available (Windows 10+ has it built-in)
curl --version >nul 2>&1
if %errorlevel% neq 0 (
    echo curl not available, skipping update check.
    echo (Update checking requires Windows 10 or newer)
    echo.
    timeout /t 2 >nul
    goto :eof
)

REM Check if PowerShell is available for ZIP extraction
powershell -Command "Write-Host 'PowerShell available'" >nul 2>&1
if %errorlevel% neq 0 (
    echo PowerShell not available, cannot auto-update.
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
curl -s -f "https://raw.githubusercontent.com/%GITHUB_USER%/%GITHUB_REPO%/%MAIN_BRANCH%/version.txt" -o "%TEMP_VERSION_FILE%" 2>nul

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
    echo [2] AUTO-UPDATE: Download and install latest version
    echo [3] Open GitHub page manually
    echo [4] Exit to update manually
    echo.
    
    set /p "choice=Enter your choice (1-4) [default: 2]: "
    
    if "%choice%"=="" set "choice=2"
    
    if "%choice%"=="2" (
        echo.
        echo ============================================
        echo   🔄 AUTO-UPDATING...
        echo ============================================
        
        REM Create backup directory with timestamp
        for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set "dt=%%I"
        set "BACKUP_DIR=backup_%dt:~0,8%_%dt:~8,6%"
        echo Creating backup in: %BACKUP_DIR%
        mkdir "%BACKUP_DIR%" 2>nul
        
        REM Backup important files (excluding user data)
        echo Backing up current files...
        for %%F in (*.py *.txt *.html) do (
            if exist "%%F" (
                copy "%%F" "%BACKUP_DIR%\" >nul 2>&1
                echo   ✓ Backed up: %%F
            )
        )
        for %%D in (templates static config engine utils) do (
            if exist "%%D\" (
                xcopy "%%D" "%BACKUP_DIR%\%%D\" /E /I /Q >nul 2>&1
                echo   ✓ Backed up: %%D\
            )
        )
        
        REM Special backup for user data that must be preserved
        echo Preserving user data...
        set "USER_DATA_BACKUP=%BACKUP_DIR%\user_data_preserve"
        mkdir "%USER_DATA_BACKUP%" 2>nul
        
        REM Preserve auth credentials
        if exist "auth\credentials.json" (
            mkdir "%USER_DATA_BACKUP%\auth" 2>nul
            copy "auth\credentials.json" "%USER_DATA_BACKUP%\auth\" >nul 2>&1
            echo   🔒 Preserved: auth\credentials.json
        )
        
        if exist "auth\encryption.key" (
            mkdir "%USER_DATA_BACKUP%\auth" 2>nul
            copy "auth\encryption.key" "%USER_DATA_BACKUP%\auth\" >nul 2>&1
            echo   🔒 Preserved: auth\encryption.key
        )
        
        REM Preserve entire data folder
        if exist "data\" (
            xcopy "data" "%USER_DATA_BACKUP%\data\" /E /I /Q >nul 2>&1
            echo   🔒 Preserved: data\ (entire folder)
        )
        
        REM Preserve entire logs folder
        if exist "logs\" (
            xcopy "logs" "%USER_DATA_BACKUP%\logs\" /E /I /Q >nul 2>&1
            echo   🔒 Preserved: logs\ (entire folder)
        )
        
        REM Preserve root log files
        if exist "user_auth.log" (
            copy "user_auth.log" "%USER_DATA_BACKUP%\" >nul 2>&1
            echo   🔒 Preserved: user_auth.log
        )
        
        if exist "auth_debug.log" (
            copy "auth_debug.log" "%USER_DATA_BACKUP%\" >nul 2>&1
            echo   🔒 Preserved: auth_debug.log
        )
        
        REM Download latest ZIP
        set "TEMP_ZIP=%TEMP%\simcricketx_latest.zip"
        echo.
        echo Downloading latest version...
        curl -L -o "%TEMP_ZIP%" "https://github.com/%GITHUB_USER%/%GITHUB_REPO%/archive/refs/heads/%MAIN_BRANCH%.zip"
        if %errorlevel% neq 0 (
            echo ✗ Download failed!
            echo Restore from backup if needed: xcopy "%BACKUP_DIR%\*" . /E /Y
            echo.
            pause
            goto :eof
        )
        echo ✓ Download completed!
        
        REM Extract to temporary directory
        set "TEMP_EXTRACT=%TEMP%\simcricketx_extract"
        if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%" 2>nul
        mkdir "%TEMP_EXTRACT%" 2>nul
        
        echo Extracting files...
        powershell -Command "Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%TEMP_EXTRACT%' -Force" >nul 2>&1
        if %errorlevel% neq 0 (
            echo ✗ Extraction failed!
            del "%TEMP_ZIP%" 2>nul
            echo Restore from backup if needed: xcopy "%BACKUP_DIR%\*" . /E /Y
            echo.
            pause
            goto :eof
        )
        echo ✓ Extraction completed!
        
        REM Find the extracted folder (GitHub creates [repo-name]-[branch]/)
        set "EXTRACTED_FOLDER=%TEMP_EXTRACT%\%GITHUB_REPO%-%MAIN_BRANCH%"
        if not exist "%EXTRACTED_FOLDER%" (
            set "EXTRACTED_FOLDER=%TEMP_EXTRACT%\%GITHUB_REPO%-master"
            if not exist "!EXTRACTED_FOLDER!" (
                echo ✗ Could not find extracted folder!
                echo Contents of extract directory:
                dir "%TEMP_EXTRACT%"
                del "%TEMP_ZIP%" 2>nul
                rmdir /s /q "%TEMP_EXTRACT%" 2>nul
                echo.
                pause
                goto :eof
            )
        )
        
        REM Copy new files to current directory
        echo Installing new files...
        pushd "%EXTRACTED_FOLDER%"
        for /f "tokens=*" %%I in ('dir /b') do (
            if exist "%%I" (
                if exist "%%I\" (
                    xcopy "%%I" "%~dp0%%I\" /E /I /Y /Q >nul 2>&1
                ) else (
                    copy "%%I" "%~dp0" >nul 2>&1
                )
                echo   ✓ Updated: %%I
            )
        )
        popd
        
        REM Restore preserved user data (CRITICAL - overwrite any updated versions)
        echo Restoring preserved user data...
        if exist "%USER_DATA_BACKUP%" (
            REM Restore auth files
            if exist "%USER_DATA_BACKUP%\auth\credentials.json" (
                mkdir "auth" 2>nul
                copy "%USER_DATA_BACKUP%\auth\credentials.json" "auth\" >nul 2>&1
                echo   🔒 Restored: auth\credentials.json
            )
            
            if exist "%USER_DATA_BACKUP%\auth\encryption.key" (
                mkdir "auth" 2>nul
                copy "%USER_DATA_BACKUP%\auth\encryption.key" "auth\" >nul 2>&1
                echo   🔒 Restored: auth\encryption.key
            )
            
            REM Restore entire data folder
            if exist "%USER_DATA_BACKUP%\data\" (
                if exist "data\" rmdir /s /q "data" 2>nul
                xcopy "%USER_DATA_BACKUP%\data" "data\" /E /I /Q >nul 2>&1
                echo   🔒 Restored: data\ (entire folder)
            )
            
            REM Restore entire logs folder
            if exist "%USER_DATA_BACKUP%\logs\" (
                if exist "logs\" rmdir /s /q "logs" 2>nul
                xcopy "%USER_DATA_BACKUP%\logs" "logs\" /E /I /Q >nul 2>&1
                echo   🔒 Restored: logs\ (entire folder)
            )
            
            REM Restore root log files
            if exist "%USER_DATA_BACKUP%\user_auth.log" (
                copy "%USER_DATA_BACKUP%\user_auth.log" "." >nul 2>&1
                echo   🔒 Restored: user_auth.log
            )
            
            if exist "%USER_DATA_BACKUP%\auth_debug.log" (
                copy "%USER_DATA_BACKUP%\auth_debug.log" "." >nul 2>&1
                echo   🔒 Restored: auth_debug.log
            )
        )
        
        REM Cleanup
        del "%TEMP_ZIP%" 2>nul
        rmdir /s /q "%TEMP_EXTRACT%" 2>nul
        
        echo.
        echo ============================================
        echo   ✅ UPDATE COMPLETED!
        echo ============================================
        echo Updated to version: %LATEST_VERSION%
        echo Backup saved in: %BACKUP_DIR%
        echo.
        echo If anything goes wrong, restore with:
        echo   xcopy "%BACKUP_DIR%\*" . /E /Y
        echo.
        timeout /t 3 >nul
    )
    
    if "%choice%"=="3" (
        echo Opening GitHub repository...
        start https://github.com/%GITHUB_USER%/%GITHUB_REPO%
        echo.
        echo Please download the latest version from GitHub.
        echo.
        pause
    )
    
    if "%choice%"=="4" (
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

REM ============================================
REM FORCE UPDATE FUNCTION
REM ============================================
:force_update
echo.
echo ============================================
echo   🔄 FORCE UPDATE MODE
echo ============================================

REM Check requirements
curl --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: curl not available. Update checking requires Windows 10 or newer
    pause
    exit /b 1
)

powershell -Command "Write-Host 'PowerShell available'" >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: PowerShell not available
    pause
    exit /b 1
)

echo Force updating to latest version...

REM Create backup
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set "dt=%%I"
set "BACKUP_DIR=backup_%dt:~0,8%_%dt:~8,6%"
echo Creating backup in: %BACKUP_DIR%
mkdir "%BACKUP_DIR%" 2>nul

for %%F in (*.py *.txt *.html) do (
    if exist "%%F" copy "%%F" "%BACKUP_DIR%\" >nul 2>&1
)
for %%D in (templates static config engine utils) do (
    if exist "%%D\" xcopy "%%D" "%BACKUP_DIR%\%%D\" /E /I /Q >nul 2>&1
)

REM Preserve user data
set "USER_DATA_BACKUP=%BACKUP_DIR%\user_data_preserve"
mkdir "%USER_DATA_BACKUP%" 2>nul

if exist "auth\credentials.json" (
    mkdir "%USER_DATA_BACKUP%\auth" 2>nul
    copy "auth\credentials.json" "%USER_DATA_BACKUP%\auth\" >nul 2>&1
)
if exist "auth\encryption.key" (
    mkdir "%USER_DATA_BACKUP%\auth" 2>nul
    copy "auth\encryption.key" "%USER_DATA_BACKUP%\auth\" >nul 2>&1
)
if exist "data\" xcopy "data" "%USER_DATA_BACKUP%\data\" /E /I /Q >nul 2>&1
if exist "logs\" xcopy "logs" "%USER_DATA_BACKUP%\logs\" /E /I /Q >nul 2>&1
if exist "user_auth.log" copy "user_auth.log" "%USER_DATA_BACKUP%\" >nul 2>&1
if exist "auth_debug.log" copy "auth_debug.log" "%USER_DATA_BACKUP%\" >nul 2>&1

REM Download and install
set "TEMP_ZIP=%TEMP%\simcricketx_latest.zip"
echo Downloading...
curl -L -o "%TEMP_ZIP%" "https://github.com/%GITHUB_USER%/%GITHUB_REPO%/archive/refs/heads/%MAIN_BRANCH%.zip"

set "TEMP_EXTRACT=%TEMP%\simcricketx_extract"
if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%" 2>nul
mkdir "%TEMP_EXTRACT%" 2>nul

echo Extracting...
powershell -Command "Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%TEMP_EXTRACT%' -Force" >nul 2>&1

set "EXTRACTED_FOLDER=%TEMP_EXTRACT%\%GITHUB_REPO%-%MAIN_BRANCH%"
if not exist "%EXTRACTED_FOLDER%" (
    set "EXTRACTED_FOLDER=%TEMP_EXTRACT%\%GITHUB_REPO%-master"
)

echo Installing...
pushd "%EXTRACTED_FOLDER%"
for /f "tokens=*" %%I in ('dir /b') do (
    if exist "%%I" (
        if exist "%%I\" (
            xcopy "%%I" "%~dp0%%I\" /E /I /Y /Q >nul 2>&1
        ) else (
            copy "%%I" "%~dp0" >nul 2>&1
        )
    )
)
popd

REM Restore user data
echo Restoring user data...
if exist "%USER_DATA_BACKUP%\auth\credentials.json" (
    mkdir "auth" 2>nul
    copy "%USER_DATA_BACKUP%\auth\credentials.json" "auth\" >nul 2>&1
)
if exist "%USER_DATA_BACKUP%\auth\encryption.key" (
    mkdir "auth" 2>nul
    copy "%USER_DATA_BACKUP%\auth\encryption.key" "auth\" >nul 2>&1
)
if exist "%USER_DATA_BACKUP%\data\" (
    if exist "data\" rmdir /s /q "data" 2>nul
    xcopy "%USER_DATA_BACKUP%\data" "data\" /E /I /Q >nul 2>&1
)
if exist "%USER_DATA_BACKUP%\logs\" (
    if exist "logs\" rmdir /s /q "logs" 2>nul
    xcopy "%USER_DATA_BACKUP%\logs" "logs\" /E /I /Q >nul 2>&1
)
if exist "%USER_DATA_BACKUP%\user_auth.log" copy "%USER_DATA_BACKUP%\user_auth.log" "." >nul 2>&1
if exist "%USER_DATA_BACKUP%\auth_debug.log" copy "%USER_DATA_BACKUP%\auth_debug.log" "." >nul 2>&1

REM Cleanup
del "%TEMP_ZIP%" 2>nul
rmdir /s /q "%TEMP_EXTRACT%" 2>nul

echo ✅ Force update completed!
echo Backup saved in: %BACKUP_DIR%
echo.
goto :eof