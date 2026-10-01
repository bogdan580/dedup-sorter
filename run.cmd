@echo off
rem Review interface launcher: starts dedup.py --review
rem on fixed port 8765 with 8 decoding workers. Additional arguments
rem passed to run.cmd go unmodified to dedup.py (e.g. --no-browser).
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo "python" not found in PATH. Install Python and try again.
    pause
    exit /b 1
)

rem Ensure the pinned ffmpeg build is present (downloads it on first run).
rem A failure only disables the compression tab, so the review still starts.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\fetch-ffmpeg.ps1"
if errorlevel 1 (
    echo WARNING: ffmpeg is not available -- the compression tab will not work.
    echo.
)

rem Archive directory is always parent to .dedup -- the same
rem root that dedup.py resolves as DEFAULT_ARCHIVE_ROOT.
for %%I in ("%~dp0..") do set "ARCHIVE_ROOT=%%~fI"
set "NEW_DIR=%ARCHIVE_ROOT%\!NEW"

if not exist "%NEW_DIR%\" (
    echo Main "!NEW" directory not found in the archive:
    echo     %NEW_DIR%
    echo.
    echo Create it first, e.g.:  mkdir "%NEW_DIR%"
    echo or run "python .dedup\dedup.py" from the archive root for a report
    echo without reviewing !NEW. Aborting.
    pause
    exit /b 1
)

dir /b "%NEW_DIR%" 2>nul | findstr /r "." >nul
if errorlevel 1 (
    echo WARNING: "!NEW" directory exists, but is empty:
    echo     %NEW_DIR%
    echo Review will start, but Inbox tab will not show anything new.
    echo.
)

echo !NEW directory: %NEW_DIR%
echo Review started: http://127.0.0.1:8765/
echo.
python dedup.py --review --port 8765 --workers 8 -y %*
echo.
pause
endlocal
