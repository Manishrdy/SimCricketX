@echo off
setlocal enabledelayedexpansion

REM ========== CONFIGURATION ==========
REM UPDATE THESE VALUES FOR YOUR REPOSITORY
set "GITHUB_USER=Manishrdy"
set "GITHUB_REPO=SimCricketX"
set "MAIN_BRANCH=main"
REM Change to "master" if your default branch is master
REM ===================================

REM ========== DEFAULT SETTINGS ==========
REM These can be overridden in .simcricketx.conf
if not defined AUTO_UPDATE_CHECK set "AUTO_UPDATE_CHECK=true"
if not defined UPDATE_CHECK_INTERVAL set "UPDATE_CHECK_INTERVAL=86400"
if not defined PRESERVE_BACKUPS_DAYS set "PRESERVE_BACKUPS_DAYS=30"
if not defined DEFAULT_PORT set "DEFAULT_PORT=7860"
if not defined CHECK_PORT_BEFORE_START set "CHECK_PORT_BEFORE_START=true"
if not defined ENABLE_UPDATE_LOGGING set "ENABLE_UPDATE_LOGGING=true"
set "LOG_FILE=simcricketx_updates.log"
REM ======================================

REM ============================================
REM LOGGING FUNCTION
REM ============================================
goto :skip_log_function
:log_message
if "%ENABLE_UPDATE_LOGGING%"=="true" (
    echo [%date% %time%] %~1 >> "%LOG_FILE%"
)
goto :eof
:skip_log_function

REM ============================================
REM PRE-FLIGHT DEPENDENCY CHECK
REM ============================================
goto :skip_check_deps
:check_dependencies
echo Checking system dependencies...
set "missing_deps="

where curl >nul 2>nul
if errorlevel 1 set "missing_deps=!missing_deps! curl"

where tar >nul 2>nul
if errorlevel 1 (
    REM Check for PowerShell as alternative
    where powershell >nul 2>nul
    if errorlevel 1 set "missing_deps=!missing_deps! tar/powershell"
)

where python3 >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 set "missing_deps=!missing_deps! python"
)

if not "!missing_deps!"=="" (
    echo x Missing dependencies:!missing_deps!
    echo.
    echo Install from:
    echo   curl: https://curl.se/windows/
    echo   tar: Built-in on Windows 10/11
    echo   python: https://www.python.org/downloads/
    echo.
    call :log_message "ERROR: Missing dependencies:!missing_deps!"
    exit /b 1
)

echo √ All dependencies available
call :log_message "All dependencies check passed"
exit /b 0
:skip_check_deps

REM ============================================
REM ROLLBACK FUNCTION
REM ============================================
goto :skip_rollback
:rollback_update
echo.
echo ============================================
echo    ROLLBACK MODE
echo ============================================

if "%~1"=="" (
    echo Available backups:
    set "found_backup=false"
    for /d %%D in (backup_*) do (
        set "found_backup=true"
        set "backup_name=%%D"
        set "backup_date=!backup_name:backup_=!"
        echo   - %%D ^(!backup_date:~0,8! !backup_date:~9,2!:!backup_date:~11,2!:!backup_date:~13,2!^)
    )
    if "!found_backup!"=="false" echo   No backups found
    echo.
    echo Usage: %~nx0 --rollback ^<backup_directory^>
    echo Example: %~nx0 --rollback backup_20260129_180000
    goto :eof
)

set "backup_dir=%~1"
if not exist "!backup_dir!" (
    echo x Error: Backup directory not found: !backup_dir!
    call :log_message "ERROR: Rollback failed - backup not found: !backup_dir!"
    goto :eof
)

echo Rolling back from: !backup_dir!
call :log_message "Starting rollback from: !backup_dir!"

REM Copy files back
xcopy "!backup_dir!\*" . /E /Y /Q >nul 2>&1

