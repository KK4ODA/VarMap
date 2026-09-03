@echo off
title VarMap - Build standalone EXE
cd /d "%~dp0"

where python >nul 2>&1 || (echo Python not found & pause & exit /b 1)
python -c "import PyInstaller" 2>nul || python -m pip install pyinstaller --quiet
python -m pip install -r requirements.txt --quiet

echo Building one-folder distribution (dist\VarMap\VarMap.exe)...
python -m PyInstaller ^
    --name VarMap ^
    --noconfirm --clean ^
    --add-data "varmap\web\static;varmap\web\static" ^
    --add-data "varmap\web\templates;varmap\web\templates" ^
    --add-data "varmap\storage\schema.sql;varmap\storage" ^
    --hidden-import waitress ^
    --hidden-import win32gui ^
    --hidden-import win32con ^
    --hidden-import serial ^
    --collect-submodules varmap ^
    varmap_launcher.py

if %ERRORLEVEL% NEQ 0 (echo Build failed & pause & exit /b 1)
echo.
echo Done: dist\VarMap\VarMap.exe   (config.json is created beside the exe on first run)
pause
