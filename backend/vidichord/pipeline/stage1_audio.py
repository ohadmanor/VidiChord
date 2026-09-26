"""Stage 1 - acquire the audio.

Downloads a YouTube video's audio stream and transcodes it to 16-bit 44.1 kHz
mono-compatible WAV, or copies in a local file. FFmpeg is fetched on first use
so the app has no external install step.

Metadata is probed before the download so the song's title and artist are known
in time to name its project folder.

Its progress messages are matched, to be reworded for the app, in
frontend/src/app/components/run-progress/run-progress.model.ts - keep the
two in step.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import NoReturn

import requests

from ..config import DATA_DIR, FFMPEG_DIR, FROZEN, Settings
from ..models import SourceDoc
from ..project import SongProject, make_song_id
from . import StageContext, ytdlp_update

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
#: This used to be eight, on the understanding that a bare 403 from Google's
#: media hosts was noise that a fresh extraction cleared. It was not. Those
#: refusals were YouTube withdrawing service from the player client yt-dlp was
#: impersonating, and once that finished the 403 became every request, so the
#: eight attempts only stalled a download that could never succeed. Retries
#: are kept for what they are actually good for - a connection that drops
#: mid-transfer - and a few are plenty for that.
_DOWNLOAD_ATTEMPTS = 3

#: Seconds to wait between those attempts.
_RETRY_PAUSE = 1.5

#: Signatures of a failure a fresh attempt may get past: the network faltering,
#: or a server having a bad moment. A 403 is deliberately absent - see
#: ``STALE_YTDLP`` - and so are the refusals in ``_BLOCKED_SIGNS``, which need
#: the user to act rather than to wait.
_TRANSIENT_SIGNS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection broken",
    "incomplete read",
    "temporary failure",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
)

#: A refusal of the media itself, as yt-dlp words it. Not the bare number: a
#: song folder is named after the song, and an error quoting its path - a
#: .part file held open, ffmpeg's last line - would make "Forbidden Fruit" a
#: refusal.
_MEDIA_REFUSAL_SIGNS = ("http error 403",)


def _is_transient(error: Exception) -> bool:
    """True if ``error`` looks like something a fresh attempt may get past."""
    lowered = str(error).lower()
    if any(sign in lowered for sign in _BLOCKED_SIGNS):
        return False
    return any(sign in lowered for sign in _TRANSIENT_SIGNS)


#: First line of every "YouTube would not identify us" explanation. Short and
#: unmistakable, because it is the one thing the user has to act on - the
#: paragraphs after it are detail.
HEADLINE = "You need to log in to YouTube."

#: What a 403 on the media itself almost always means.
#:
#: yt-dlp fetches audio by impersonating one of YouTube's own player clients.
#: YouTube retires those, and a retired one still answers for the video's
#: details and still hands out streaming URLs - the refusal arrives only when
#: the download starts. So the song looks fine right up to the moment it
#: fails, no retry clears it, and no cookie helps. Only a yt-dlp new enough to
#: ask as a client YouTube still serves - which :func:`download` fetches on
#: the spot (see :mod:`ytdlp_update`), so this text is followed by how that
#: went rather than by a command for the user to run.
STALE_YTDLP = (
    "YouTube would not send this song's audio.\n\n"
    "This usually means yt-dlp, which VidiChord downloads with, has been "
    "overtaken by a change at YouTube's end: it asks for the audio as a "
    "player YouTube no longer serves. The refusal usually arrives only once "
    "the download starts, which is why a video's details can load normally."
)

#: The paragraph after it, one per outcome of the update.
UPDATED_STILL_REFUSED = (
    "VidiChord updated yt-dlp to {version} and YouTube still refused, so this "
    "is a change yt-dlp has not caught up with yet. That usually takes it a "
    "few days; try the song again then."
)
ALREADY_NEWEST = (
    "VidiChord's yt-dlp is already the newest release ({version}), so this is "
    "a change yt-dlp has not caught up with yet. That usually takes it a few "
    "days; try the song again then."
)
UPDATE_FAILED = "VidiChord tried to update yt-dlp and could not: {reason}\n\n{manual}"

#: What the user can do instead, when the app could not update - which
#: differs, because only a source checkout has a pip to hand them.
MANUAL_FROM_SOURCE = (
    "Running from source, this does the same by hand:\n\n"
    f"    {ytdlp_update.MANUAL_COMMAND}\n\n"
    "and then try the song again."
)
MANUAL_PACKAGED = (
    "It tries again every time VidiChord starts, and whenever a download is "
    "refused like this. A newer VidiChord build also brings a newer yt-dlp."
)
#: When the user turned updating off, nothing was tried - and a yt-dlp
#: installed by hand then needs a restart, because only an update the app
#: makes itself swaps the running copy.
UPDATES_OFF = (
    "VidiChord did not update yt-dlp, because updating is turned off "
    f"({ytdlp_update.DISABLE_ENV} is set). Unset it and restart VidiChord to "
    "let it update."
)
UPDATES_OFF_FROM_SOURCE = (
    f"{UPDATES_OFF} Or update by hand:\n\n"
    f"    {ytdlp_update.MANUAL_COMMAND}\n\n"
    "and then restart VidiChord."
)
FROM_FILE = "\"Add from file\" needs none of this, if you already have the audio."


def _manual_update() -> str:
    return MANUAL_PACKAGED if FROZEN else MANUAL_FROM_SOURCE

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


def _is_stale_refusal(error: Exception, context: str = "") -> bool:
    """True if ``error`` is the refusal a newer yt-dlp fixes.

    That is a 403 on the media with none of the wording that points at the
    user instead - a sign-in demand, a rate limit, a browser whose cookies
    could not be read.
    """
    lowered = f"{error} {context}".lower()
    if any(sign in lowered for sign in _COOKIE_READ_SIGNS + _BLOCKED_SIGNS):
        return False
    return any(sign in lowered for sign in _MEDIA_REFUSAL_SIGNS)


def explain_failure(
    error: Exception,
    settings: Settings | None = None,
    context: str = "",
    outcome: str | None = None,
) -> str:
    """Turn a yt-dlp refusal into something a user can act on.

    ``context`` carries yt-dlp's own messages when they were captured rather
    than printed, so a refusal is recognised by them too. ``outcome`` is how
    the attempt to update yt-dlp went, for the refusal that calls for one;
    without it the user is handed the update to make.
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
        # A 403 on the media, with none of the sign-in or rate-limit wording
        # around it, is the one refusal a cookie does nothing for - and the
        # rawest-looking of them all, so it is the one most worth translating.
        if _is_stale_refusal(error, context):
            if outcome is None:
                outcome = _manual_update()
            original = f"Original error: {raw}"
            return "\n\n".join([STALE_YTDLP, outcome, FROM_FILE, original])
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
    logger = _QuietLogger()
    options = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": logger,
    }
    options.update(js_runtime_options())
    options.update(cookie_options(settings))

    def attempt() -> dict:
        # A retry is judged by its own messages, not the first attempt's.
        logger.errors.clear()
        with ytdlp_update.load().YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)

    return _ask_youtube(attempt, None, settings, lambda: " ".join(logger.errors))


