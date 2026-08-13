"""Application settings and filesystem locations.

Replaces the old ``config_manager`` module. Two directories are configurable:

``library_dir``
    Root of the song library. Each song gets one folder underneath it holding
    its audio plus one JSON artifact per pipeline stage (see :mod:`project`).

``sheets_dir``
    Where songbook exports are written. Usually points at an external songbook
    checkout, which is why it is kept separate from the library.

There are no secrets here. Nothing in VidiChord calls a paid API: lyrics come
from LRClib and Genius, and transcription runs locally.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Directory holding this package (``backend/vidichord``).
PACKAGE_DIR = Path(__file__).resolve().parent

# ``backend/`` - the application root when running from source.
BACKEND_DIR = PACKAGE_DIR.parent

# Repository root when running from source; irrelevant when frozen.
REPO_DIR = BACKEND_DIR.parent

# Bundled third-party binaries. PyInstaller unpacks datas into ``sys._MEIPASS``.
_BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", BACKEND_DIR))

# Where user data belongs. ``_MEIPASS`` will not do: in a single-file build it
# is a temporary directory the bootloader deletes on exit, so settings would be
# forgotten and the song library would vanish between launches. Beside the
# executable it persists, and the app stays portable - copy the exe and its
# folder anywhere and the library follows.
_FROZEN = bool(getattr(sys, "frozen", False))
DATA_DIR = Path(sys.executable).resolve().parent if _FROZEN else BACKEND_DIR

CONFIG_PATH = DATA_DIR / "config.json"

FFMPEG_DIR = _BUNDLE_ROOT / "ffmpeg"
ESSENTIA_BIN = (
    PACKAGE_DIR / "vendor" / "essentia" / "bin" / "streaming_extractor_music.exe"
)

# Where the compiled Angular app lives, checked in bundle-first order.
_FRONTEND_CANDIDATES = (
    _BUNDLE_ROOT / "frontend" / "dist" / "frontend" / "browser",
    REPO_DIR / "frontend" / "dist" / "frontend" / "browser",
)

DEFAULT_LIBRARY_DIR = (DATA_DIR if _FROZEN else REPO_DIR) / "VidiChord_Files"

# Whether spawning helper processes is affordable. In a onefile build every
# spawned worker re-runs the executable, and the bootloader re-extracts the
# whole bundle to a fresh temp directory per process - so what saves minutes
# in a normal install costs more than it saves there. Onefile extraction dirs
# are named "_MEIxxxx"; a onedir bundle lives in "_internal" beside the exe.
PROCESS_POOLS_OK = not (_FROZEN and _BUNDLE_ROOT.name.startswith("_MEI"))


def int_env(name: str, default: int, minimum: int = 1) -> int:
    """An integer tuning knob from the environment, clamped to be usable.

    Anything that does not parse as a non-negative integer falls back to the
    default, so a typo degrades to the built-in behaviour instead of crashing
    a worker mid-pipeline.
    """
    raw = os.environ.get(name, "").strip()
    if not raw.isdigit():
        return default
    return max(minimum, int(raw))


def _port() -> int:
    """The port to serve on, overridable for when 8001 is already taken.

    The release build's start-up check relies on this: a developer's own server
    is usually sitting on 8001, and the check must not be skipped just because
    of that.
    """
    raw = os.environ.get("VIDICHORD_PORT", "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return 8001


PORT = _port()


def frontend_dir() -> Path | None:
    """Return the directory holding the built Angular app, or None if unbuilt."""
    for candidate in _FRONTEND_CANDIDATES:
        if (candidate / "index.html").is_file():
            return candidate
    return None


class Settings:
    """User-editable settings, persisted to ``backend/config.json``."""

    def __init__(self, library_dir: str | os.PathLike[str] | None = None,
                 sheets_dir: str | os.PathLike[str] | None = None,
                 cookies_file: str | os.PathLike[str] | None = None,
                 cookies_browser: str | None = None,
                 path: Path | None = None) -> None:
        self.library_dir = Path(library_dir) if library_dir else DEFAULT_LIBRARY_DIR
        # Empty means "not configured"; export refuses to run until it is set.
        self.sheets_dir = Path(sheets_dir) if sheets_dir else None
        #: A Netscape-format cookie jar, so YouTube requests are made as a
        #: signed-in user. See :mod:`vidichord.pipeline.stage1_audio`.
        self.cookies_file = Path(cookies_file) if cookies_file else None
        #: A browser to read those cookies from instead, e.g. "firefox" or
        #: "chrome:Profile 1". Ignored when ``cookies_file`` is set.
        self.cookies_browser = (cookies_browser or "").strip()
        #: Where :meth:`save` writes. Overridable so tests never touch the
        #: user's real config file.
        self.path = Path(path) if path else CONFIG_PATH

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, str]:
        return {
            "library_dir": str(self.library_dir),
            "sheets_dir": str(self.sheets_dir) if self.sheets_dir else "",
            "cookies_file": str(self.cookies_file) if self.cookies_file else "",
            "cookies_browser": self.cookies_browser,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> "Settings":
        return cls(
            library_dir=data.get("library_dir") or None,
            sheets_dir=data.get("sheets_dir") or None,
            cookies_file=data.get("cookies_file") or None,
            cookies_browser=data.get("cookies_browser") or None,
            path=path,
        )

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """Read settings from disk, falling back to defaults on any problem."""
        path = Path(path) if path else CONFIG_PATH
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    settings = cls.from_dict(json.load(handle), path=path)
                settings.ensure_directories()
                return settings
            except (OSError, ValueError):
                # A corrupt config should not stop the app from starting.
                pass
        settings = cls(path=path)
        settings.ensure_directories()
        return settings

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=4)
        self.ensure_directories()

    def ensure_directories(self) -> None:
        self.library_dir.mkdir(parents=True, exist_ok=True)

    # -- helpers -----------------------------------------------------------

    def is_inside_library(self, path: Path) -> bool:
        """True if ``path`` resolves to somewhere under the library root.

        Used to gate audio streaming so the server cannot be tricked into
        serving arbitrary files from disk.
        """
        try:
            path.resolve().relative_to(self.library_dir.resolve())
        except (ValueError, OSError):
            return False
        return True