REM If user data was backed up separately, restore it too
if exist "!backup_dir!\user_data_preserve" (
    echo Restoring user data from backup...
    if exist "!backup_dir!\user_data_preserve\data" (
        if exist "data" rd /s /q "data" 2>nul
        xcopy "!backup_dir!\user_data_preserve\data" "data\" /E /I /Y /Q >nul 2>&1
    )
    if exist "!backup_dir!\user_data_preserve\logs" (
        if exist "logs" rd /s /q "logs" 2>nul
        xcopy "!backup_dir!\user_data_preserve\logs" "logs\" /E /I /Y /Q >nul 2>&1
    )
)

echo √ Rollback completed!
call :log_message "Rollback completed successfully from: !backup_dir!"
echo.
goto :eof
:skip_rollback

REM ============================================
REM CLEANUP OLD BACKUPS
REM ============================================
goto :skip_cleanup
:cleanup_old_backups
echo Cleaning up old backups ^(older than %PRESERVE_BACKUPS_DAYS% days^)...
call :log_message "Starting cleanup of backups older than %PRESERVE_BACKUPS_DAYS% days"

set "cleaned=0"
for /d %%D in (backup_*) do (
    REM Check if directory is older than specified days
    forfiles /p "%%D" /d -%PRESERVE_BACKUPS_DAYS% >nul 2>&1
    if not errorlevel 1 (
        echo   Removing old backup: %%D
        rd /s /q "%%D" 2>nul
        call :log_message "Removed old backup: %%D"
        set /a cleaned+=1
    )
)

if !cleaned! gtr 0 (
    echo   √ Cleaned up !cleaned! old backup^(s^)
) else (
    echo   No old backups to clean up
)
goto :eof
:skip_cleanup

REM ============================================
REM AUTO-UPDATE FUNCTION WITH USER DATA PROTECTION
REM ============================================
goto :main_script

:check_and_update
REM Check if update checking is disabled
if not "%AUTO_UPDATE_CHECK%"=="true" (
    echo Auto-update check is disabled in config.
    call :log_message "Update check skipped - disabled in config"
    goto :eof
)

