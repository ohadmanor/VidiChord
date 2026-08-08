@echo off
REM Start VidiChord from source. Run setup.bat first if .venv does not exist.
setlocal
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No virtual environment found.
    echo Run setup.bat first to create one and install dependencies.
    pause
    exit /b 1
)

echo Starting VidiChord...
call .venv\Scripts\activate.bat
python main.py
pause
