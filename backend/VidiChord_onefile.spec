# -*- mode: python ; coding: utf-8 -*-
"""Single-file PyInstaller build for VidiChord.

Produces one self-contained ``VidiChord.exe`` holding the Angular app, ffmpeg,
the Essentia binaries and every Python dependency, so the machine it runs on
needs no Python, no Node and no installs.

Build it with ``release_windows.bat`` from the repository root, which builds
the frontend, runs the tests and stamps the version first. The sibling
``VidiChord.spec`` builds the same app as a folder instead, which starts far
faster; this one trades that away for having a single file to hand over.

Onefile means the bootloader unpacks the whole bundle - a good fraction of a
gigabyte - into a temporary directory on *every* launch, before any Python
runs. That wait is why the console window stays on: without it the delay looks
like a hang.

Written for PyInstaller 6.x. It deliberately avoids the ``cipher`` and
``win_private_assemblies`` arguments that version removed.
"""

import re
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

BACKEND_DIR = Path(SPECPATH).resolve()
REPO_DIR = BACKEND_DIR.parent
FRONTEND_DIST = REPO_DIR / "frontend" / "dist" / "frontend" / "browser"
ESSENTIA_DIR = BACKEND_DIR / "vidichord" / "vendor" / "essentia"
FFMPEG_DIR = BACKEND_DIR / "ffmpeg"
BUILD_DIR = BACKEND_DIR / "build"

# --- sanity checks ---------------------------------------------------------
# Failing here is far kinder than shipping an exe that serves a blank page or
# cannot decode audio.

if not (FRONTEND_DIST / "index.html").is_file():
    raise SystemExit(
        f"The Angular app is not built: no index.html under {FRONTEND_DIST}.\n"
        "Run release_windows.bat, or `npm run build` in frontend/."
    )

if not (ESSENTIA_DIR / "bin" / "streaming_extractor_music.exe").is_file():
    raise SystemExit(
        f"The Essentia binaries are missing from {ESSENTIA_DIR}.\n"
        "One of the three chord engines lives there; the build needs them."
    )


# --- version ---------------------------------------------------------------

def _read_version() -> str:
    """Pull ``__version__`` out of the package, the single source of truth."""
    source = (BACKEND_DIR / "vidichord" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.M)
    if not match:
        raise SystemExit("Could not find __version__ in vidichord/__init__.py")
    return match.group(1)


VERSION = _read_version()

# Windows wants exactly four numbers; "1.0.0" becomes (1, 0, 0, 0).
_parts = [int(part) for part in re.findall(r"\d+", VERSION)][:4]
_parts += [0] * (4 - len(_parts))
FILEVERS = tuple(_parts)

VERSION_RESOURCE = BUILD_DIR / "VidiChord_version.txt"
BUILD_DIR.mkdir(parents=True, exist_ok=True)
VERSION_RESOURCE.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={FILEVERS},
    prodvers={FILEVERS},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('FileDescription', 'VidiChord - chord sheets from YouTube links'),
         StringStruct('FileVersion', '{VERSION}'),
         StringStruct('InternalName', 'VidiChord'),
         StringStruct('OriginalFilename', 'VidiChord.exe'),
         StringStruct('ProductName', 'VidiChord'),
         StringStruct('ProductVersion', '{VERSION}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
    encoding="utf-8",
)

ICON = BACKEND_DIR / "VidiChord.ico"
ICON_ARG = str(ICON) if ICON.is_file() else None


# --- payload ---------------------------------------------------------------

datas = [
    # The server serves this directory; config.frontend_dir() looks for it
    # under sys._MEIPASS first.
    (str(FRONTEND_DIST), "frontend/dist/frontend/browser"),
    # config.ESSENTIA_BIN resolves relative to the package directory, so this
    # has to land beside the (frozen) vidichord package, DLLs included.
    (str(ESSENTIA_DIR), "vidichord/vendor/essentia"),
]

# ffmpeg is normally downloaded on first use, but in a onefile build the
# download target lives inside the temporary extraction directory and is thrown
# away when the app exits - so it would be re-fetched on every launch. Bundling
# it is what makes the single file genuinely standalone.
if (FFMPEG_DIR / "ffmpeg.exe").is_file() and (FFMPEG_DIR / "ffprobe.exe").is_file():
    datas.append((str(FFMPEG_DIR), "ffmpeg"))
else:
    print("*** WARNING: backend/ffmpeg is missing ffmpeg.exe/ffprobe.exe.")
    print("*** The exe will download ffmpeg on every launch. Run")
    print("*** release_windows.bat, which fetches it before building.")

binaries = []
hiddenimports = [
    # uvicorn picks its loop, protocol and lifespan implementations by name at
    # runtime, so the analysis cannot see them.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "ctranslate2",
    "soundfile",
    "soxr",
    "audioread",
    "pooch",
    "sklearn.utils._typedefs",
    "sklearn.neighbors._partition_nodes",
]

