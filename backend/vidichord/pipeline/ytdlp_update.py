"""Keeping yt-dlp current, because YouTube does not keep still.

yt-dlp fetches audio by impersonating one of YouTube's own player clients,
and YouTube retires those every few weeks. A retired client still answers for
a video's details and still hands out streaming URLs; the refusal - a bare
403 - arrives only once the download starts. No retry and no cookie clears
it. A newer yt-dlp does, and one nearly always exists by the time the refusal
is seen, because yt-dlp releases within days of each such change.

Telling the user to run pip worked, but only until the next change, so the
app keeps its own copy current instead:

- :func:`check_in_background` runs :func:`ensure_current` as the app starts,
  before anything has imported yt-dlp, so an upgrade there is simply what the
  first song imports. :func:`ensure_current` runs again as a download starts,
  at most once a day, for an app left running for days.
- :func:`upgrade` runs when YouTube refuses a request that way anyway. The
  caller asks again on whatever it installed.

Running from source, an upgrade is pip, on the interpreter the app runs in.
The packaged exe has no pip and its own copy frozen inside it, so there an
upgrade downloads the release's wheels from PyPI, checks them against the
digests PyPI publishes, and unpacks them into a folder beside the exe; a
finder placed ahead of PyInstaller's then serves yt-dlp from that folder
instead of from the bundle. The frozen copy stays as the fallback: a download
that will not import is set aside, and the bundle's is used.

Either way, an upgrade that lands after yt-dlp was imported unloads the old
package, so the next :func:`load` gets the new one without a restart.

``VIDICHORD_NO_YTDLP_UPDATE=1`` turns all of this off, for a machine with no
internet or someone who wants to keep the yt-dlp they have.

Several VidiChords may run at once - a double-click on the exe does it - and
the upgrade lock only holds within one. So each unpacks into a staging folder
of its own, a copy counts only once a marker written last says it is whole,
and a copy another one finished first is accepted as it stands.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import importlib.metadata
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

import requests

from ..config import DATA_DIR, FROZEN

#: What pip is asked for. The ``default`` extra is where yt-dlp pins the
#: yt-dlp-ejs release it expects - the package that supplies the script Node
#: runs to answer YouTube's signature challenge. yt-dlp checks that script's
#: version and hash against its own manifest and silently discards a mismatch,
#: which looks exactly like having no JavaScript engine at all; installing the
#: pair through the extra keeps them matched by construction. The packaged
#: app reads the same pin out of the release's metadata.
PACKAGE = "yt-dlp[default]"

#: Where releases are looked up. PyPI rather than GitHub, because PyPI is
#: where pip gets them from: a release tagged on GitHub and not yet uploaded
#: would only produce an upgrade that installs nothing.
_PYPI_URL = "https://pypi.org/pypi/{project}/json"
_PYPI_RELEASE_URL = "https://pypi.org/pypi/{project}/{version}/json"

#: Seconds to wait on PyPI for a release's details. The daily check sits on
#: the path of a download, which must not stall for long when offline.
_PYPI_TIMEOUT = 5.0

#: Seconds to allow for downloading a wheel - a few megabytes.
_WHEEL_TIMEOUT = 120.0

#: How long an answer from PyPI is trusted before it is asked again. yt-dlp
#: releases a few times a month; a day's lag is nothing next to that.
CHECK_INTERVAL = 24 * 60 * 60.0

#: Seconds to allow pip. A wheel of a few megabytes, on a slow connection.
_PIP_TIMEOUT = 300.0

#: What a user running from source is told to do when the app could not.
MANUAL_COMMAND = f'backend\\.venv\\Scripts\\pip install -U "{PACKAGE}"'

#: Turns updating off.
DISABLE_ENV = "VIDICHORD_NO_YTDLP_UPDATE"

#: Where the packaged app keeps the copies it downloads: one folder per
#: release, beside the exe, where settings and the library already live.
#: Named for the app, like VidiChord_Files: a folder called just "yt-dlp" is
#: one a user may well have there already, with things of their own in it.
UPDATES_DIR = DATA_DIR / "VidiChord_yt-dlp"

#: Suffixes of folders in ``UPDATES_DIR`` that are not a usable copy: one
#: still being unpacked (followed by a random part, one per process), and one
#: that was unpacked and would not import.
_PARTIAL, _BROKEN = ".partial", ".broken"

#: Written into a copy last, once both wheels are unpacked. A copy without it
#: is unfinished - an unpack cut short - and is never loaded.
_COMPLETE = ".complete"

#: How old an unfinished staging folder must be before it is cleared away.
#: Another VidiChord running at the same time may still be filling one in.
_STALE_PARTIAL = 60 * 60.0

#: How soon to ask PyPI again when it did not answer. Short, because at
#: start-up the network is often simply not up yet; not so short that a
#: network that blocks PyPI adds a wait to every song.
_RETRY_AFTER_NO_ANSWER = 15 * 60.0

#: The packages an upgrade replaces, and unloads once it has.
_PACKAGES = ("yt_dlp", "yt_dlp_ejs")

#: Held while yt-dlp is upgraded, and while it is imported. Two downloads
#: refused in the same minute would otherwise both upgrade, and one of them
#: would be unloading modules the other was importing - which Python's import
#: machinery does not survive.
_lock = threading.RLock()

#: When PyPI was last asked, on the monotonic clock; None until it has been.
_last_check: float | None = None

#: What yt-dlp changes outside itself when it is imported, and that a second
#: import would get wrong. It wraps urllib3's percent-encoding pattern in a
#: proxy of its own; imported again, it wraps the proxy, whose attribute
#: lookup does not reach through a second layer - so every request made
#: through ``requests`` fails with "'Urllib3PercentREOverride' object has no
#: attribute 'sub'". (Its other patches, of ``requests.adapters`` and
#: ``websockets``, are plain assignments that a second import simply
#: repeats.) So the original is kept, and put back before the new copy loads.
_PATCHED = (("urllib3.util.url", "_PERCENT_RE"),)

#: The originals of ``_PATCHED``, as they were before yt-dlp first loaded.
_originals: dict[tuple[str, str], object] = {}


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() not in ("", "0")


# -- versions ----------------------------------------------------------------


def _key(version: str | None) -> tuple[int, ...]:
    """A yt-dlp version as something comparable: 2026.07.04 equals 2026.7.4.

    Releases are dates; master builds add a time as a fourth part, which
    sorts them after the release of the same day, as it should.
    """
    parts: list[int] = []
    for piece in (version or "").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def is_newer(candidate: str, current: str | None) -> bool:
    return _key(candidate) > _key(current)


def installed() -> str:
    """The version the running process has loaded, importing it if need be."""
    return load().version.__version__


def available() -> str | None:
    """The version the next import would load, found without importing it.

    From source that is what pip has on disk. In the packaged app it is the
    newest copy downloaded beside the exe, or else the one frozen into it,
    whose version the build records as package metadata.
    """
    if FROZEN:
        chosen = _chosen()
        return chosen.name if chosen is not None else _metadata_version()
    return _metadata_version()


def _metadata_version() -> str | None:
    importlib.invalidate_caches()
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return None


def _current() -> str:
    """What a request made now would run on."""
    if "yt_dlp" in sys.modules:
        return installed()
    return available() or installed()


# -- PyPI --------------------------------------------------------------------


def _pypi(project: str, version: str | None = None) -> dict:
    """A release's details from PyPI: the newest, or ``version``."""
    if version is None:
        url = _PYPI_URL.format(project=project)
    else:
        url = _PYPI_RELEASE_URL.format(project=project, version=version)
    try:
        response = requests.get(url, timeout=_PYPI_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception as error:
        raise RuntimeError(f"PyPI could not be reached ({error})") from error


def latest() -> str | None:
    """The newest release on PyPI, or None when that cannot be learned."""
    try:
        version = _pypi("yt-dlp")["info"]["version"]
    except Exception:
        return None
    return str(version) if version else None


# -- loading -----------------------------------------------------------------


def _is_ours(value: object) -> bool:
    """True for an object whose class yt-dlp defines, such as its proxies."""
    return type(value).__module__.split(".", 1)[0] in _PACKAGES


def _remember_originals() -> None:
    for module_name, attribute in _PATCHED:
        if (module_name, attribute) in _originals:
            continue
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        value = getattr(module, attribute, None)
        if value is not None and not _is_ours(value):
            _originals[(module_name, attribute)] = value


class _DownloadedCopyFinder:
    """Serves yt-dlp from a copy downloaded beside the exe.

    It sits first on ``sys.meta_path``, ahead of PyInstaller's finder, and
    answers only for the packages an upgrade replaces. A package the copy
    does not hold is left to the bundle; once a package does come from the
    copy, so does every module in it - a bundled module under a downloaded
    package would be a mixture of two releases.
    """

    def __init__(self) -> None:
        self.directory: Path | None = None

    def find_spec(self, name, path=None, target=None):
        top = name.split(".", 1)[0]
        if self.directory is None or top not in _PACKAGES:
            return None
        if not (self.directory / top).is_dir():
            return None
        search = [str(self.directory)] if "." not in name else path
        spec = importlib.machinery.PathFinder.find_spec(name, search)
        if spec is None:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return spec

    def invalidate_caches(self) -> None:
        pass


_finder = _DownloadedCopyFinder()


#: The name of a usable copy's folder: a version and nothing else. Not
#: ``_key``, which reads "2026.8.19.broken" as 2026.8.19 and would choose a
#: copy set aside for failing to load, again and again.
_VERSION_NAME = re.compile(r"\d+(\.\d+)*")


def _copies() -> list[Path]:
    """Usable copies downloaded beside the exe, newest first."""
    if not UPDATES_DIR.is_dir():
        return []
    found = [
        entry
        for entry in UPDATES_DIR.iterdir()
        if entry.is_dir()
        and _VERSION_NAME.fullmatch(entry.name)
        and (entry / _COMPLETE).is_file()
    ]
    return sorted(found, key=lambda entry: _key(entry.name), reverse=True)


def _chosen() -> Path | None:
    """The copy to load: the newest downloaded, if newer than the bundle's.

    A copy older than the bundle is what an earlier build downloaded; a newer
    build carries something better.
    """
    copies = _copies()
    if not copies:
        return None
    bundled = _metadata_version()
    return copies[0] if is_newer(copies[0].name, bundled) else None


def _activate() -> None:
    """Point the finder at the copy to load, or at nothing."""
    _finder.directory = _chosen()
    if _finder.directory is not None and _finder not in sys.meta_path:
        sys.meta_path.insert(0, _finder)


def load():
    """Import yt-dlp, and return the package.

    Every use of yt-dlp goes through here rather than ``import yt_dlp``, for
    three reasons. The import happens under the upgrade lock, so no upgrade
    can unload the package half-way through initialising it. It happens at
    the point of use, so after an upgrade the caller gets the new package -
    a copy imported once at the top of a module would be the old one for
    good. And what yt-dlp patches in other libraries is recorded first, so
    that an upgrade can undo it (see ``_PATCHED``).

    In the packaged app, a downloaded copy that fails to import is set aside
    and the next best - in the end, the bundle's - is loaded instead.
    """
    with _lock:
        fell_back = False
        while True:
            if "yt_dlp" not in sys.modules:
                _remember_originals()
                if FROZEN:
                    _activate()
            try:
                import yt_dlp

                if _finder.directory is not None and not getattr(yt_dlp, "__file__", None):
                    # A folder with no __init__.py imports without complaint,
                    # as an empty namespace package. It is not yt-dlp.
                    raise ImportError("the copy has no yt_dlp/__init__.py")
                if fell_back:
                    where = (
                        f"the copy downloaded earlier ({_finder.directory.name})"
                        if _finder.directory is not None
                        else "the one VidiChord was built with"
                    )
                    print(f"Using yt-dlp {where} instead.", file=sys.stderr)
                return yt_dlp
            except Exception as error:
                broken = _finder.directory
                if broken is None:
                    raise
                print(
                    f"The downloaded yt-dlp {broken.name} would not load ({error}); "
                    "setting it aside.",
                    file=sys.stderr,
                )
                fell_back = True
                _unload()
                _set_aside(broken)


def _unload() -> None:
    """Forget the loaded yt-dlp, so the next :func:`load` reads the new files.

    yt-dlp is pure Python and the app imports it at the point of use, so
    dropping its modules from ``sys.modules`` is nearly enough: the next
    import executes the new files. What its import did to the world outside
    it has to be undone as well, or the new copy would do it on top: the
    urllib3 pattern in ``_PATCHED`` goes back to the original, and the
    finders yt-dlp put on ``sys.meta_path`` for its plugins come off. A
    download already in flight keeps the old modules alive through its own
    references and finishes on them.
    """
    for (module_name, attribute), original in _originals.items():
        module = sys.modules.get(module_name)
        if module is not None:
            setattr(module, attribute, original)
    sys.meta_path[:] = [finder for finder in sys.meta_path if not _is_ours(finder)]
    for name in list(sys.modules):
        if name.split(".", 1)[0] in _PACKAGES:
            del sys.modules[name]
    importlib.invalidate_caches()


def _set_aside(copy: Path) -> None:
    """Keep a copy that would not import from being chosen - or fetched - again."""
    target = copy.with_name(copy.name + _BROKEN)
    shutil.rmtree(target, ignore_errors=True)
    try:
        copy.rename(target)
    except OSError:
        shutil.rmtree(copy, ignore_errors=True)


# -- checking and upgrading --------------------------------------------------


def check_in_background() -> threading.Thread:
    """Run the daily check now, as the app starts, without holding it up.

    Nothing has imported yt-dlp yet at this point, so an upgrade here needs
    no unloading: the first song simply imports the new copy. A song added
    while it is still running waits for it, on the upgrade lock.
    """
    thread = threading.Thread(target=ensure_current, name="yt-dlp update", daemon=True)
    thread.start()
    return thread


def ensure_current(report=None) -> None:
    """Upgrade if PyPI has a newer release than the one in use. Never raises.

    Asks at most once a day, and stays quiet when it cannot ask: a download
    that then fails takes the other route, :func:`upgrade`, and explains.
    """
    global _last_check
    if disabled():
        return
    # The lock only claims the day's check. Holding it through the request to
    # PyPI would hold up every other download's import for as long as PyPI
    # takes to answer; upgrade() takes it again for the part that matters.
    with _lock:
        now = time.monotonic()
        if _last_check is not None and now - _last_check < CHECK_INTERVAL:
            return
        _last_check = now

    newest = latest()
    if newest is None:
        # No answer is not the day's answer: the network may just not be up
        # yet. Ask again soon, rather than in a day.
        with _lock:
            _last_check = time.monotonic() - CHECK_INTERVAL + _RETRY_AFTER_NO_ANSWER
        return
    if not is_newer(newest, _current()):
        return
    try:
        upgrade(report)
    except RuntimeError as error:
        print(f"Could not update yt-dlp: {error}", file=sys.stderr)


def upgrade(report=None, stale: str | None = None) -> str | None:
    """Install the newest yt-dlp and load it in place of the running one.

    ``stale`` is the version the caller was refused on. If a different one is
    in use by the time the lock is taken, another refused download - or the
    daily check - got here first and this *is* the upgrade: it is returned
    without fetching anything again.

    Returns the version now in use when it differs from the one the caller
    ran on, or None when that was the newest already. Raises RuntimeError,
    with the reason, when the upgrade could not be carried out.
    """
    if disabled():
        raise RuntimeError(f"updating yt-dlp is turned off ({DISABLE_ENV} is set)")
    with _lock:
        before = _current()
        if stale is not None and _key(before) != _key(stale):
            return before
        if report:
            report(f"Updating yt-dlp ({before})...", None)
        print(f"Updating yt-dlp {before}...", file=sys.stderr)

        if FROZEN:
            _download_newest(before)
        else:
            _pip_upgrade()

        after = available()
        if after is None or _key(after) == _key(before):
            if not FROZEN:
                _explain_pip_installing_nothing(before)
            return None
        if "yt_dlp" in sys.modules:
            _unload()
            loaded = installed()
            if _key(loaded) == _key(before):
                # load() found the new copy broken and fell back.
                raise RuntimeError(f"yt-dlp {after} was downloaded but would not load")
            print(f"yt-dlp is now {loaded}.", file=sys.stderr)
            return loaded
        # Nothing has imported yt-dlp yet - the start-up check - so the new
        # copy is not loaded until the first song asks for it.
        print(f"yt-dlp {after} is installed; the next song will use it.", file=sys.stderr)
        return after


def _explain_pip_installing_nothing(before: str) -> None:
    """Raise if pip changed nothing although there is something newer.

    pip exits 0 when it cannot reach its index at all, or when its index is a
    mirror that has not picked up the release yet: it keeps what is installed
    and calls that success. Taken at its word, that would tell the user yt-dlp
    is already the newest release and to wait days for a fix that is out.
    """
    newest = latest()
    if newest is None:
        raise RuntimeError(
            "pip installed nothing, and PyPI could not be reached to see whether "
            "a newer yt-dlp is out"
        )
    if is_newer(newest, before):
        raise RuntimeError(
            f"pip did not install yt-dlp {newest}, the newest release on PyPI - "
            "check pip's network, proxy or index settings"
        )


def _pip_upgrade() -> None:
    command = [
        sys.executable, "-m", "pip", "install", "--upgrade",
        "--disable-pip-version-check", "--quiet", PACKAGE,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PIP_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"pip could not be run: {error}") from error
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        status = f"pip exited with status {result.returncode}"
        raise RuntimeError(lines[-1].strip() if lines else status)


# -- the packaged app's upgrade ----------------------------------------------

#: yt-dlp's pin on the yt-dlp-ejs release it expects, in its metadata.
_EJS_PIN = re.compile(r"^yt-dlp-ejs\s*==\s*([\w.]+)\s*;.*extra\s*==\s*['\"]default['\"]")

#: The oldest Python a release accepts, from its metadata.
_PYTHON_FLOOR = re.compile(r">=\s*(\d+)\.(\d+)")


def _download_newest(before: str) -> None:
    """Fetch the newest release into ``UPDATES_DIR``, if newer than ``before``."""
    release = _pypi("yt-dlp")
    info = release.get("info") or {}
    version = str(info.get("version") or "")
    if not is_newer(version, before):
        return

    floor = _PYTHON_FLOOR.search(info.get("requires_python") or "")
    if floor and sys.version_info[:2] < (int(floor[1]), int(floor[2])):
        raise RuntimeError(
            f"yt-dlp {version} needs Python {floor[1]}.{floor[2]}, and this build of "
            f"VidiChord has {sys.version_info[0]}.{sys.version_info[1]}"
        )
    if (UPDATES_DIR / (version + _BROKEN)).exists():
        raise RuntimeError(f"yt-dlp {version} was downloaded before and would not load")

    target = UPDATES_DIR / version
    if not (target / _COMPLETE).is_file():
        # A staging folder of this process's own: a second VidiChord started
        # at the same moment is fetching the same release, and sharing one
        # would have each delete the other's files half-way through.
        try:
            UPDATES_DIR.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=UPDATES_DIR, prefix=version + _PARTIAL + "-"))
        except OSError as error:
            raise RuntimeError(f"could not write to {UPDATES_DIR} ({error})") from error
        try:
            _unpack(_wheel(release), staging)
            pin = next(
                (found[1] for line in info.get("requires_dist") or []
                 if (found := _EJS_PIN.match(line))),
                None,
            )
            if pin:
                _unpack(_wheel(_pypi("yt-dlp-ejs", pin)), staging)
            (staging / _COMPLETE).write_text(version, encoding="utf-8")
            if target.exists() and not (target / _COMPLETE).is_file():
                shutil.rmtree(target, ignore_errors=True)  # an unfinished copy
            try:
                staging.rename(target)
            except OSError:
                # The other VidiChord finished first. Its copy is as good.
                if not (target / _COMPLETE).is_file():
                    raise
        except OSError as error:
            raise RuntimeError(f"could not write to {UPDATES_DIR} ({error})") from error
        finally:
            # Whatever went wrong - a bad checksum as much as a full disk -
            # leaves no half-unpacked copy behind. After the rename there is
            # nothing here to remove.
            shutil.rmtree(staging, ignore_errors=True)
    _prune(keep=target)


