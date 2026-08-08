"""Application settings and filesystem locations.

Replaces the old ``config_manager`` module. Two directories are configurable:

``library_dir``
    Root of the song library. Each song gets one folder underneath it holding
    its audio plus one JSON artifact per pipeline stage (see :mod:`project`).

``sheets_dir``
    Where songbook exports are written. Usually points at an external songbook
    checkout, which is why it is kept separate from the library.

Secrets are never stored in ``config.json`` - they come from the environment.
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

CONFIG_PATH = BACKEND_DIR / "config.json"

# Bundled third-party binaries. PyInstaller unpacks datas into ``sys._MEIPASS``.
_BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", BACKEND_DIR))

FFMPEG_DIR = _BUNDLE_ROOT / "ffmpeg"
ESSENTIA_BIN = (
    PACKAGE_DIR / "vendor" / "essentia" / "bin" / "streaming_extractor_music.exe"
)

# Where the compiled Angular app lives, checked in bundle-first order.
_FRONTEND_CANDIDATES = (
    _BUNDLE_ROOT / "frontend" / "dist" / "frontend" / "browser",
    REPO_DIR / "frontend" / "dist" / "frontend" / "browser",
)

DEFAULT_LIBRARY_DIR = REPO_DIR / "VidiChord_Files"

#: Environment variable holding the LLM key, if the optional refiner is used.
LLM_API_KEY_ENV = "VIDICHORD_LLM_API_KEY"

PORT = 8001


def frontend_dir() -> Path | None:
    """Return the directory holding the built Angular app, or None if unbuilt."""
    for candidate in _FRONTEND_CANDIDATES:
        if (candidate / "index.html").is_file():
            return candidate
    return None


class Settings:
    """User-editable settings, persisted to ``backend/config.json``."""

    def __init__(self, library_dir: str | os.PathLike[str] | None = None,
                 sheets_dir: str | os.PathLike[str] | None = None) -> None:
        self.library_dir = Path(library_dir) if library_dir else DEFAULT_LIBRARY_DIR
        # Empty means "not configured"; export refuses to run until it is set.
        self.sheets_dir = Path(sheets_dir) if sheets_dir else None

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, str]:
        return {
            "library_dir": str(self.library_dir),
            "sheets_dir": str(self.sheets_dir) if self.sheets_dir else "",
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        return cls(
            library_dir=data.get("library_dir") or None,
            sheets_dir=data.get("sheets_dir") or None,
        )

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls) -> "Settings":
        """Read settings from disk, falling back to defaults on any problem."""
        if CONFIG_PATH.is_file():
            try:
                with CONFIG_PATH.open("r", encoding="utf-8") as handle:
                    settings = cls.from_dict(json.load(handle))
                settings.ensure_directories()
                return settings
            except (OSError, ValueError):
                # A corrupt config should not stop the app from starting.
                pass
        settings = cls()
        settings.ensure_directories()
        return settings

    def save(self) -> None:
        with CONFIG_PATH.open("w", encoding="utf-8") as handle:
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


def llm_api_key() -> str | None:
    """Return the optional LLM API key from the environment."""
    return os.environ.get(LLM_API_KEY_ENV) or None
