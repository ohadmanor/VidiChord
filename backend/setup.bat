@echo off
REM Create the virtual environment and install dependencies.
REM
REM Prefers Python 3.12, then 3.11, because madmom - which supplies downbeat
REM tracking and one of the three chord engines - has no wheels beyond 3.12.
REM Any Python 3.11+ works; without madmom the app runs in a reduced mode.
setlocal
cd /d "%~dp0"

set PY=
for %%V in (3.12 3.11 3.13) do (
    if not defined PY (
        py -%%V --version >nul 2>&1 && set PY=py -%%V
    )
)
if not defined PY (
    echo Could not find Python 3.11, 3.12 or 3.13 via the py launcher.
    echo Falling back to whatever "python" resolves to.
    set PY=python
)

echo Creating virtual environment with: %PY%
%PY% -m venv .venv
if errorlevel 1 (
    echo Failed to create the virtual environment.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip

REM madmom builds from source and needs Cython and NumPy present first.
python -m pip install "cython<3" "numpy<2"
python -m pip install -r requirements.txt

python -c "import madmom" 2>nul
if errorlevel 1 (
    echo.
    echo NOTE: madmom is not installed. VidiChord will still run, but bar
    echo lines will be estimated rather than tracked and chords will be fused
    echo from two engines instead of three.
)

echo.
echo Setup complete. Run start_backend.bat to launch VidiChord.
pause
