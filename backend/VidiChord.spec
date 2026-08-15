# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for VidiChord.

Bundles the Angular app, ffmpeg and the Essentia binaries alongside the Python
package, so the result runs with no external installs.

Build with `build_windows_exe.bat` from the repository root, which builds the
frontend first.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Data directories, as (source, destination-in-bundle) pairs.
datas = [
    ("../frontend/dist/frontend/browser", "frontend/dist/frontend/browser"),
    ("vidichord/vendor/essentia", "vidichord/vendor/essentia"),
]

# ffmpeg is downloaded on first run, so it may not exist at build time.
import os
if os.path.isdir("ffmpeg"):
    datas.append(("ffmpeg", "ffmpeg"))

# librosa and faster-whisper ship data files their loaders read at runtime, and
# yt-dlp and yt_dlp_ejs ship the JavaScript that answers YouTube's signature
# challenges - without it downloads are refused in the frozen build.
for package in ("librosa", "faster_whisper", "yt_dlp", "yt_dlp_ejs"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "faster_whisper",
    "ctranslate2",
    "librosa",
    "soundfile",
    "yt_dlp",
    "numpy",
    "scipy",
    "sklearn.utils._typedefs",
    "sklearn.neighbors._partition_nodes",
]
hiddenimports += collect_submodules("vidichord")

# madmom is optional; the app degrades gracefully without it.
try:
    import madmom  # noqa: F401

    hiddenimports += collect_submodules("madmom")
    datas += collect_data_files("madmom")
except Exception:
    pass

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VidiChord",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Console stays on: extraction takes minutes and prints useful progress.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="VidiChord",
)
