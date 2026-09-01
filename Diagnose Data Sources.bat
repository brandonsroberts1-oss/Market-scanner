@echo off
REM ===================================================================
REM  Market Scanner - check why market data is not loading.
REM  Double-click this file. It prints what every data source returned.
REM ===================================================================
setlocal
cd /d "%~dp0"
title Market Scanner - Data Diagnostics

set "PYEXE="
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
    python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo.
    echo   Python 3.10 or newer was not found. Install it from
    echo   https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    echo.
    pause >nul
    exit /b 1
)

%PYEXE% "%~dp0launcher.py" --diagnose

echo.
echo   Copy the text above if you need to report the problem.
echo   Press any key to close this window.
pause >nul
