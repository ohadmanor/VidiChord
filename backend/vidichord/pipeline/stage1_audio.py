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
import sys
import time
import zipfile
from pathlib import Path

import requests

from ..config import DATA_DIR, FFMPEG_DIR, Settings
from ..models import SourceDoc
from ..project import SongProject, make_song_id
from . import StageContext

_FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

#: A cookie jar with this name, sitting beside the executable, is picked up
#: without any configuration. It is the least fiddly way to hand the app a
#: signed-in session.
COOKIE_FILENAME = "cookies.txt"

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


def cookie_options(settings: Settings | None = None) -> dict:
    """yt-dlp options that make requests as a signed-in user.

    YouTube meets anonymous requests with "Sign in to confirm you're not a
    bot", and no choice of player client gets around it - the audio formats
    themselves are withheld. Sending the user's own cookies is yt-dlp's
    documented answer, so the app looks for them in three places: the setting,
    the environment, and a ``cookies.txt`` beside the executable.
    """
    explicit = getattr(settings, "cookies_file", None)
    candidates = [
        Path(explicit) if explicit else None,
        Path(os.environ["VIDICHORD_COOKIES"])
        if os.environ.get("VIDICHORD_COOKIES")
        else None,
        DATA_DIR / COOKIE_FILENAME,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return {"cookiefile": str(candidate)}

    browser = (
        getattr(settings, "cookies_browser", "")
        or os.environ.get("VIDICHORD_COOKIES_BROWSER", "")
    ).strip()
    if browser:
        # yt-dlp wants (browser, profile, keyring, container).
        name, _, profile = browser.partition(":")
        return {"cookiesfrombrowser": (name.strip().lower(), profile.strip() or None, None, None)}

    return {}


#: Engines yt-dlp can run YouTube's player JavaScript in, best first.
#:
#: Streaming URLs carry an "n" parameter that has to be descrambled by running
#: the player's own code, and YouTube throttles or refuses requests that get it
#: wrong. yt-dlp used to do this in a Python interpreter of its own; since
#: 2026.07 it shells out to a real engine and enables *only* Deno by default,
#: so a machine with Node - which the README has always recommended, and which
#: is far more commonly installed - silently lost the ability to answer the
#: challenge. Naming every supported engine lets yt-dlp use whichever one is
#: actually on PATH; the rest are reported unavailable and ignored.
_JS_RUNTIMES = ("node", "deno", "bun", "quickjs")


def js_runtime_options() -> dict:
    """yt-dlp options letting it use any JavaScript engine present."""
    return {"js_runtimes": {name: {} for name in _JS_RUNTIMES}}


#: yt-dlp's way of saying it found no engine to run the player's code in.
_JS_WARNING_SIGNS = (
    "javascript runtime",
    "signature solving failed",
    "challenge solving failed",
)

#: What to say instead. yt-dlp cannot bundle an engine and neither can a
#: PyInstaller build - it is a separate program - so the remedy is to put one
#: beside the app, where the frozen build already looks for cookies.
NO_JS_ENGINE = (
    "YouTube needs a JavaScript engine to unscramble its download links, and "
    "none was found. Install Node.js, or put node.exe beside VidiChord.exe. "
    "Songs added from a file need none of this."
)


#: How many times to ask YouTube for the media before believing the refusal.
#:
#: Google's media servers reject a large share of requests with a bare 403 for
#: no lasting reason - measured at roughly half of all attempts on one video,
#: with the very next attempt on a freshly extracted URL succeeding. yt-dlp
#: does not retry these itself: a 403 is a client error, so it stops. Retrying
#: the whole extraction is the only thing that clears it, and it is quick - a
#: refused attempt costs under three seconds, so enough of them to make a run
#: of bad luck unlikely still fails fast when the refusal is the real kind.
_DOWNLOAD_ATTEMPTS = 8

#: Seconds to wait between those attempts.
_RETRY_PAUSE = 1.5

#: Signatures of a refusal that a fresh attempt is likely to get past. A 403
#: from the media host is transient; the sign-in and rate-limit refusals in
#: ``_BLOCKED_SIGNS`` are not, and must not be retried into a long stall.
_TRANSIENT_SIGNS = ("403", "forbidden", "unable to download video data")


def _is_transient(error: Exception) -> bool:
    """True if ``error`` looks like the 403 that simply retrying gets past."""
    lowered = str(error).lower()
    if any(sign in lowered for sign in _BLOCKED_SIGNS):
        return False
    return any(sign in lowered for sign in _TRANSIENT_SIGNS)


#: First line of every "YouTube would not identify us" explanation. Short and
#: unmistakable, because it is the one thing the user has to act on - the
#: paragraphs after it are detail.
HEADLINE = "You need to log in to YouTube."

#: Signatures of a refusal that more retries will not fix.
_BLOCKED_SIGNS = (
    "confirm you",  # "Sign in to confirm you're not a bot" - any apostrophe.
    "not a bot",
    "429",
    "too many requests",
    "po token",
    "requested format is not available",
    "only images are available",
)


#: Signatures of yt-dlp failing to read a browser's cookie store, rather than
#: of YouTube refusing anything. Windows browsers are the usual cause.
_COOKIE_READ_SIGNS = (
    "could not copy",
    "cookie database",
    "failed to decrypt",
    "dpapi",
    "unsupported browser",
)


def explain_failure(
    error: Exception, settings: Settings | None = None, context: str = ""
) -> str:
    """Turn a yt-dlp refusal into something a user can act on.

    ``context`` carries yt-dlp's own messages when they were captured rather
    than printed, so a refusal is recognised by them too.
    """
    raw = str(error)
    lowered = f"{raw} {context}".lower()

    if any(sign in lowered for sign in _COOKIE_READ_SIGNS):
        return (
            f"{HEADLINE}\n\n"
            "VidiChord could not read the cookies out of that browser. On "
            "Windows this is normal for Chrome and Edge: they encrypt their "
            "cookie store, and Chrome also locks it while it is running.\n\n"
            f"Export a '{COOKIE_FILENAME}' file instead (any 'Get cookies.txt' "
            "browser extension does it while you are signed in to YouTube) and "
            "save it next to the app, or point \"cookies_browser\" at "
            "\"firefox\".\n\n"
            f"Original error: {raw}"
        )

    if not any(sign in lowered for sign in _BLOCKED_SIGNS):
        return raw

    if cookie_options(settings):
        return (
            f"{HEADLINE}\n\n"
            "VidiChord sent the cookies it is set up to use and YouTube still "
            "refused, which almost always means they have expired. Sign in to "
            f"YouTube again and export a fresh '{COOKIE_FILENAME}'.\n\n"
            "YouTube also caps how much one network may fetch. If you have "
            "tried several times in a row, or share an office connection, a "
            "wait may be needed on top of signing in."
        )
    return (
        f"{HEADLINE}\n\n"
        "YouTube will not serve this video's audio to a request it cannot "
        "identify. It asks the caller to prove it is not a bot and withholds "
        "every audio format until it does, and VidiChord cannot answer that on "
        "its own - it needs your session.\n\n"
        f"Save a '{COOKIE_FILENAME}' file next to the app: any 'Get "
        "cookies.txt' browser extension exports one while you are signed in to "
        "YouTube. Or set \"cookies_browser\" to \"firefox\" in Settings. Then "
        "try the song again.\n\n"
        "YouTube also caps how much one network may fetch, so if you have "
        "tried several times in a row, a wait may be needed as well - signing "
        "in alone will not clear that part.\n\n"
        "\"Add from file\" needs none of this, if you already have the audio."
    )


class _QuietLogger:
    """Keeps yt-dlp's own output off the console.

    Its error lines are what users were reading and reporting: several lines of
    advice about command-line flags for a tool they never ran. This stage
    translates the failure itself, so the raw text is kept for context and the
    explanation is printed in its place.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        # Warnings carry the "requires a PO Token" hints, which say why no
        # audio format came back. Worth keeping as context, not worth showing.
        self.errors.append(str(message).strip())

    def error(self, message: str) -> None:
        self.errors.append(str(message).strip())


def probe(url: str, settings: Settings | None = None) -> dict:
    """Fetch video metadata without downloading the media."""
    import yt_dlp

    logger = _QuietLogger()
    options = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": logger,
    }
    options.update(js_runtime_options())
    options.update(cookie_options(settings))
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as error:
        message = explain_failure(error, settings, " ".join(logger.errors))
        # The exe runs with a console, and that is where a user watching a run
        # looks first. Say the same thing there as in the app.
        print(f"\n{message}\n", file=sys.stderr)
        raise RuntimeError(message) from error


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
        # yt-dlp's own warnings advise command-line flags for a tool the user
        # never ran - the very thing _QuietLogger exists to keep off the
        # screen. The JavaScript-engine ones are the reachable case, and they
        # have a VidiChord answer, so say that instead.
        text = str(message)
        if any(sign in text.lower() for sign in _JS_WARNING_SIGNS):
            self._report(NO_JS_ENGINE, None)
            return
        self._report(f"Warning: {text}", None)

    def error(self, message: str) -> None:
        self._report(f"Error: {message}", None)


def download(
    url: str, destination: Path, report=None, settings: Settings | None = None
) -> None:
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
    options.update(js_runtime_options())
    options.update(cookie_options(settings))
    options["logger"] = _ProgressLogger(report) if report else _QuietLogger()

    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                if ydl.download([url]) != 0:
                    raise RuntimeError("Download or conversion failed")
        except Exception as error:
            # Each attempt re-extracts, because the refusal is attached to the
            # streaming URL rather than to the video: reusing it would fail the
            # same way every time.
            if attempt < _DOWNLOAD_ATTEMPTS and _is_transient(error):
                if report:
                    report(
                        f"YouTube refused the download, retrying "
                        f"({attempt}/{_DOWNLOAD_ATTEMPTS - 1})...",
                        None,
                    )
                time.sleep(_RETRY_PAUSE)
                continue
            message = explain_failure(error, settings)
            print(f"\n{message}\n", file=sys.stderr)
            raise RuntimeError(message) from error
        else:
            break

    if not destination.is_file():
        raise RuntimeError(f"Expected {destination.name} to exist after download")


def prepare_from_youtube(url: str, settings: Settings, report=None) -> SongProject:
    """Create a project for a YouTube URL and fetch its audio into it.

    Runs before the pipeline proper, because the project folder is named after
    metadata that only the probe can supply.
    """
    if report:
        report("Reading video details...", 0.0)
    info = probe(url, settings)
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
        download(source.url, project.audio_path, context.report, context.settings)

    context.report("Audio ready.", 100.0)
