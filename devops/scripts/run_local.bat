@echo off
REM ==========================================================================
REM  VidiChord - set up, build, and run locally on Windows.
REM
REM  One command from a fresh checkout to a running app:
REM    1  environment     backend\.venv, dependencies (Demucs and PyTorch
REM                       among them), and madmom if it builds
REM    2  frontend        the Angular app that the backend serves
REM    3  run             starts the server and opens a browser
REM
REM  Steps 1 and 2 skip themselves once their work is done, so an ordinary
REM  launch goes straight to the app.
REM
REM  Usage:
REM    devops\scripts\run_local.bat [--reinstall] [--rebuild] [--no-pause]
REM
REM      --reinstall    discard backend\.venv and build it again
REM      --rebuild      rebuild the Angular app even if it is already built
REM      --no-pause     do not wait for a keypress on failure (for CI)
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
REM This script lives in devops\scripts, so the repository root is two levels
REM up. Everything below is derived from ROOT.
cd /d "%~dp0..\.."

set "ROOT=%CD%"
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"
set "FRONTEND_OUT=%ROOT%\frontend\dist\frontend\browser"
set "REINSTALL=0"
set "REBUILD=0"
set "NO_PAUSE=0"
set "STEP=0"
set "RC=0"

REM Hebrew song titles are unprintable on a cp1252 console without this, and
REM the app prints them from the first song onwards.
set "PYTHONIOENCODING=utf-8"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--reinstall" (set "REINSTALL=1" & shift & goto parse_args)
REM Stem separation used to be opt-in. It is part of the install now; the flag
REM is still accepted, so an old habit or shortcut does not stop the launch.
if /i "%~1"=="--with-stems" (shift & goto parse_args)
if /i "%~1"=="--rebuild"   (set "REBUILD=1"   & shift & goto parse_args)
if /i "%~1"=="--no-pause"  (set "NO_PAUSE=1"  & shift & goto parse_args)
if /i "%~1"=="--help" goto usage
if /i "%~1"=="-h"     goto usage
if /i "%~1"=="/?"     goto usage
echo Unknown option: %~1
goto usage
:args_done

echo ==========================================================================
echo   VidiChord - run locally
echo ==========================================================================


REM --- 1. environment -------------------------------------------------------
call :step "Environment"

call :ensure_env
if not "%RC%"=="0" goto end


REM --- 2. frontend ----------------------------------------------------------
call :step "Angular frontend"

REM The backend serves this build; without it every page answers 503.
if "%REBUILD%"=="1" goto build_frontend
if not exist "%FRONTEND_OUT%\index.html" goto build_frontend

REM A build is only as good as the node_modules it came from. One installed
REM before package.json last changed - a version since pinned, say - builds
REM without a word of complaint and then fails in the browser: a library
REM compiled for a newer Angular than this one leaves every icon throwing, the
REM page frozen on its first frame. npm ls notices the mismatch; rebuild then.
if not exist "%ROOT%\frontend\node_modules" goto frontend_built
where /q npm
if errorlevel 1 goto frontend_built
cd /d "%ROOT%\frontend"
call npm ls --depth=0 >nul 2>&1
set "NPM_LS=!errorlevel!"
cd /d "%ROOT%"
if not "!NPM_LS!"=="0" (
    echo frontend\node_modules does not match package.json - reinstalling and rebuilding.
    goto build_frontend
)

:frontend_built
echo Already built. Pass --rebuild to build it again.
goto frontend_done

:build_frontend
where /q npm
if errorlevel 1 goto no_npm
cd /d "%ROOT%\frontend"
REM package-lock.json is not committed, so a fresh checkout has none and
REM "npm ci" would refuse to run.
if exist package-lock.json (
    call npm ci
    if errorlevel 1 call npm install
) else (
    call npm install
)
if errorlevel 1 goto npm_failed
call npm run build
if errorlevel 1 goto frontend_failed
cd /d "%ROOT%"
if not exist "%FRONTEND_OUT%\index.html" goto frontend_missing
echo Built %FRONTEND_OUT%
:frontend_done


REM --- 3. run ---------------------------------------------------------------
call :step "Run"

echo VidiChord serves on http://127.0.0.1:8001 and opens a browser itself.
echo This window has to stay open while the app runs; Ctrl+C stops it.
echo.
cd /d "%ROOT%\backend"
"%PY%" main.py
cd /d "%ROOT%"
goto end


REM --- failures -------------------------------------------------------------

:no_npm
echo npm was not found on PATH. The app's interface is an Angular build, so
echo Node.js and npm are needed at least once to produce it. https://nodejs.org
goto fail

:npm_failed
cd /d "%ROOT%"
echo npm could not install the frontend dependencies.
goto fail

:frontend_failed
cd /d "%ROOT%"
echo The Angular build failed.
goto fail

:frontend_missing
echo The Angular build produced no index.html in:
echo   %FRONTEND_OUT%
echo Check the outputPath in frontend\angular.json.
goto fail

