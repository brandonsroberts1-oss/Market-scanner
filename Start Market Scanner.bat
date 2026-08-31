@echo off
REM ===================================================================
REM  Market Scanner - double-click this file to start the app.
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Market Scanner

REM --- Find a usable Python -----------------------------------------
REM The "py" launcher is the most reliable on Windows. Fall back to
REM "python", but skip the Microsoft Store stub, which is not a real
REM Python and only opens the Store when you run it.
set "PYEXE="

py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"

if not defined PYEXE (
    python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
    python3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYEXE=python3"
)

if not defined PYEXE (
    echo.
    echo   ------------------------------------------------------------
    echo    PYTHON IS NOT INSTALLED ^(or is too old^)
    echo   ------------------------------------------------------------
    echo.
    echo    Market Scanner needs Python 3.10 or newer.
    echo.
    echo    1. Download it from   https://www.python.org/downloads/
    echo    2. Run the installer.
    echo    3. IMPORTANT: tick "Add python.exe to PATH" on the first
    echo       screen of the installer before clicking Install.
    echo    4. Double-click this file again.
    echo.
    echo    Opening the download page for you now...
    echo.
    start "" "https://www.python.org/downloads/"
    echo   Press any key to close this window.
    pause >nul
    exit /b 1
)

REM --- Hand over to the launcher ------------------------------------
%PYEXE% "%~dp0launcher.py"
set "EXITCODE=%ERRORLEVEL%"

REM Keep the window open on failure so the error is readable, instead
REM of the window flashing shut the way a plain script would.
if not "%EXITCODE%"=="0" (
    echo.
    echo   The app stopped with an error ^(code %EXITCODE%^).
    echo   Press any key to close this window.
    pause >nul
)
exit /b %EXITCODE%
