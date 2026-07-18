@echo off
echo Building Angular Frontend...
cd frontend
call npm run build
if %errorlevel% neq 0 (
    echo Frontend build failed!
    pause
    exit /b %errorlevel%
)
cd ..

echo Installing PyInstaller...
cd backend
call .venv\Scripts\activate
pip install pyinstaller

echo Running PyInstaller...
pyinstaller VidiChord.spec --noconfirm

echo Done!
echo The bundled executable is located in:
echo C:\Develop\Github\VidiChord\backend\dist\VidiChord\VidiChord.exe
pause
