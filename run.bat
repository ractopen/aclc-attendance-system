@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title ACLC Attendance ^& Evaluation Server

echo ============================================================
echo   ACLC College of Mandaue - HIV Awareness Seminar
echo   Offline Attendance, Evaluation ^& Demographics System
echo ============================================================
echo.

:: 1. Detect working Python 3 interpreter (supports python or py -3)
set "PY_CMD="
python -c "import sys; assert sys.version_info >= (3, 8)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=python"
) else (
    py -3 -c "import sys; assert sys.version_info >= (3, 8)" >nul 2>nul
    if !errorlevel! equ 0 (
        set "PY_CMD=py -3"
    )
)

if "%PY_CMD%"=="" (
    echo [ERROR] Python 3.8+ was not found on this Windows machine!
    echo Please install Python 3.10 or newer from https://www.python.org/
    echo IMPORTANT: Check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

:: 2. If venv was copied from Linux or another folder and is invalid, rebuild it
set "NEED_VENV=0"
if not exist "venv\Scripts\python.exe" (
    set "NEED_VENV=1"
) else (
    "venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if !errorlevel! neq 0 set "NEED_VENV=1"
)

if "%NEED_VENV%"=="1" (
    if exist venv (
        echo [INFO] Cleaning up non-Windows or relocated virtual environment...
        rmdir /s /q venv
    )
    echo [INFO] Creating clean Windows virtual environment...
    %PY_CMD% -m venv venv
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to create virtual environment. Please check your Python setup.
        pause
        exit /b 1
    )
    echo [INFO] Installing dependencies from requirements.txt...
    "venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
)

:: 3. Verify all required packages are ready
"venv\Scripts\python.exe" -c "import fastapi, uvicorn, qrcode, PIL, jinja2, user_agents" >nul 2>nul
if %errorlevel% neq 0 (
    echo [INFO] Installing required dependencies from requirements.txt...
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
)

:: 4. Detect available port (8080 default, fallback 8000/8081) and active LAN IP
for /f %%i in ('"venv\Scripts\python.exe" -c "import socket; p=next((x for x in (8080,8000,8081,8888) if socket.socket(socket.AF_INET,socket.SOCK_STREAM).connect_ex(('127.0.0.1',x))!=0),8080); print(p)"') do set "PORT=%%i"
if "%PORT%"=="" set "PORT=8080"

for /f %%i in ('"venv\Scripts\python.exe" -c "from main import get_local_ip; print(get_local_ip())"') do set "LAN_IP=%%i"
if "%LAN_IP%"=="" set "LAN_IP=192.168.1.100"

:: 5. Display Quick Access URLs
echo.
echo ============================================================
echo   SERVER READY ^& LISTENING ON PORT %PORT%
echo ============================================================
echo   * Student Page (Local):       http://localhost:%PORT%/
echo   * Student Page (Wi-Fi/LAN):   http://%LAN_IP%:%PORT%/
echo   * Projector Big Screen:       http://localhost:%PORT%/projector
echo   * Admin Control Dashboard:    http://localhost:%PORT%/admin
echo   * Exit QR Pass Scanner:       http://localhost:%PORT%/admin/scanner
echo   * Admin Master Password:      nevergrowold
echo ============================================================
echo   Note: Connect student phones to the same Wi-Fi router or
echo   hotspot and scan the Projector QR code (http://%LAN_IP%:%PORT%/)
echo ============================================================
echo.
echo Press Ctrl+C in this window at any time to stop the server.
echo.

"venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port %PORT% --reload
pause