def _wheel(release: dict) -> bytes:
    """A release's universal wheel, checked against PyPI's own digest."""
    wheels = [
        entry for entry in release.get("urls") or []
        if entry.get("packagetype") == "bdist_wheel"
        and str(entry.get("filename", "")).endswith("-py3-none-any.whl")
    ]
    if not wheels:
        name = (release.get("info") or {}).get("name", "the release")
        raise RuntimeError(f"PyPI has no wheel for {name} that VidiChord can use")
    entry = wheels[0]
    try:
        response = requests.get(entry["url"], timeout=_WHEEL_TIMEOUT)
        response.raise_for_status()
    except Exception as error:
        raise RuntimeError(f"{entry['filename']} could not be downloaded ({error})") from error
    expected = (entry.get("digests") or {}).get("sha256", "")
    actual = hashlib.sha256(response.content).hexdigest()
    if not expected or actual != expected:
        raise RuntimeError(f"{entry['filename']} did not match the checksum PyPI publishes")
    return response.content


def _unpack(wheel: bytes, directory: Path) -> None:
    """Unpack a wheel - a zip - refusing any entry that would land outside."""
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        for name in archive.namelist():
            parts = PurePosixPath(name).parts
            if name.startswith(("/", "\\")) or ".." in parts or ":" in name:
                raise RuntimeError(f"the downloaded wheel holds an unsafe path: {name}")
        archive.extractall(directory)


def _prune(keep: Path) -> None:
    """Remove copies no longer needed: all but ``keep`` and the one loaded.

    Only folders this module makes are touched - a version, or a version's
    staging folder - whatever else is in there is left alone. The loaded copy
    stays until the next upgrade, because a download still in flight on it
    may yet import a module from it. Copies set aside as broken stay too, so
    the same release is not fetched every day, and so do staging folders
    young enough that another VidiChord may still be filling them in.
    """
    now = time.time()
    for entry in UPDATES_DIR.iterdir() if UPDATES_DIR.is_dir() else []:
        if entry in (keep, _finder.directory) or not entry.is_dir():
            continue
        name, partial, _suffix = entry.name.partition(_PARTIAL)
        if not _VERSION_NAME.fullmatch(name):
            continue  # not ours, or set aside as broken
        if partial:
            try:
                if now - entry.stat().st_mtime < _STALE_PARTIAL:
                    continue
            except OSError:
                continue
        shutil.rmtree(entry, ignore_errors=True)
