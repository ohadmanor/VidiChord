@echo off
REM ==========================================================================
REM  VidiChord - set up, build, and package the Windows executable.
REM
REM  Produces release\VidiChord-<version>-win64.exe: one self-contained file
REM  holding the Angular app, ffmpeg, the Essentia binaries, the madmom models,
REM  Demucs with PyTorch for stem separation, and every Python dependency. The
REM  machine it runs on needs no Python and no
REM  installs - but it does need a JavaScript engine to download from YouTube,
REM  which is a separate program and cannot be bundled. See the closing banner.
REM
REM  Steps, in order:
REM    1  environment      backend\.venv, dependencies, and madmom if it builds
REM    2  preflight        Node.js, npm, and what the build will bundle
REM    3  version          read from vidichord\__init__.py, the one source
REM    4  build tools      PyInstaller, and the exe icon from the app logo
REM    5  tests            the whole suite; a red test stops the release
REM    6  ffmpeg           fetched now so it can be bundled, not downloaded
REM                        into a temporary folder on every launch
REM    7  frontend         Angular, production configuration
REM    8  executable       PyInstaller, single file
REM    9  package          name, size, SHA256, and a real start-up check
REM
REM  Usage:
REM    devops\scripts\build_release.bat [--reinstall] [--skip-tests]
REM                                     [--skip-smoke] [--no-pause]
REM
REM      --reinstall    discard backend\.venv and build it again
REM      --skip-tests   do not run pytest first
REM      --skip-smoke   do not launch the built exe to check that it starts
REM      --no-pause     do not wait for a keypress at the end (for CI)
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
REM This script lives in devops\scripts, so the repository root is two levels
REM up. Every path below is derived from ROOT, so this one line is what ties the
REM build to the tree it builds.
cd /d "%~dp0..\.."

set "ROOT=%CD%"
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"
set "RELEASE_DIR=%ROOT%\release"
set "WORK_DIR=%ROOT%\backend\build"
set "FRONTEND_OUT=%ROOT%\frontend\dist\frontend\browser"
set "SPEC=VidiChord_onefile.spec"
set "PORT=8001"
set "SMOKE_TIMEOUT=420"
set "REINSTALL=0"
set "SKIP_TESTS=0"
set "SKIP_SMOKE=0"
set "NO_PAUSE=0"
set "STEP=0"
set "RC=0"
set "SCRATCH=%TEMP%\vidichord_release.txt"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--reinstall"  (set "REINSTALL=1"  & shift & goto parse_args)
if /i "%~1"=="--skip-tests" (set "SKIP_TESTS=1" & shift & goto parse_args)
if /i "%~1"=="--skip-smoke" (set "SKIP_SMOKE=1" & shift & goto parse_args)
if /i "%~1"=="--no-pause"   (set "NO_PAUSE=1"   & shift & goto parse_args)
if /i "%~1"=="--help" goto usage
if /i "%~1"=="-h"     goto usage
if /i "%~1"=="/?"     goto usage
echo Unknown option: %~1
goto usage
:args_done

