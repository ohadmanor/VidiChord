"""Stage 1 - acquire the audio.

Downloads a YouTube video's audio stream and transcodes it to 16-bit 44.1 kHz
mono-compatible WAV, or copies in a local file. FFmpeg is fetched on first use
so the app has no external install step.

Metadata is probed before the download so the song's title and artist are known
in time to name its project folder.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path

import requests

from ..config import FFMPEG_DIR, Settings
from ..models import SourceDoc
from ..project import SongProject, make_song_id
from . import StageContext

_FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

_ILLEGAL = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")

#: YouTube auto-generated channels are named "<Artist> - Topic".
_TOPIC_SUFFIX = " - Topic"


def ffmpeg_paths() -> tuple[Path, Path]:
    return FFMPEG_DIR / "ffmpeg.exe", FFMPEG_DIR / "ffprobe.exe"


def ensure_ffmpeg(report=None) -> Path:
    """Return the directory holding ffmpeg, downloading it if necessary."""
    ffmpeg, ffprobe = ffmpeg_paths()
    if ffmpeg.is_file() and ffprobe.is_file():
        return FFMPEG_DIR

    # A system-wide install is just as good.
    system_ffmpeg = shutil.which("ffmpeg")
    system_ffprobe = shutil.which("ffprobe")
    if system_ffmpeg and system_ffprobe:
        return Path(system_ffmpeg).parent

    if report:
        report("Downloading FFmpeg (first run only)...", 0.0)

    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    archive = FFMPEG_DIR / "ffmpeg.zip"
    try:
        with requests.get(_FFMPEG_URL, stream=True, timeout=120) as response:
            response.raise_for_status()
            with archive.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    if chunk:
                        handle.write(chunk)

        if report:
            report("Extracting FFmpeg...", None)

        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                name = os.path.basename(entry.filename)
                if name in ("ffmpeg.exe", "ffprobe.exe"):
                    with bundle.open(entry) as source, (FFMPEG_DIR / name).open("wb") as target:
                        shutil.copyfileobj(source, target)
    finally:
        archive.unlink(missing_ok=True)

    ffmpeg, ffprobe = ffmpeg_paths()
    if not (ffmpeg.is_file() and ffprobe.is_file()):
        raise RuntimeError("Could not obtain ffmpeg.exe and ffprobe.exe")
    return FFMPEG_DIR


def sanitize(name: str) -> str:
    cleaned = _ILLEGAL.sub(" - ", name)
    return _WHITESPACE.sub(" ", cleaned).strip(". ")


def split_names(info: dict) -> tuple[str, str]:
    """Best guess at ``(artist, title)`` from yt-dlp metadata."""
    artist = (info.get("artist") or "").strip()
    track = (info.get("track") or "").strip()
    uploader = (info.get("uploader") or "").strip()
    title = (info.get("title") or "").strip()

    if artist and track:
        return artist, track
    if track and uploader:
        if uploader.endswith(_TOPIC_SUFFIX):
            uploader = uploader[: -len(_TOPIC_SUFFIX)]
        return uploader, track
    # Fall back to the "Artist - Title" convention in the video title.
    if " - " in title:
        left, _, right = title.partition(" - ")
        return left.strip(), right.strip()
    return uploader or "Unknown", title or "Unknown"


def probe(url: str) -> dict:
    """Fetch video metadata without downloading the media."""
    import yt_dlp

    options = {"noplaylist": True, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


class _ProgressLogger:
    """Adapts yt-dlp's logger interface onto the stage reporter."""

    _PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")

    def __init__(self, report) -> None:
        self._report = report

    def debug(self, message: str) -> None:
        if message.startswith("[download]"):
            found = self._PERCENT.search(message)
            self._report(message.strip(), float(found.group(1)) if found else None)
        elif message.startswith("[ExtractAudio]"):
            self._report("Converting to WAV...", None)

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self._report(f"Warning: {message}", None)

    def error(self, message: str) -> None:
        self._report(f"Error: {message}", None)


def download(url: str, destination: Path, report=None) -> None:
    """Download the best audio stream and write it to ``destination`` as WAV."""
    import yt_dlp

    ffmpeg_dir = ensure_ffmpeg(report)
    # yt-dlp appends the container extension to outtmpl, so hand it a stem.
    stem = destination.with_suffix("")

    options = {
        "ffmpeg_location": str(ffmpeg_dir),
        "format": "bestaudio/best",
        "outtmpl": f"{stem}.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "postprocessor_args": {"ffmpeg": ["-acodec", "pcm_s16le", "-ar", "44100"]},
    }
    if report:
        options["logger"] = _ProgressLogger(report)

    with yt_dlp.YoutubeDL(options) as ydl:
        if ydl.download([url]) != 0:
            raise RuntimeError("Download or conversion failed")

    if not destination.is_file():
        raise RuntimeError(f"Expected {destination.name} to exist after download")


def prepare_from_youtube(url: str, settings: Settings, report=None) -> SongProject:
    """Create a project for a YouTube URL and fetch its audio into it.

    Runs before the pipeline proper, because the project folder is named after
    metadata that only the probe can supply.
    """
    if report:
        report("Reading video details...", 0.0)
    info = probe(url)
    artist, title = split_names(info)

    project = SongProject.create(
        settings.library_dir, make_song_id(sanitize(artist), sanitize(title), seed=url)
    )
    project.write(
        SourceDoc(
            kind="youtube",
            url=url,
            video_id=info.get("id", ""),
            title=title,
            artist=artist,
            uploader=info.get("uploader", ""),
            duration=float(info.get("duration") or 0.0),
        )
    )

    manifest = project.read_manifest()
    manifest.title, manifest.artist = title, artist
    project.write_manifest(manifest)
    return project


def prepare_from_file(path: Path, settings: Settings, report=None) -> SongProject:
    """Create a project from a local audio file."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    stem = path.stem
    if " - " in stem:
        artist, _, title = stem.partition(" - ")
    else:
        artist, title = "Unknown", stem

    project = SongProject.create(
        settings.library_dir,
        make_song_id(sanitize(artist.strip()), sanitize(title.strip()), seed=str(path)),
    )
    if report:
        report(f"Copying {path.name}...", 0.0)
    shutil.copyfile(path, project.audio_path)

    project.write(
        SourceDoc(kind="local", url=str(path), title=title.strip(), artist=artist.strip())
    )
    manifest = project.read_manifest()
    manifest.title, manifest.artist = title.strip(), artist.strip()
    project.write_manifest(manifest)
    return project


def run(context: StageContext) -> None:
    """Ensure the project has its audio, downloading it if it does not."""
    project = context.project
    if project.audio_path.is_file() and not context.param("force", False):
        context.report("Audio already present.", 100.0)
        return

    source = project.read_optional(SourceDoc)
    if source is None or not source.url:
        raise RuntimeError("No audio source recorded for this song")

    if source.kind == "local":
        context.report(f"Copying {Path(source.url).name}...", 0.0)
        shutil.copyfile(source.url, project.audio_path)
    else:
        download(source.url, project.audio_path, context.report)

    context.report("Audio ready.", 100.0)