REM Check throttling - only check once per interval
set "last_check_file=.last_update_check"
set "current_time="
for /f %%i in ('powershell -command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "current_time=%%i"

if exist "%last_check_file%" (
    set /p last_check=<"%last_check_file%"
    set /a time_diff=%current_time% - !last_check!
    
    if !time_diff! lss %UPDATE_CHECK_INTERVAL% (
        set /a hours_left=^(%UPDATE_CHECK_INTERVAL% - !time_diff!^) / 3600
        echo Update check performed recently ^(next check in ~!hours_left!h^), skipping...
        call :log_message "Update check skipped - last check was !time_diff! seconds ago"
        goto :eof
    )
)

echo.
echo ============================================
echo Checking for updates...
echo ============================================
call :log_message "Starting update check"

REM Check if curl is available
where curl >nul 2>nul
if errorlevel 1 (
    echo curl not available, skipping update check.
    echo ^(Install curl from https://curl.se/windows/ or use Windows 10/11 built-in^)
    echo.
    timeout /t 2 >nul
    call :log_message "Update check failed - curl not available"
    goto :eof
)

REM Check if tar or PowerShell is available
set "has_extractor=false"
where tar >nul 2>nul
if not errorlevel 1 (
    set "has_extractor=true"
    set "use_powershell=false"
) else (
    where powershell >nul 2>nul
    if not errorlevel 1 (
        set "has_extractor=true"
        set "use_powershell=true"
    )
)

if "%has_extractor%"=="false" (
    echo tar and PowerShell not available, cannot auto-update.
    echo.
    timeout /t 2 >nul
    call :log_message "Update check failed - no extraction tool available"
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
    call :log_message "Update check failed - network error"
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
    call :log_message "Update check failed - could not read version file"
    goto :eof
)

if "%LATEST_VERSION%"=="" (
    echo Could not determine latest version.
    echo.
    timeout /t 2 >nul
    call :log_message "Update check failed - empty version"
    goto :eof
)

echo Latest version: %LATEST_VERSION%

REM Save the check timestamp
echo %current_time% > "%last_check_file%"
call :log_message "Current: %CURRENT_VERSION%, Latest: %LATEST_VERSION%"

REM Compare versions
if "%CURRENT_VERSION%"=="%LATEST_VERSION%" (
    echo √ You have the latest version!
    echo.
    timeout /t 2 >nul
    call :log_message "Already on latest version"
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
    
    call :log_message "Update available - prompting user"
    set /p choice="Enter your choice (1-4) [default: 2]: "
    
    if "!choice!"=="" set "choice=2"
    
    call :log_message "User choice: !choice!"
    if "!choice!"=="2" (
        echo.
        echo ============================================
        echo    AUTO-UPDATING...
        echo ============================================
        call :log_message "Starting auto-update"
        
        REM Create backup directory with timestamp
        for /f "tokens=2 delims==." %%I in ('wmic os get localdatetime /value') do set datetime=%%I
        set "BACKUP_DIR=backup_!datetime:~0,8!_!datetime:~8,6!"
        echo Creating backup in: !BACKUP_DIR!
        mkdir "!BACKUP_DIR!" 2>nul
        call :log_message "Created backup directory: !BACKUP_DIR!"
        
        REM Backup important files (excluding user data)
        echo Backing up current files...
        for %%F in (*.py *.txt *.html *.sh *.bat) do (
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
        call :log_message "Downloading from GitHub"
        curl -L -o "!TEMP_ZIP!" "https://github.com/%GITHUB_USER%/%GITHUB_REPO%/archive/refs/heads/%MAIN_BRANCH%.zip"
        if errorlevel 1 (
            echo x Download failed!
            echo Restore from backup if needed: xcopy "!BACKUP_DIR!\*" . /E /Y
            echo.
            call :log_message "ERROR: Download failed"
            pause
            goto :eof
        )
        echo √ Download completed!
        call :log_message "Download completed"
        
        REM Verify download integrity
        echo Verifying download integrity...
        if "%use_powershell%"=="true" (
            powershell -command "try { Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::OpenRead('!TEMP_ZIP!').Dispose(); exit 0 } catch { exit 1 }"
        ) else (
            tar -tf "!TEMP_ZIP!" >nul 2>&1
        )
        if errorlevel 1 (
            echo x Downloaded file is corrupted!
            del "!TEMP_ZIP!" 2>nul
            echo Restore from backup if needed: xcopy "!BACKUP_DIR!\*" . /E /Y
            echo.
            call :log_message "ERROR: Downloaded file is corrupted"
            pause
            goto :eof
        )
        echo √ Download verified!
        call :log_message "Download integrity verified"
        
        REM Extract to temporary directory
        set "TEMP_EXTRACT=%TEMP%\simcricketx_extract"
        if exist "!TEMP_EXTRACT!" rd /s /q "!TEMP_EXTRACT!" 2>nul
        mkdir "!TEMP_EXTRACT!" 2>nul
        
        echo Extracting files...
        if "%use_powershell%"=="true" (
            powershell -command "Expand-Archive -Path '!TEMP_ZIP!' -DestinationPath '!TEMP_EXTRACT!' -Force"
        ) else (
            tar -xf "!TEMP_ZIP!" -C "!TEMP_EXTRACT!"
        )
        if errorlevel 1 (
            echo x Extraction failed!
            del "!TEMP_ZIP!" 2>nul
            echo Restore from backup if needed: xcopy "!BACKUP_DIR!\*" . /E /Y
            echo.
            call :log_message "ERROR: Extraction failed"
            pause
            goto :eof
        )
        echo √ Extraction completed!
        call :log_message "Extraction completed"
        
        REM Find the extracted folder (GitHub creates [repo-name]-[branch]/)
        set "EXTRACTED_FOLDER=!TEMP_EXTRACT!\%GITHUB_REPO%-%MAIN_BRANCH%"
        if not exist "!EXTRACTED_FOLDER!" (
            REM Try alternative naming
            set "EXTRACTED_FOLDER=!TEMP_EXTRACT!\%GITHUB_REPO%-master"
            if not exist "!EXTRACTED_FOLDER!" (
                echo x Could not find extracted folder!
                echo Contents of extract directory:
                dir "!TEMP_EXTRACT!"
                del "!TEMP_ZIP!" 2>nul
                rd /s /q "!TEMP_EXTRACT!" 2>nul
                echo.
                call :log_message "ERROR: Could not find extracted folder"
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
        call :log_message "New files installed"
        
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
        call :log_message "User data restored"
        
        REM Cleanup
        del "!TEMP_ZIP!" 2>nul
        rd /s /q "!TEMP_EXTRACT!" 2>nul
        
        REM Cleanup old backups
        call :cleanup_old_backups
        
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
        echo   Or use: %~nx0 --rollback !BACKUP_DIR!
        echo.
        call :log_message "Update completed successfully to version: %LATEST_VERSION%"
        timeout /t 3 >nul
    ) else if "!choice!"=="3" (
        echo Opening GitHub repository...
        start "" "https://github.com/%GITHUB_USER%/%GITHUB_REPO%"
        echo.
        echo Please download the latest version from GitHub.
        echo.
        call :log_message "User chose to open GitHub manually"
        pause
    ) else if "!choice!"=="4" (
        echo.
        echo Please download the latest version from:
        echo https://github.com/%GITHUB_USER%/%GITHUB_REPO%
        echo.
        echo After updating, run this script again.
        echo.
        call :log_message "User chose to exit and update manually"
        pause
        exit /b 0
    ) else (
        echo Continuing with current version...
        echo.
        call :log_message "User chose to continue with current version"
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
call :log_message "Force update initiated"

REM Check requirements
where curl >nul 2>nul
if errorlevel 1 (
    echo ERROR: curl not available. Install from https://curl.se/windows/
    call :log_message "ERROR: Force update failed - curl not available"
    exit /b 1
)

set "use_powershell=false"
where tar >nul 2>nul
if errorlevel 1 (
    where powershell >nul 2>nul
    if errorlevel 1 (
        echo ERROR: tar and PowerShell not available.
        call :log_message "ERROR: Force update failed - no extraction tool"
        exit /b 1
    )
    set "use_powershell=true"
)

echo Force updating to latest version...

REM Create backup
for /f "tokens=2 delims==." %%I in ('wmic os get localdatetime /value') do set datetime=%%I
set "BACKUP_DIR=backup_%datetime:~0,8%_%datetime:~8,6%"
echo Creating backup in: %BACKUP_DIR%
mkdir "%BACKUP_DIR%" 2>nul
call :log_message "Created backup: %BACKUP_DIR%"

for %%F in (*.py *.txt *.html *.sh *.bat) do (
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

REM Verify download
echo Verifying download...
if "%use_powershell%"=="true" (
    powershell -command "try { Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::OpenRead('%TEMP_ZIP%').Dispose(); exit 0 } catch { exit 1 }"
) else (
    tar -tf "%TEMP_ZIP%" >nul 2>&1
)
if errorlevel 1 (
    echo x Downloaded file is corrupted!
    del "%TEMP_ZIP%" 2>nul
    call :log_message "ERROR: Force update - corrupted download"
    exit /b 1
)

set "TEMP_EXTRACT=%TEMP%\simcricketx_extract"
if exist "%TEMP_EXTRACT%" rd /s /q "%TEMP_EXTRACT%" 2>nul
mkdir "%TEMP_EXTRACT%" 2>nul

echo Extracting...
if "%use_powershell%"=="true" (
    powershell -command "Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%TEMP_EXTRACT%' -Force"
) else (
    tar -xf "%TEMP_ZIP%" -C "%TEMP_EXTRACT%"
)

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

REM Cleanup old backups
call :cleanup_old_backups

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
call :log_message "Force update completed successfully"
goto :eof

REM ============================================
REM CHECK PORT AVAILABILITY
REM ============================================
:check_port_availability
if not "%CHECK_PORT_BEFORE_START%"=="true" goto :eof

echo Checking if port %DEFAULT_PORT% is available...

REM Check using netstat
netstat -an | findstr ":%DEFAULT_PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo WARNING: Port %DEFAULT_PORT% is already in use!
    echo The application may fail to start.
    echo.
    call :log_message "WARNING: Port %DEFAULT_PORT% is already in use"
    set /p continue="Continue anyway? (y/n): "
    if /i not "!continue!"=="y" (
        echo Exiting...
        call :log_message "User chose to exit due to port conflict"
        exit /b 1
    )
) else (
    echo √ Port %DEFAULT_PORT% is available
)
goto :eof

REM ============================================
REM MAIN SCRIPT LOGIC
REM ============================================
:main_script

REM Load config file if it exists
set "CONFIG_FILE=.simcricketx.conf"
if exist "%CONFIG_FILE%" (
    echo Loading configuration from %CONFIG_FILE%...
    call "%CONFIG_FILE%"
    call :log_message "Loaded configuration from %CONFIG_FILE%"
)

REM Handle command line arguments
if /i "%~1"=="--update" (
    call :force_update
    exit /b 0
)
if /i "%~1"=="--rollback" (
    call :rollback_update "%~2"
    exit /b 0
)
if /i "%~1"=="--help" (
    echo Usage: %~nx0 [options]
    echo Options:
    echo   --update              Force update to latest version
    echo   --rollback [backup]   Rollback to a previous backup
    echo   --skip-update         Skip update check
    echo   --help                Show this help
    echo.
    echo Configuration:
    echo   Create a .simcricketx.conf file to customize behavior
    exit /b 0
)

call :log_message "========== Starting SimCricketX =========="

echo ============================================
echo Starting SimCricketX Flask App...
echo ============================================
echo.

REM Change to script directory
cd /d "%~dp0"
echo Current directory: %CD%
echo.

REM Check dependencies first
call :check_dependencies
if errorlevel 1 (
    pause
    exit /b 1
)
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
    call :log_message "ERROR: Python not found"
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
    call :log_message "ERROR: requirements.txt not found"
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
    call :log_message "ERROR: app.py not found"
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
        call :log_message "ERROR: Failed to create virtual environment"
        pause
        exit /b 1
    )
    echo Virtual environment created!
    call :log_message "Virtual environment created"
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
    call :log_message "ERROR: Failed to activate virtual environment"
    pause
    exit /b 1
)

echo Virtual environment activated!
echo.


echo Upgrading pip...
python -m pip install --upgrade pip
echo.

REM Check if requirements have changed
set "REQUIREMENTS_HASH_FILE=venv\.requirements_hash"
set "SKIP_INSTALL=false"

REM Calculate MD5 hash of requirements.txt using PowerShell
for /f "delims=" %%I in ('powershell -command "Get-FileHash -Algorithm MD5 requirements.txt | Select-Object -ExpandProperty Hash"') do set "CURRENT_HASH=%%I"

if exist "%REQUIREMENTS_HASH_FILE%" (
    set /p LAST_HASH=<"%REQUIREMENTS_HASH_FILE%"
    if "!CURRENT_HASH!"=="!LAST_HASH!" (
        echo Dependencies unchanged since last install, skipping...
        echo ^(Delete venv\.requirements_hash to force reinstall^)
        set "SKIP_INSTALL=true"
        call :log_message "Skipped dependency installation - requirements unchanged"
    )
)

if "%SKIP_INSTALL%"=="false" (
    echo Installing dependencies...
    pip install --no-cache-dir -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies.
        echo Check your internet connection and requirements.txt content.
        echo.
        call :log_message "ERROR: Failed to install dependencies"
        pause
        exit /b 1
    )
    
    REM Save the hash for next time
    echo !CURRENT_HASH! > "%REQUIREMENTS_HASH_FILE%"
    call :log_message "Dependencies installed and hash saved"
)

echo.

REM Check port availability
call :check_port_availability
if errorlevel 1 exit /b 1

echo.
echo =======================================================
echo   Starting Flask Application...
echo   Access it at: http://127.0.0.1:%DEFAULT_PORT%
echo   Press CTRL+C to stop the server
echo =======================================================
echo.

call :log_message "Starting Flask application on port %DEFAULT_PORT%"
python app.py

echo.
echo Application stopped.
call :log_message "Application stopped"
pause