:usage
echo Usage: devops\scripts\run_local.bat [--reinstall] [--rebuild] [--no-pause]
echo.
echo   Sets up backend\.venv, builds the Angular app, and runs VidiChord.
echo   Both of the first two steps are skipped when already done.
goto end

:fail
set "RC=1"
echo.
echo Could not start VidiChord.
goto end


REM --- shared -----------------------------------------------------------------

:ensure_env
REM Create backend\.venv and install everything into it. Idempotent: an
REM existing environment is reused, and --reinstall discards it first.
REM
REM Prefers Python 3.12, then 3.11: madmom - which supplies downbeat tracking
REM and one of the three chord engines - cannot be built on 3.13 or newer. The
REM app runs without it, in a reduced mode. See README.md.
if "%REINSTALL%"=="1" if exist "%ROOT%\backend\.venv" (
    echo Discarding the existing backend\.venv.
    rd /s /q "%ROOT%\backend\.venv"
)

if exist "%PY%" (
    echo Using the existing backend\.venv.
    goto ensure_deps
)

set "BOOTPY="
for %%V in (3.12 3.11 3.13) do (
    if not defined BOOTPY (
        py -%%V --version >nul 2>&1 && set "BOOTPY=py -%%V"
    )
)
if not defined BOOTPY (
    echo Could not find Python 3.11, 3.12 or 3.13 via the py launcher.
    echo Falling back to whatever "python" resolves to.
    set "BOOTPY=python"
)
echo Creating backend\.venv with: !BOOTPY!
cd /d "%ROOT%\backend"
!BOOTPY! -m venv .venv
cd /d "%ROOT%"
if not exist "%PY%" (
    echo Failed to create backend\.venv.
    set "RC=1"
    goto :eof
)

:ensure_deps
"%PY%" -m pip install --upgrade --disable-pip-version-check --quiet pip
echo Installing dependencies from backend\requirements.txt.
"%PY%" -m pip install --disable-pip-version-check -r "%ROOT%\backend\requirements.txt"
if errorlevel 1 (
    echo Dependency installation failed.
    set "RC=1"
    goto :eof
)

REM madmom is optional and awkward. All three of its quirks are handled here:
REM  1. Its setup.py imports Cython without declaring it, so pip's isolated
REM     build environment cannot see it - hence --no-build-isolation, which in
REM     turn needs setuptools and wheel present in the venv.
REM  2. The PyPI sdist ships C files generated by an old Cython that include
REM     longintrepr.h, a header Python 3.12 removed. Installing from git means
REM     there are no stale C files and Cython regenerates them.
REM  3. Its extensions must be compiled against the NumPy the venv runs. Built
REM     against NumPy 1.x and run on 2.x, "import madmom" still succeeds but
REM     every compiled module fails with "numpy.dtype size changed". So the
REM     check imports a compiled module, and the build uses the venv's NumPy
REM     (2.x - librosa and scipy require it) with --no-deps so pip cannot swap
REM     NumPy after the build; --force-reinstall rebuilds a broken copy.
"%PY%" -c "import madmom.features.downbeats" 2>nul
if not errorlevel 1 goto madmom_ready

"%PY%" -c "import sys; sys.exit(0 if sys.version_info < (3, 13) else 1)"
if errorlevel 1 (
    echo Skipping madmom: it cannot be built on Python 3.13 or newer.
    goto madmom_done
)

echo Installing madmom. This needs a C compiler and takes a minute.
"%PY%" -m pip install --disable-pip-version-check --quiet setuptools wheel "cython>=3.0" "mido>=1.2.6"
"%PY%" -m pip install --disable-pip-version-check --no-build-isolation --no-deps --force-reinstall "git+https://github.com/CPJKU/madmom.git"
if not errorlevel 1 "%PY%" -c "import madmom.features.downbeats"
if errorlevel 1 (
    echo.
    echo madmom could not be installed. This usually means the Microsoft C++
    echo Build Tools are missing. Install them with:
    echo   winget install Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools"
    echo.
    echo VidiChord still runs: bar lines are estimated rather than tracked,
    echo and chords are fused from two engines instead of three.
    goto madmom_done
)

:madmom_ready
echo madmom is available: downbeat tracking and all three chord engines.
:madmom_done

REM Demucs comes in with requirements.txt, and PyTorch with it - about 650 MB,
REM the first time. With it, every song is split into vocals, drums, bass and
REM other; the player mixes them and the lyrics are timed against the isolated
REM vocal. Checked by importing it rather than by asking pip, because an
REM install that went in but cannot load is the case worth reporting.
"%PY%" -c "import demucs.api, torch" 2>nul
if not errorlevel 1 (
    echo Stem separation is available.
    goto :eof
)
echo.
echo Demucs did not load, so songs will not be split into stems. VidiChord
echo still runs, timing the lyrics against the full mix. To see why:
echo   backend\.venv\Scripts\python -c "import demucs.api, torch"
goto :eof

:step
set /a STEP+=1
echo.
echo --------------------------------------------------------------------------
echo   [!STEP!/3] %~1
echo --------------------------------------------------------------------------
goto :eof

:end
if "%NO_PAUSE%"=="0" pause
exit /b %RC%
