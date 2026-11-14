@echo off
setlocal
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" tests\test_performance.py
) else (
    python tests\test_performance.py
)
if %errorlevel% neq 0 pause