for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "[DateTime]::UtcNow.Ticks"`) do set "T0=%%T"

echo ==========================================================================
echo   VidiChord release build
echo ==========================================================================


REM --- 1. environment -------------------------------------------------------
call :step "Environment"

call :ensure_env
if not "%RC%"=="0" goto fail_quiet
if not exist "%PY%" goto no_venv


REM --- 2. preflight ---------------------------------------------------------
call :step "Preflight"

where /q npm
if errorlevel 1 goto no_npm

REM Captured through a file rather than `for /f`: cmd mangles a command whose
REM first token is a quoted path, which every call to the venv python is.
"%PY%" -c "import sys;print(sys.version.split()[0])" > "%SCRATCH%" 2>nul
set "PYVER="
if exist "%SCRATCH%" set /p PYVER=<"%SCRATCH%"
for /f "usebackq delims=" %%V in (`node --version 2^>nul`) do set "NODEVER=%%V"

echo Repository : %ROOT%
echo Python     : %PYVER%  (backend\.venv)
echo Node.js    : %NODEVER%

REM yt-dlp goes stale faster than anything else here: YouTube retires the
REM player clients it impersonates, and a frozen exe carries whatever was
REM installed on build day. Worth seeing before shipping.
"%PY%" -c "import yt_dlp;print(yt_dlp.version.__version__)" > "%SCRATCH%" 2>nul
set "YTDLPVER="
if exist "%SCRATCH%" set /p YTDLPVER=<"%SCRATCH%"
echo yt-dlp     : %YTDLPVER%

REM madmom needs Python 3.12 or older. Its absence is not an error - it costs
REM downbeat tracking and one of the three chord engines. See README.md.
"%PY%" -c "import madmom.features.downbeats" 2>nul
if errorlevel 1 goto no_madmom
echo madmom     : present, three chord engines will be bundled
goto preflight_done
:no_madmom
echo madmom     : MISSING - the release will estimate bar lines and fuse two
echo              chord engines instead of three. Re-run on Python 3.12 with a
echo              C compiler to get it, or accept the reduced build.
:preflight_done

REM Demucs is not optional for a release: the spec stops the build without
REM it, rather than ship an exe that says "not installed" to every user.
"%PY%" -c "import demucs.api, torch" 2>nul
if errorlevel 1 goto no_demucs
echo Demucs     : present, stem separation will be bundled
goto demucs_checked
:no_demucs
echo Demucs     : MISSING - the executable step will stop. It comes with
echo              requirements.txt; --reinstall sets the venv up again.
:demucs_checked


REM --- 3. version -----------------------------------------------------------
call :step "Version"

set "VERSION="
for /f "usebackq tokens=2 delims==" %%V in (`findstr /b /c:"__version__" "backend\vidichord\__init__.py"`) do set "VERSION=%%V"
set VERSION=%VERSION:"=%
set VERSION=%VERSION: =%
if "%VERSION%"=="" goto no_version

set "EXE_NAME=VidiChord-%VERSION%-win64.exe"
echo Building VidiChord %VERSION% as %EXE_NAME%


REM --- 4. build tools -------------------------------------------------------
call :step "Build tools"

REM Pinned to 6.x: PyInstaller 7 has not been tried against this spec, and 6.x
REM is the version whose onefile bootloader the spec is written for.
"%PY%" -m pip install --upgrade --disable-pip-version-check --quiet "pyinstaller>=6.6,<7" pillow
if errorlevel 1 goto pip_failed
"%PY%" -m PyInstaller --version > "%SCRATCH%" 2>nul
set "PIVER="
if exist "%SCRATCH%" set /p PIVER=<"%SCRATCH%"
echo PyInstaller %PIVER%

if exist "backend\VidiChord.ico" goto icon_ready
if not exist "frontend\public\VidiChord.png" goto icon_ready
echo Making backend\VidiChord.ico from the app logo.
"%PY%" -c "from PIL import Image; Image.open('frontend/public/VidiChord.png').convert('RGBA').save('backend/VidiChord.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if errorlevel 1 echo Icon conversion failed; the exe will use the default icon.
:icon_ready


REM --- 5. tests -------------------------------------------------------------
call :step "Tests"

if "%SKIP_TESTS%"=="1" goto tests_skipped
cd /d "%ROOT%\backend"
"%PY%" -m pytest tests -q
if errorlevel 1 goto tests_failed
cd /d "%ROOT%"
goto tests_done
:tests_skipped
echo Skipped (--skip-tests).
:tests_done


REM --- 6. ffmpeg ------------------------------------------------------------
call :step "ffmpeg"

if exist "backend\ffmpeg\ffmpeg.exe" if exist "backend\ffmpeg\ffprobe.exe" goto ffmpeg_ready
echo Fetching ffmpeg so the release can bundle it.
cd /d "%ROOT%\backend"
"%PY%" -c "import pathlib,shutil;from vidichord.config import FFMPEG_DIR;from vidichord.pipeline.stage1_audio import ensure_ffmpeg;s=pathlib.Path(ensure_ffmpeg());FFMPEG_DIR.mkdir(parents=True,exist_ok=True);[shutil.copy2(s/n,FFMPEG_DIR/n) for n in ('ffmpeg.exe','ffprobe.exe') if s!=FFMPEG_DIR];print('ffmpeg ready in',FFMPEG_DIR)"
cd /d "%ROOT%"
if exist "backend\ffmpeg\ffmpeg.exe" goto ffmpeg_ready
echo.
echo WARNING: ffmpeg could not be fetched. The build continues, but the exe
echo          will download it into its temporary folder on every launch.
goto ffmpeg_done
:ffmpeg_ready
echo Bundling backend\ffmpeg (ffmpeg.exe, ffprobe.exe).
:ffmpeg_done


REM --- 7. frontend ----------------------------------------------------------
call :step "Angular frontend"

REM A stale dist would be bundled as-is, source maps and all, so start clean.
if exist "%ROOT%\frontend\dist" rd /s /q "%ROOT%\frontend\dist"
cd /d "%ROOT%\frontend"
call npm ci
if errorlevel 1 call npm install
if errorlevel 1 goto npm_failed
call npm run build -- --configuration production
if errorlevel 1 goto frontend_failed
cd /d "%ROOT%"
if not exist "%FRONTEND_OUT%\index.html" goto frontend_missing
echo Built %FRONTEND_OUT%


REM --- 8. executable --------------------------------------------------------
call :step "Single-file executable"
echo This is the slow part: PyInstaller walks every dependency and compresses
echo the lot into one file. Several minutes is normal.
echo.

if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"
if exist "%RELEASE_DIR%\VidiChord.exe" del /q "%RELEASE_DIR%\VidiChord.exe"

cd /d "%ROOT%\backend"
"%PY%" -m PyInstaller "%SPEC%" --noconfirm --clean --distpath "%RELEASE_DIR%" --workpath "%WORK_DIR%"
if errorlevel 1 goto pyinstaller_failed
cd /d "%ROOT%"
if not exist "%RELEASE_DIR%\VidiChord.exe" goto exe_missing


REM --- 9. package -----------------------------------------------------------
call :step "Package"

set "EXE_PATH=%RELEASE_DIR%\%EXE_NAME%"
if exist "%EXE_PATH%" del /q "%EXE_PATH%"
move /y "%RELEASE_DIR%\VidiChord.exe" "%EXE_PATH%" >nul
if errorlevel 1 goto rename_failed

for %%A in ("%EXE_PATH%") do set "BYTES=%%~zA"
set /a MB=%BYTES%/1048576

set "SHA256="
for /f "usebackq skip=1 delims=" %%H in (`certutil -hashfile "%EXE_PATH%" SHA256`) do if not defined SHA256 set "SHA256=%%H"
set SHA256=%SHA256: =%

if "%SKIP_SMOKE%"=="1" goto smoke_skipped

REM Nothing but starting the thing proves a bundle is complete: a missing
REM hidden import only surfaces at run time, and pytest never sees the exe.
REM The check gets a port of its own through VIDICHORD_PORT - a developer's own
REM server is usually sitting on %PORT%, and that is no reason to skip it.
powershell -NoProfile -Command "foreach($p in 8801..8809){ if(-not (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)){ $p; break } }" > "%SCRATCH%"
set "CHECK_PORT="
if exist "%SCRATCH%" set /p CHECK_PORT=<"%SCRATCH%"
if "%CHECK_PORT%"=="" goto smoke_busy

echo Start-up check: running the exe on port %CHECK_PORT% until it serves
echo /api/config. It unpacks itself first, so give it a moment.
set "VIDICHORD_NO_BROWSER=1"
set "VIDICHORD_PORT=%CHECK_PORT%"
start "VidiChord start-up check" /min "%EXE_PATH%"
powershell -NoProfile -Command "$sw=[Diagnostics.Stopwatch]::StartNew(); while($sw.Elapsed.TotalSeconds -lt %SMOKE_TIMEOUT%){ try{ if((Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%CHECK_PORT%/api/config' -TimeoutSec 5).StatusCode -eq 200){ exit 0 } }catch{}; Start-Sleep -Seconds 3 }; exit 1"
set "SMOKE_RC=!errorlevel!"
taskkill /f /im "%EXE_NAME%" /t >nul 2>&1
set "VIDICHORD_PORT="
if not "%SMOKE_RC%"=="0" goto smoke_failed
echo The exe unpacked, started, and served /api/config on port %CHECK_PORT%.
goto smoke_done
:smoke_busy
echo Skipped: ports 8801-8809 are all in use, so there is nowhere to start a
echo test instance. Free one of them and re-run.
goto smoke_done
:smoke_skipped
echo Skipped (--skip-smoke).
:smoke_done

for /f "usebackq delims=" %%E in (`powershell -NoProfile -Command "[math]::Round(([DateTime]::UtcNow.Ticks - %T0%)/10000000)"`) do set "ELAPSED=%%E"

echo.
echo ==========================================================================
echo   Release ready
echo ==========================================================================
echo   File     : %EXE_PATH%
echo   Version  : %VERSION%
echo   Size     : %MB% MB
echo   SHA256   : %SHA256%
echo   Built in : %ELAPSED%s
echo.
echo   Hand over that one file. Every launch unpacks the bundle to a temporary
echo   folder before the server starts, so give it a minute; the console window
echo   shows what it is doing and must stay open while the app runs.
echo.
echo   config.json and the VidiChord_Files library are written next to the exe,
echo   so keep it somewhere writable - a normal folder, not Program Files.
echo   The target machine needs a JavaScript engine for YouTube downloads -
echo   Node.js, Deno, Bun or QuickJS. YouTube signs its download links and
echo   unscrambling them means running the player's own code, which no bundle
echo   can do for it. Installing Node.js is the usual answer; node.exe beside
echo   VidiChord.exe works too. Local audio files need none of this.
echo.
echo   The first song also downloads the Whisper models from Hugging Face,
echo   about 2 GB, once.
echo.
goto end


REM --- failures -------------------------------------------------------------

:no_venv
echo The environment step finished but backend\.venv\Scripts\python.exe is
echo still missing. Re-run with --reinstall.
goto fail

:no_npm
echo npm was not found on PATH. The release includes the Angular app, so
echo Node.js and npm are needed to build it. https://nodejs.org
goto fail

:no_version
echo Could not read __version__ from backend\vidichord\__init__.py.
goto fail

:pip_failed
echo Could not install PyInstaller into backend\.venv.
goto fail

:tests_failed
cd /d "%ROOT%"
echo.
echo Tests failed - not building a release from a red suite.
echo Fix them, or re-run with --skip-tests if you know what you are shipping.
goto fail

:npm_failed
cd /d "%ROOT%"
echo npm could not install the frontend dependencies.
goto fail

:frontend_failed
cd /d "%ROOT%"
echo The Angular production build failed.
goto fail

:frontend_missing
echo The Angular build produced no index.html in:
echo   %FRONTEND_OUT%
echo Check the outputPath in frontend\angular.json.
goto fail

:pyinstaller_failed
cd /d "%ROOT%"
echo PyInstaller failed. The warnings file usually names the culprit:
echo   %WORK_DIR%\VidiChord\warn-VidiChord.txt
goto fail

:exe_missing
echo PyInstaller reported success but %RELEASE_DIR%\VidiChord.exe is not there.
goto fail

:rename_failed
echo Could not rename the exe to %EXE_NAME%. Is an older copy still running?
goto fail

:smoke_failed
echo.
echo The exe did not answer on port %CHECK_PORT% within %SMOKE_TIMEOUT% seconds.
echo The file is at %EXE_PATH% - run it from a console to see why. A missing
echo hidden import in %SPEC% is the usual cause.
goto fail

:usage
echo Usage: devops\scripts\build_release.bat [--reinstall] [--skip-tests] [--skip-smoke] [--no-pause]
echo.
echo   Sets up backend\.venv, builds the Angular app, and packages
echo   release\VidiChord-^<version^>-win64.exe, one self-contained file.
goto end

:fail_quiet
set "RC=1"
goto end

:fail
set "RC=1"
echo.
echo Release build FAILED.
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

REM The exe carries whatever yt-dlp is installed on build day. It updates
REM itself from PyPI as YouTube changes, into VidiChord_yt-dlp beside it, but
REM the bundled copy is what it falls back on - so every release ships the
REM newest one there is.
echo Updating yt-dlp to the newest release, for the bundle.
"%PY%" -m pip install --disable-pip-version-check --quiet --upgrade "yt-dlp[default]"
if errorlevel 1 echo Could not update yt-dlp. The release will carry the version already installed.

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
    echo VidiChord still builds: bar lines are estimated rather than tracked,
    echo and chords are fused from two engines instead of three.
    goto madmom_done
)

:madmom_ready
echo madmom is available: downbeat tracking and all three chord engines.
:madmom_done
goto :eof

:step
set /a STEP+=1
echo.
echo --------------------------------------------------------------------------
echo   [!STEP!/9] %~1
echo --------------------------------------------------------------------------
goto :eof

:end
if exist "%SCRATCH%" del /q "%SCRATCH%"
if "%NO_PAUSE%"=="0" pause
exit /b %RC%
