@echo off
REM Build a standalone VidiChord.exe: Angular app first, then PyInstaller.
setlocal
cd /d "%~dp0"

echo === Building the Angular frontend ===
pushd frontend
call npm ci
if errorlevel 1 call npm install
call npm run build
if errorlevel 1 (
    echo Frontend build failed.
    popd
    pause
    exit /b 1
)
popd

echo.
echo === Building the executable ===
pushd backend
if not exist ".venv\Scripts\python.exe" (
    echo No virtual environment found. Run backend\setup.bat first.
    popd
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pyinstaller

pyinstaller VidiChord.spec --noconfirm
if errorlevel 1 (
    echo PyInstaller failed.
    popd
    pause
    exit /b 1
)
popd

echo.
echo Done. The bundle is in:
echo   %CD%\backend\dist\VidiChord\VidiChord.exe
pause