def _fail(message: str, error: Exception) -> NoReturn:
    """Raise ``message`` as the stage's failure, and print it too.

    The exe runs with a console, and that is where a user watching a run
    looks first. Say the same thing there as in the app.
    """
    print(f"\n{message}\n", file=sys.stderr)
    raise RuntimeError(message) from error


def _ask_youtube(attempt, report, settings: Settings | None, context=lambda: ""):
    """Run ``attempt``, one request to YouTube, updating yt-dlp if that is refused.

    ``attempt`` returns what the request produced and raises yt-dlp's own
    error otherwise. A refusal of the kind a newer yt-dlp fixes is answered
    by installing one and running ``attempt`` once more; every other failure,
    and that one when nothing more can be done, is raised as a RuntimeError
    carrying the explanation. ``context`` supplies yt-dlp's captured messages,
    for recognising a refusal the exception itself is vague about.
    """
    # Which copy the refusal, if any, was of. Another download may replace it
    # while this one runs, and then the retry is on that copy, not on a pip
    # run of our own - see ``upgrade``.
    ran_on = ytdlp_update.installed()
    try:
        return attempt()
    except Exception as error:
        if not _is_stale_refusal(error, context()):
            _fail(explain_failure(error, settings, context()), error)

        # The refusal a newer yt-dlp fixes. Fetch one and ask again, and say
        # how that went either way: the user used to be handed a pip command
        # at this point, and the point of running it here is that they are
        # not any more. The packaged app does the same, into a folder beside
        # the exe (see ytdlp_update).
        if ytdlp_update.disabled():
            outcome = UPDATES_OFF if FROZEN else UPDATES_OFF_FROM_SOURCE
            _fail(explain_failure(error, settings, context(), outcome), error)
        try:
            version = ytdlp_update.upgrade(report, stale=ran_on)
        except RuntimeError as reason:
            outcome = UPDATE_FAILED.format(reason=reason, manual=_manual_update())
            _fail(explain_failure(error, settings, context(), outcome), error)
        if version is None:
            outcome = ALREADY_NEWEST.format(version=ran_on)
            _fail(explain_failure(error, settings, context(), outcome), error)

        if report:
            report(f"Updated yt-dlp to {version}, asking YouTube again...", None)
        try:
            return attempt()
        except Exception as again:
            outcome = None
            if _is_stale_refusal(again, context()):
                outcome = UPDATED_STILL_REFUSED.format(version=version)
            _fail(explain_failure(again, settings, context(), outcome), again)


class _ProgressLogger:
    """Adapts yt-dlp's logger interface onto the stage reporter."""

    _PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")

    def __init__(self, report) -> None:
        self._report = report

    def debug(self, message: str) -> None:
        # The download fills 90% of the stage and the conversion the rest, so
        # the bar keeps moving while ffmpeg works instead of waiting at full.
        if message.startswith("[download]"):
            found = self._PERCENT.search(message)
            self._report(message.strip(), float(found.group(1)) * 0.9 if found else None)
        elif message.startswith("[ExtractAudio]"):
            self._report("Converting to WAV...", 92.0)

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

    _ask_youtube(lambda: _fetch(url, options, report), report, settings)

    if not destination.is_file():
        raise RuntimeError(f"Expected {destination.name} to exist after download")


def _fetch(url: str, options: dict, report=None) -> None:
    """One download, given a few more attempts if the connection falters.

    Raises yt-dlp's own error once it gives up.
    """
    yt_dlp = ytdlp_update.load()

    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                if ydl.download([url]) != 0:
                    raise RuntimeError("Download or conversion failed")
            return
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
            raise


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
        # Every few weeks YouTube changes something that only a newer yt-dlp
        # can follow. Asking PyPI once a day, here where the progress is
        # visible, is cheaper than finding out from the refusal.
        ytdlp_update.ensure_current(context.report)
        download(source.url, project.audio_path, context.report, context.settings)

    context.report("Audio ready.", 100.0)
