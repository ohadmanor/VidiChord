"""VidiChord entry point.

Starts the API and static host, then opens a browser at it.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import webbrowser


def _configure_console() -> None:
    """Make stdout/stderr handle Hebrew titles on a cp1252 console."""
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", "") or ""
        if encoding.lower() not in ("utf-8", "utf8"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def _configure_hf() -> None:
    """Quiet huggingface_hub's symlink warning before it imports.

    Model downloads land in the HF cache, which symlinks blobs into snapshot
    directories. Windows only allows that under Developer Mode or as admin, so
    the cache falls back to copying. That costs disk but works, and the warning
    only alarms users. Set the variable yourself to keep the warning.
    """
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _configure_path() -> None:
    """Put the bundled ffmpeg on PATH, and warn if Node.js is missing.

    yt-dlp needs a JavaScript runtime to solve YouTube's player challenges.
    """
    from vidichord.config import FFMPEG_DIR

    if FFMPEG_DIR.is_dir():
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(FFMPEG_DIR)

    if shutil.which("node") is None:
        print(
            "Note: Node.js was not found on PATH. Some YouTube downloads may "
            "fail without a JavaScript runtime.",
            file=sys.stderr,
        )


def _open_browser(url: str) -> None:
    def launch() -> None:
        import time

        time.sleep(1.0)
        webbrowser.open(url)

    threading.Thread(target=launch, daemon=True).start()


def main() -> None:
    _configure_console()
    _configure_hf()

    # The package lives beside this file when running from source.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    _configure_path()

    import uvicorn

    from vidichord.config import PORT

    url = f"http://127.0.0.1:{PORT}"
    print(f"VidiChord {_version()} listening on {url}")

    if os.environ.get("VIDICHORD_NO_BROWSER") != "1":
        _open_browser(url)

    uvicorn.run(
        "vidichord.server:app",
        host="127.0.0.1",
        port=PORT,
        log_level=os.environ.get("VIDICHORD_LOG_LEVEL", "warning"),
    )


def _version() -> str:
    from vidichord import __version__

    return __version__


if __name__ == "__main__":
    # Chord extraction fans madmom out over worker processes. In the frozen
    # build a spawned worker re-runs this executable, and without this call it
    # would start another server instead of doing its work.
    import multiprocessing

    multiprocessing.freeze_support()
    main()