# librosa lazy-loads its submodules, faster-whisper ships the Silero VAD model
# as a data file, and yt-dlp imports its extractors by name - every one of
# these is invisible to static analysis.
for package in ("vidichord", "librosa", "faster_whisper", "yt_dlp"):
    hiddenimports += collect_submodules(package)

# yt-dlp and yt_dlp_ejs carry the JavaScript that answers YouTube's signature
# challenges, and read it back with importlib.resources - so it has to be
# bundled as data or every download is refused in the frozen build.
for package in ("librosa", "faster_whisper", "_soundfile_data", "yt_dlp", "yt_dlp_ejs"):
    try:
        datas += collect_data_files(package)
    except Exception as error:  # a missing optional package must not stop the build
        print(f"*** Note: no data files collected for {package}: {error}")

# That loop cannot report the failure that matters: collect_data_files returns
# an empty list for a package that is not installed rather than raising, so a
# venv predating yt-dlp-ejs would build a green exe that refuses every YouTube
# download - the exact bug this release fixes. Nothing else catches it either:
# the test suite never builds a bundle, and the start-up check only asks the
# exe for /api/config. So check here, where it can still stop the build.
#
# The two packages must also agree: yt-dlp verifies the solver script's version
# and hash against its own vendored manifest and silently discards a mismatch,
# leaving Node with no script to run.
try:
    import yt_dlp_ejs
    from yt_dlp.extractor.youtube.jsc._builtin import vendor as _yt_dlp_vendor
except ImportError as error:
    raise SystemExit(
        f"*** {error}\n"
        "*** yt-dlp-ejs supplies the JavaScript that signs YouTube download\n"
        "*** links; without it the exe cannot download anything from YouTube.\n"
        "*** Run backend\\setup.bat, or: .venv\\Scripts\\pip install -r requirements.txt"
    )

if yt_dlp_ejs.version.split(".")[:2] != _yt_dlp_vendor.VERSION.split(".")[:2]:
    raise SystemExit(
        f"*** yt-dlp-ejs {yt_dlp_ejs.version} does not match the {_yt_dlp_vendor.VERSION}\n"
        "*** that this yt-dlp expects, so its solver script would be rejected at\n"
        "*** run time and YouTube downloads would fail. Reinstall the pair together:\n"
        "***   .venv\\Scripts\\pip install -U yt-dlp yt-dlp-ejs"
    )

# collect_data_files yields (source file, destination *directory*), so the
# filename to test is the first element.
_solver_scripts = [source for source, _ in datas if str(source).endswith(".js")]
if not _solver_scripts:
    raise SystemExit(
        "*** No JavaScript solver scripts were collected, so the exe could not\n"
        "*** sign a YouTube download link. Expected them from yt_dlp and yt_dlp_ejs."
    )
print(f"*** Bundling yt-dlp-ejs {yt_dlp_ejs.version} ({len(_solver_scripts)} solver scripts).")

for package in ("ctranslate2", "onnxruntime", "av"):
    try:
        binaries += collect_dynamic_libs(package)
    except Exception as error:
        print(f"*** Note: no dynamic libraries collected for {package}: {error}")

# madmom is optional: it supplies downbeat tracking and the third chord engine,
# and needs Python <= 3.12 plus a C compiler. Without it the app still runs, so
# the build must not require it - but when it is installed its neural network
# models have to come along or every engine call fails at runtime.
try:
    import madmom  # noqa: F401

    hiddenimports += collect_submodules("madmom")
    datas += collect_data_files("madmom")
    print(f"*** Bundling madmom {madmom.__version__} (three chord engines).")
except Exception:
    print("*** Building WITHOUT madmom: bar lines estimated, two chord engines.")

a = Analysis(
    [str(BACKEND_DIR / "main.py")],
    pathex=[str(BACKEND_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here is imported by the app, and each one costs tens of
    # megabytes in a file users have to download.
    excludes=[
        "tkinter",
        "matplotlib",
        "pytest",
        "_pytest",
        "Cython",
        "pyximport",
        "IPython",
        "notebook",
        # Pillow is a build-time dependency only - it converts the app logo
        # into the exe icon. The analysis reaches it through
        # sklearn.datasets, where the import sits inside a try/except in a
        # function VidiChord never calls.
        "PIL",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VidiChord",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off on purpose. It mangles the ctranslate2 and onnxruntime DLLs
    # this app loads, and packed executables trip antivirus heuristics - a bad
    # trade for a file people are asked to download and trust.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Extraction takes a while on every launch; the console makes the wait
    # visible and carries the pipeline's progress output.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_ARG,
    version=str(VERSION_RESOURCE),
)
