@echo off
set PYTHONIOENCODING=utf-8
echo Starting VidiChord Backend...
call .venv\Scripts\activate.bat
python main.py
pause
