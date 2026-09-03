@echo off
setlocal EnableDelayedExpansion
title VarMap - VarAC Position Map
color 0B
cd /d "%~dp0"

echo.
echo  ========================================
echo    VarMap - VarAC position mapping
echo  ========================================
echo.

where python >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo  ERROR: Python is not installed or not in PATH.
    echo  Install Python 3.11+ from https://www.python.org/downloads/
    echo  and tick "Add Python to PATH".
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  Found %%v

python -c "import flask" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo  Installing dependencies...
    python -m pip install -r requirements.txt
)
python -c "import waitress" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    python -m pip install waitress >nul 2>&1
)
python -c "import win32gui" >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo  pywin32 missing - transmit features need it: python -m pip install pywin32
)

echo.
echo  Starting VarMap. The browser opens automatically; press Ctrl+C to stop.
echo.
python -m varmap %*

echo.
echo  VarMap has stopped.
pause
endlocal
