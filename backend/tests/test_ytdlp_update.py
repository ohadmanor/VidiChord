"""yt-dlp keeps itself current, so the user is not asked to.

YouTube retires the player clients yt-dlp impersonates every few weeks, and a
stale copy is refused with a bare 403 the moment a download starts. These
cover the check at start-up and as a download starts, the upgrade after a
refusal - with pip from source, and by downloading wheels in the packaged
app - the two refusals that meet at the same upgrade, and what the user is
told.
"""

import hashlib
import io
import os
import subprocess
import sys
import threading
import time
import types
import zipfile
from pathlib import Path

import pytest

from vidichord.config import Settings
from vidichord.models import SourceDoc
from vidichord.pipeline import StageContext, stage1_audio, ytdlp_update
from vidichord.project import SongProject

OLD, NEW = "2026.07.04", "2026.08.19"
BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture()
def source_checkout(monkeypatch, tmp_path):
    """Running from source on OLD, nothing asked yet, PyPI unreachable."""
    monkeypatch.setattr(ytdlp_update, "_last_check", None)
    monkeypatch.setattr(ytdlp_update, "FROZEN", False)
    monkeypatch.setattr(stage1_audio, "FROZEN", False)
    monkeypatch.setattr(ytdlp_update, "latest", lambda: None)
    monkeypatch.setattr(ytdlp_update, "installed", lambda: OLD)
    monkeypatch.setattr(ytdlp_update, "_current", lambda: OLD)
    monkeypatch.delenv(ytdlp_update.DISABLE_ENV, raising=False)
    monkeypatch.delenv("VIDICHORD_COOKIES", raising=False)
    monkeypatch.delenv("VIDICHORD_COOKIES_BROWSER", raising=False)
    monkeypatch.setattr(stage1_audio, "DATA_DIR", tmp_path)
    return monkeypatch


def _upgrades(monkeypatch, result=None, error=None):
    """Stand in for upgrade(): record each call's ``stale``, then answer."""
    calls = []

    def upgrade(report=None, stale=None):
        calls.append(stale)
        if error is not None:
            raise RuntimeError(error)
        return result

    monkeypatch.setattr(ytdlp_update, "upgrade", upgrade)
    return calls


def _no_upgrade(monkeypatch):
    def upgrade(report=None, stale=None):
        pytest.fail("upgraded anyway")

    monkeypatch.setattr(ytdlp_update, "upgrade", upgrade)


# -- versions ----------------------------------------------------------------


def test_release_dates_compare_as_dates():
    assert ytdlp_update.is_newer("2026.8.19", OLD)
    assert not ytdlp_update.is_newer(OLD, "2026.8.19")


def test_zero_padding_does_not_make_a_release_look_new():
    """yt-dlp reports 2026.07.04; PyPI calls the same release 2026.7.4."""
    assert not ytdlp_update.is_newer("2026.7.4", OLD)
    assert not ytdlp_update.is_newer(OLD, "2026.7.4")


def test_a_master_build_is_newer_than_the_release_of_its_day():
    assert ytdlp_update.is_newer("2026.8.19.123456", "2026.8.19")


def test_a_version_that_is_not_a_date_is_never_newer():
    assert not ytdlp_update.is_newer("unknown", "2026.8.19")


def test_anything_is_newer_than_a_version_that_is_not_known():
    assert ytdlp_update.is_newer("2026.8.19", None)


# -- the check ---------------------------------------------------------------


def test_the_check_runs_at_start_up_without_holding_it_up(source_checkout):
    ran = threading.Event()
    source_checkout.setattr(ytdlp_update, "ensure_current", lambda report=None: ran.set())

    thread = ytdlp_update.check_in_background()
    thread.join(5)

    assert ran.is_set()
    assert thread.daemon


def test_main_starts_the_check_before_the_server():
    """The start-up check is only worth having if it runs before any song."""
    source = (BACKEND / "main.py").read_text(encoding="utf-8")

    assert source.index("check_in_background()") < source.index("uvicorn.run(")


def test_a_newer_release_on_pypi_is_installed(source_checkout):
    source_checkout.setattr(ytdlp_update, "latest", lambda: "2026.8.19")
    calls = _upgrades(source_checkout, result=NEW)

    ytdlp_update.ensure_current()

    assert len(calls) == 1


def test_pypi_is_asked_once_a_day_not_once_a_song(source_checkout):
    asked = []
    source_checkout.setattr(ytdlp_update, "latest", lambda: asked.append(1) or OLD)

    ytdlp_update.ensure_current()
    ytdlp_update.ensure_current()

    assert len(asked) == 1


def test_being_current_means_no_upgrade(source_checkout):
    source_checkout.setattr(ytdlp_update, "latest", lambda: "2026.7.4")
    _no_upgrade(source_checkout)

    ytdlp_update.ensure_current()


def test_being_offline_is_not_an_error(source_checkout):
    # The fixture's latest() already answers None, as it does with no network.
    _no_upgrade(source_checkout)

    ytdlp_update.ensure_current()


def test_no_answer_from_pypi_is_asked_again_soon_not_tomorrow(source_checkout):
    """At start-up the network is often not up yet."""
    clock = {"now": 1000.0}
    source_checkout.setattr(ytdlp_update.time, "monotonic", lambda: clock["now"])
    asked = []
    source_checkout.setattr(ytdlp_update, "latest", lambda: asked.append(clock["now"]))

    ytdlp_update.ensure_current()
    clock["now"] += 5 * 60
    ytdlp_update.ensure_current()
    clock["now"] += 11 * 60
    ytdlp_update.ensure_current()

    assert asked == [1000.0, 1000.0 + 16 * 60]


def test_an_upgrade_that_fails_does_not_stop_anything(source_checkout, capsys):
    source_checkout.setattr(ytdlp_update, "latest", lambda: "2026.8.19")
    _upgrades(source_checkout, error="pip exploded")

    ytdlp_update.ensure_current()

    assert "pip exploded" in capsys.readouterr().err


def test_pypi_is_asked_without_holding_up_other_downloads(source_checkout):
    """The lock guards imports too; a slow PyPI must not keep it."""
    held = []
    source_checkout.setattr(
        ytdlp_update, "latest", lambda: held.append(ytdlp_update._lock._is_owned())
    )

    ytdlp_update.ensure_current()

    assert held == [False]


def test_updating_can_be_turned_off(source_checkout):
    source_checkout.setenv(ytdlp_update.DISABLE_ENV, "1")
    source_checkout.setattr(ytdlp_update, "latest", lambda: pytest.fail("asked PyPI"))

    ytdlp_update.ensure_current()
    with pytest.raises(RuntimeError, match=ytdlp_update.DISABLE_ENV):
        ytdlp_update.upgrade()


def _project(tmp_path, kind, url):
    project = SongProject.create(tmp_path / "library", "Artist - Title [abc123]")
    project.write(SourceDoc(kind=kind, url=url))
    return StageContext(project=project, settings=Settings(library_dir=tmp_path))


def test_the_check_runs_again_as_a_youtube_download_starts(source_checkout, tmp_path):
    """For an app left running for days; in the job, where progress shows."""
    order = []
    source_checkout.setattr(
        ytdlp_update, "ensure_current", lambda report=None: order.append("check")
    )
    source_checkout.setattr(
        stage1_audio, "download", lambda *args, **kwargs: order.append("download")
    )

    stage1_audio.run(_project(tmp_path, "youtube", "https://youtu.be/x"))

    assert order == ["check", "download"]


def test_a_local_file_is_not_a_reason_to_ask(source_checkout, tmp_path):
    song = tmp_path / "Artist - Title.wav"
    song.write_bytes(b"RIFF")
    source_checkout.setattr(
        ytdlp_update, "ensure_current", lambda report=None: pytest.fail("asked")
    )

    stage1_audio.run(_project(tmp_path, "local", str(song)))


# -- upgrading from source: pip ----------------------------------------------


def _pip(monkeypatch, returncode=0, stderr="", after=NEW, loaded=True):
    """Stand in for pip: record the command, and 'install' ``after``.

    ``loaded`` says whether yt-dlp was imported before the upgrade. Returns
    the commands pip was run with and the times the package was unloaded.
    """
    commands, unloads = [], []
    monkeypatch.setattr(ytdlp_update, "available", lambda: OLD)
    if loaded:
        monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))
    else:
        monkeypatch.delitem(sys.modules, "yt_dlp", raising=False)

    def run(command, **kwargs):
        commands.append(command)
        monkeypatch.setattr(ytdlp_update, "available", lambda: after)
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr=stderr)

    def unload():
        unloads.append(1)
        monkeypatch.setattr(ytdlp_update, "installed", lambda: after)

    monkeypatch.setattr(ytdlp_update.subprocess, "run", run)
    monkeypatch.setattr(ytdlp_update, "_unload", unload)
    return commands, unloads


def test_the_upgrade_runs_pip_on_this_interpreter_and_reports_the_new_version(
    source_checkout,
):
    commands, unloads = _pip(source_checkout)

    assert ytdlp_update.upgrade() == NEW

    [command] = commands
    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    assert "--upgrade" in command
    assert command[-1] == ytdlp_update.PACKAGE
    assert unloads == [1]


def test_the_upgrade_asks_for_the_default_extra_so_the_solver_script_matches():
    """yt-dlp silently discards a yt-dlp-ejs it did not expect; the extra pins it."""
    assert ytdlp_update.PACKAGE == "yt-dlp[default]"
    assert ytdlp_update.PACKAGE in ytdlp_update.MANUAL_COMMAND


def test_at_start_up_there_is_nothing_to_unload(source_checkout, capsys):
    """The point of checking before anything imports yt-dlp."""
    _commands, unloads = _pip(source_checkout, loaded=False)

    assert ytdlp_update.upgrade() == NEW
    assert unloads == []
    # Not "yt-dlp is now": it has not been loaded yet.
    assert "the next song will use it" in capsys.readouterr().err


def test_nothing_newer_means_none_and_nothing_unloaded(source_checkout):
    """pip spells the release it left alone 2026.7.4; that is still 2026.07.04."""
    _commands, unloads = _pip(source_checkout, after="2026.7.4")
    source_checkout.setattr(ytdlp_update, "latest", lambda: "2026.7.4")

    assert ytdlp_update.upgrade() is None
    assert unloads == []


def test_a_copy_someone_else_already_replaced_is_the_upgrade(source_checkout):
    """The second of two refusals must not run pip again - or say 'newest'."""
    commands, _unloads = _pip(source_checkout)
    source_checkout.setattr(ytdlp_update, "_current", lambda: NEW)

    assert ytdlp_update.upgrade(stale=OLD) == NEW
    assert commands == []


def test_the_copy_the_caller_was_refused_on_is_replaced(source_checkout):
    commands, _unloads = _pip(source_checkout)

    assert ytdlp_update.upgrade(stale=OLD) == NEW
    assert len(commands) == 1


def test_pip_installing_nothing_while_pypi_has_newer_is_a_failure(source_checkout):
    """pip exits 0 when its index is unreachable or behind. That is not
    'already the newest', and saying so would have the user wait days."""
    _pip(source_checkout, after=OLD)
    source_checkout.setattr(ytdlp_update, "latest", lambda: "2026.8.19")

    with pytest.raises(RuntimeError, match="did not install yt-dlp 2026.8.19"):
        ytdlp_update.upgrade()


def test_pip_installing_nothing_with_pypi_unreachable_is_a_failure(source_checkout):
    _pip(source_checkout, after=OLD)

    with pytest.raises(RuntimeError, match="PyPI could not be reached"):
        ytdlp_update.upgrade()


def test_pips_last_line_is_the_reason_it_failed(source_checkout):
    _pip(
        source_checkout,
        returncode=1,
        stderr="Collecting yt-dlp\nERROR: No matching distribution found for yt-dlp\n",
    )

    with pytest.raises(RuntimeError, match="No matching distribution"):
        ytdlp_update.upgrade()


def test_a_pip_that_cannot_be_run_is_a_reason_too(source_checkout):
    def run(command, **kwargs):
        raise OSError("no such file")

    source_checkout.setattr(ytdlp_update.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="no such file"):
        ytdlp_update.upgrade()


# -- upgrading the packaged app: wheels from PyPI ----------------------------


def _wheel(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


YT_DLP_WHEEL = _wheel({
    "yt_dlp/__init__.py": "",
    "yt_dlp/version.py": "__version__ = '2026.08.19'\n",
    "yt_dlp-2026.8.19.dist-info/METADATA": "Name: yt-dlp\nVersion: 2026.8.19\n",
})
EJS_WHEEL = _wheel({"yt_dlp_ejs/__init__.py": "version = '0.8.0'\n"})


def _release(name, version, filename, wheel, digest=None, **info):
    return {
        "info": {"name": name, "version": version, **info},
        "urls": [
            {
                "packagetype": "sdist",
                "filename": f"{name}-{version}.tar.gz",
                "url": "https://files.example/sdist",
                "digests": {"sha256": "0" * 64},
            },
            {
                "packagetype": "bdist_wheel",
                "filename": filename,
                "url": f"https://files.example/{filename}",
                "digests": {"sha256": digest or hashlib.sha256(wheel).hexdigest()},
            },
        ],
    }


class _Response:
    def __init__(self, payload=None, content=b""):
        self._payload, self.content = payload, content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture()
def packaged(source_checkout, tmp_path):
    """The exe, built with OLD, nothing downloaded yet, nothing imported."""
    source_checkout.setattr(ytdlp_update, "FROZEN", True)
    source_checkout.setattr(stage1_audio, "FROZEN", True)
    source_checkout.setattr(ytdlp_update, "UPDATES_DIR", tmp_path / "VidiChord_yt-dlp")
    source_checkout.setattr(ytdlp_update, "_metadata_version", lambda: OLD)
    source_checkout.setattr(ytdlp_update, "_current", ytdlp_update.__dict__["_current"])
    source_checkout.delitem(sys.modules, "yt_dlp", raising=False)
    source_checkout.setattr(
        ytdlp_update.subprocess, "run", lambda *args, **kwargs: pytest.fail("ran pip")
    )

    pypi = {
        "yt-dlp": _release(
            "yt-dlp", "2026.8.19", "yt_dlp-2026.8.19-py3-none-any.whl", YT_DLP_WHEEL,
            requires_python=">=3.10",
            requires_dist=[
                'requests<3,>=2.32.2; extra == "default"',
                'yt-dlp-ejs==0.8.0; extra == "default"',
            ],
        ),
        "yt-dlp-ejs/0.8.0": _release(
            "yt-dlp-ejs", "0.8.0", "yt_dlp_ejs-0.8.0-py3-none-any.whl", EJS_WHEEL
        ),
    }
    files = {
        "yt_dlp-2026.8.19-py3-none-any.whl": YT_DLP_WHEEL,
        "yt_dlp_ejs-0.8.0-py3-none-any.whl": EJS_WHEEL,
    }
    fetched = []

    def get(url, timeout=None):
        fetched.append(url)
        if url.startswith("https://pypi.org/pypi/"):
            key = url.removeprefix("https://pypi.org/pypi/").removesuffix("/json")
            return _Response(payload=pypi[key])
        return _Response(content=files[url.rsplit("/", 1)[1]])

    source_checkout.setattr(ytdlp_update.requests, "get", get)
    return types.SimpleNamespace(
        pypi=pypi, files=files, fetched=fetched, updates=tmp_path / "VidiChord_yt-dlp"
    )


def test_the_exe_downloads_the_newest_release_beside_itself(packaged):
    assert ytdlp_update.upgrade() == "2026.8.19"

    copy = packaged.updates / "2026.8.19"
    assert (copy / "yt_dlp" / "version.py").is_file()
    assert (copy / "yt_dlp_ejs" / "__init__.py").is_file()
    assert [entry.name for entry in packaged.updates.iterdir()] == ["2026.8.19"]


def test_the_exe_fetches_the_yt_dlp_ejs_its_yt_dlp_pins(packaged):
    """yt-dlp discards any other yt-dlp-ejs, silently."""
    ytdlp_update.upgrade()

    assert "https://pypi.org/pypi/yt-dlp-ejs/0.8.0/json" in packaged.fetched


def test_a_wheel_that_does_not_match_pypis_checksum_is_refused(packaged):
    wheel = packaged.pypi["yt-dlp"]["urls"][1]
    wheel["digests"]["sha256"] = "f" * 64

    with pytest.raises(RuntimeError, match="checksum"):
        ytdlp_update.upgrade()

    assert not any(packaged.updates.iterdir())


def test_a_wheel_that_would_write_outside_its_folder_is_refused(packaged, tmp_path):
    hostile = _wheel({"../../escaped.py": "", "yt_dlp/version.py": ""})
    packaged.files["yt_dlp-2026.8.19-py3-none-any.whl"] = hostile
    packaged.pypi["yt-dlp"]["urls"][1]["digests"]["sha256"] = hashlib.sha256(
        hostile
    ).hexdigest()

    with pytest.raises(RuntimeError, match="unsafe path"):
        ytdlp_update.upgrade()

    assert not (tmp_path.parent / "escaped.py").exists()


def test_a_release_for_a_newer_python_than_the_exes_is_not_fetched(packaged):
    packaged.pypi["yt-dlp"]["info"]["requires_python"] = ">=3.99"

    with pytest.raises(RuntimeError, match="needs Python 3.99"):
        ytdlp_update.upgrade()

    assert not any("files.example" in url for url in packaged.fetched)


def test_nothing_newer_on_pypi_means_nothing_fetched(packaged):
    packaged.pypi["yt-dlp"]["info"]["version"] = "2026.7.4"

    assert ytdlp_update.upgrade() is None
    assert not any("files.example" in url for url in packaged.fetched)


def test_a_release_that_would_not_load_before_is_not_fetched_again(packaged):
    (packaged.updates / "2026.8.19.broken" / "yt_dlp").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="would not load"):
        ytdlp_update.upgrade()


def test_older_copies_are_cleared_away_but_not_the_broken_marker(packaged):
    names = (
        "2026.7.20", "2026.8.1.broken",
        "2026.8.2.partial-stale", "2026.8.3.partial-in-progress",
    )
    for name in names:
        (packaged.updates / name / "yt_dlp").mkdir(parents=True)
    two_hours_ago = time.time() - 2 * 60 * 60
    os.utime(packaged.updates / "2026.8.2.partial-stale", (two_hours_ago, two_hours_ago))

    ytdlp_update.upgrade()

    assert sorted(entry.name for entry in packaged.updates.iterdir()) == [
        "2026.8.1.broken", "2026.8.19",
        # Young enough that another VidiChord may still be filling it in.
        "2026.8.3.partial-in-progress",
    ]


def test_nothing_that_is_not_ours_is_ever_removed(packaged):
    """The folder is the app's, but a user can still put things in it."""
    for name in ("downloads", ".git", "notes"):
        (packaged.updates / name).mkdir(parents=True)
        (packaged.updates / name / "keep.txt").write_text("mine", encoding="utf-8")
    (packaged.updates / "yt-dlp.exe").write_bytes(b"MZ")

    ytdlp_update.upgrade()

    assert sorted(entry.name for entry in packaged.updates.iterdir()) == [
        ".git", "2026.8.19", "downloads", "notes", "yt-dlp.exe",
    ]


def test_the_folder_is_named_for_the_app():
    """Not 'yt-dlp': a user may well have a folder of that name beside the exe."""
    assert ytdlp_update.UPDATES_DIR.name == "VidiChord_yt-dlp"


def test_a_copy_is_only_used_once_it_is_whole(packaged):
    """The marker is written last; a copy without it was cut short."""
    ytdlp_update.upgrade()
    copy = packaged.updates / "2026.8.19"
    assert (copy / ytdlp_update._COMPLETE).is_file()

    (copy / ytdlp_update._COMPLETE).unlink()

    assert ytdlp_update._chosen() is None


def test_a_copy_another_vidichord_finished_first_is_accepted(packaged, monkeypatch):
    """Two exes started together fetch the same release; the slower one's
    rename meets the faster one's finished copy."""
    unpack = ytdlp_update._unpack

    def unpack_while_another_finishes(wheel, directory):
        unpack(wheel, directory)
        other = packaged.updates / "2026.8.19"
        if not other.exists():
            unpack(wheel, other)
            (other / ytdlp_update._COMPLETE).write_text("2026.8.19", encoding="utf-8")

    monkeypatch.setattr(ytdlp_update, "_unpack", unpack_while_another_finishes)

    assert ytdlp_update.upgrade() == "2026.8.19"
    assert [entry.name for entry in packaged.updates.iterdir()] == ["2026.8.19"]


def test_only_a_folder_named_for_a_version_is_a_copy(packaged):
    """'2100.1.1.broken' starts like a version; choosing it again would loop."""
    for name in ("2100.1.1.broken", "2100.1.2.partial-x", "2026.8.19"):
        package = packaged.updates / name / "yt_dlp"
        package.mkdir(parents=True)
        (package / "version.py").write_text("", encoding="utf-8")
        (package.parent / ytdlp_update._COMPLETE).write_text("", encoding="utf-8")

    assert [copy.name for copy in ytdlp_update._copies()] == ["2026.8.19"]


def test_a_copy_older_than_the_exes_own_is_not_used(packaged):
    """What an older build downloaded, once a newer build has replaced it."""
    old = packaged.updates / "2026.7.1" / "yt_dlp"
    old.mkdir(parents=True)
    (old / "version.py").write_text("__version__ = '2026.07.01'\n", encoding="utf-8")
    (old.parent / ytdlp_update._COMPLETE).write_text("", encoding="utf-8")

    assert ytdlp_update._chosen() is None
    assert ytdlp_update.available() == OLD


def test_an_exe_that_cannot_write_beside_itself_says_so(packaged, monkeypatch):
    def refuse(self, *args, **kwargs):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(Path, "mkdir", refuse)

    with pytest.raises(RuntimeError, match="could not write"):
        ytdlp_update.upgrade()


#: A fresh interpreter plays the exe: FROZEN, with copies downloaded into a
#: temporary folder, and the venv's yt-dlp standing in for the bundled one.
_LOAD_AS_THE_EXE = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
updates = Path(sys.argv[2])
from vidichord.pipeline import ytdlp_update
ytdlp_update.FROZEN = True
ytdlp_update.UPDATES_DIR = updates
ytdlp_update._metadata_version = lambda: "2000.1.1"

yt_dlp = ytdlp_update.load()
import yt_dlp.version
import yt_dlp_ejs
print(yt_dlp.version.__version__)
print(Path(yt_dlp.__file__).parent.parent.name)
print(Path(yt_dlp.version.__file__).is_relative_to(updates))
print(Path(yt_dlp_ejs.__file__).is_relative_to(updates))
print(sorted(entry.name for entry in updates.iterdir()))
"""


def _copy(updates, version, init="", with_init=True):
    package = updates / version / "yt_dlp"
    package.mkdir(parents=True)
    if with_init:
        (package / "__init__.py").write_text(init, encoding="utf-8")
    (package / "version.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")
    (package.parent / ytdlp_update._COMPLETE).write_text(version, encoding="utf-8")


def _run_as_the_exe(updates):
    pytest.importorskip("yt_dlp")
    result = subprocess.run(
        [sys.executable, "-c", _LOAD_AS_THE_EXE, str(BACKEND), str(updates)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split("\n"), result.stderr


def test_the_exe_loads_its_downloaded_copy_ahead_of_its_own(tmp_path):
    """The whole package, submodules too - not a mixture of two releases."""
    _copy(tmp_path, "2099.1.1")

    (version, folder, submodule_downloaded, ejs_downloaded, _rest, _), _ = _run_as_the_exe(
        tmp_path
    )

    assert version == "2099.1.1"
    assert folder == "2099.1.1"
    assert submodule_downloaded == "True"
    # The copy holds no yt-dlp-ejs, so the bundled one is still used.
    assert ejs_downloaded == "False"


def test_a_downloaded_copy_that_will_not_load_is_set_aside(tmp_path):
    _copy(tmp_path, "2099.1.1")
    _copy(tmp_path, "2100.1.1", init="raise ImportError('broken release')\n")

    (version, *_rest, folders, _), stderr = _run_as_the_exe(tmp_path)

    assert version == "2099.1.1"
    assert folders == "['2099.1.1', '2100.1.1.broken']"
    assert "broken release" in stderr
    # Says which copy it fell back on - not the bundle, here.
    assert "Using yt-dlp the copy downloaded earlier (2099.1.1) instead." in stderr


def test_a_copy_without_its_init_is_not_taken_for_yt_dlp(tmp_path):
    """A folder with no __init__.py imports as an empty namespace package,
    without complaint. It would then fail every song."""
    _copy(tmp_path, "2099.1.1")
    _copy(tmp_path, "2100.1.1", with_init=False)

    (version, *_rest, folders, _), _stderr = _run_as_the_exe(tmp_path)

    assert version == "2099.1.1"
    assert folders == "['2099.1.1', '2100.1.1.broken']"


def test_with_every_copy_broken_the_exe_falls_back_on_its_own(tmp_path):
    _copy(tmp_path, "2100.1.1", init="raise ImportError('broken release')\n")

    (version, folder, *_rest), _ = _run_as_the_exe(tmp_path)

    assert version != "2100.1.1"
    assert folder != "2100.1.1"


def test_the_exe_is_told_what_it_can_do_when_the_update_fails(
    packaged, youtube_refuses, tmp_path
):
    """The exe has no pip to hand the user."""
    packaged.pypi["yt-dlp"]["urls"][1]["digests"]["sha256"] = "f" * 64

    message = _failure(tmp_path)

    assert message.startswith(stage1_audio.STALE_YTDLP)
    assert "checksum" in message
    assert stage1_audio.MANUAL_PACKAGED in message
    assert ytdlp_update.MANUAL_COMMAND not in message


# -- unloading the old copy --------------------------------------------------


def test_the_old_yt_dlp_is_forgotten_so_the_next_import_is_the_new_one():
    ours = ("yt_dlp", "yt_dlp.extractor", "yt_dlp.extractor.youtube", "yt_dlp_ejs")
    # Whatever of the real package other tests loaded goes back afterwards.
    loaded = {
        name: module
        for name, module in sys.modules.items()
        if name.split(".", 1)[0] in ytdlp_update._PACKAGES
    }
    try:
        for name in ours:
            sys.modules[name] = types.ModuleType(name)
        sys.modules["yt_dlp_unrelated"] = types.ModuleType("yt_dlp_unrelated")

        ytdlp_update._unload()

        assert not any(
            name.split(".", 1)[0] in ytdlp_update._PACKAGES for name in sys.modules
        )
        assert "yt_dlp_unrelated" in sys.modules
    finally:
        sys.modules.pop("yt_dlp_unrelated", None)
        sys.modules.update(loaded)


#: Loads the real yt-dlp, unloads and reloads it as an upgrade does, and then
#: asks a local server for a page through yt-dlp's own networking. Run in a
#: fresh interpreter, so this session's yt-dlp - real or fake - is untouched.
_RELOAD_FOR_REAL = r"""
import http.server, sys, threading
sys.path.insert(0, sys.argv[1])
from vidichord.pipeline import ytdlp_update

class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *args):
        pass

server = http.server.HTTPServer(("127.0.0.1", 0), Page)
threading.Thread(target=server.serve_forever, daemon=True).start()

class Complaints:
    seen = []
    def debug(self, message): pass
    def info(self, message): pass
    def warning(self, message): self.seen.append(message)
    def error(self, message): self.seen.append(message)

import urllib3.util.url

ytdlp_update.load()
finders = len(sys.meta_path)
for _ in range(3):
    ytdlp_update._unload()
    yt_dlp = ytdlp_update.load()
    # urllib3 2.8 calls .sub on the pattern, which a twice-wrapped proxy has
    # lost; 2.7 only calls .subn, which it keeps. Ask for both, so the test
    # does not depend on which urllib3 is installed.
    urllib3.util.url._PERCENT_RE.sub("", "%41")
    urllib3.util.url._PERCENT_RE.subn("", "%41")
    with yt_dlp.YoutubeDL({"quiet": True, "logger": Complaints()}) as ydl:
        body = ydl.urlopen(f"http://127.0.0.1:{server.server_port}/a%41b").read()
    assert body == b"ok", body
print(len(sys.meta_path) - finders, Complaints.seen)
"""


def test_the_real_yt_dlp_survives_being_reloaded():
    """What an upgrade does to a running process, on the yt-dlp installed.

    A naive reload wraps urllib3's pattern twice, after which - on urllib3
    2.8 - every request through ``requests`` fails and yt-dlp complains on
    each one. It also leaves a pair of plugin finders behind. If a future
    yt-dlp patches something else that way, this is where it shows - before a
    release carries it.
    """
    pytest.importorskip("yt_dlp")
    result = subprocess.run(
        [sys.executable, "-c", _RELOAD_FOR_REAL, str(BACKEND)],
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0 []"


def test_an_import_waits_for_an_upgrade_in_progress(monkeypatch):
    """Unloading a package another thread is half-way through importing
    breaks that import. So imports and upgrades take turns."""
    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))
    loaded = threading.Event()

    with ytdlp_update._lock:
        worker = threading.Thread(target=lambda: ytdlp_update.load() and loaded.set())
        worker.start()
        assert not loaded.wait(0.2)
    worker.join(5)

    assert loaded.is_set()


# -- a refused request -------------------------------------------------------

REFUSED = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
INFO = {"id": "x", "title": "Artist - Title", "uploader": "Artist"}


class _FakeYoutubeDL:
    """yt-dlp as stage 1 sees it: answers, in order, then delivers.

    An answer is an error message to raise, or None to succeed.
    """

    answers: list = []
    seen: list = []

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _answer(self):
        _FakeYoutubeDL.seen.append(type(self).__name__)
        answer = type(self).answers.pop(0)
        if answer is not None:
            raise RuntimeError(answer)

    def download(self, urls):
        self._answer()
        Path(self.options["outtmpl"].replace("%(ext)s", "wav")).write_bytes(b"RIFF")
        return 0

    def extract_info(self, url, download=False):
        self._answer()
        return dict(INFO)


@pytest.fixture()
def youtube(source_checkout, tmp_path):
    """A fake yt-dlp, an ffmpeg that is there, and no waiting between attempts."""
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = _FakeYoutubeDL
    source_checkout.setitem(sys.modules, "yt_dlp", module)
    source_checkout.setattr(stage1_audio, "ensure_ffmpeg", lambda report=None: tmp_path)
    source_checkout.setattr(stage1_audio.time, "sleep", lambda seconds: None)
    _FakeYoutubeDL.answers = []
    _FakeYoutubeDL.seen = []
    return _FakeYoutubeDL


@pytest.fixture()
def youtube_refuses(source_checkout, tmp_path, monkeypatch):
    """A fake yt-dlp that refuses, served without disturbing a packaged fixture."""
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = _FakeYoutubeDL
    _FakeYoutubeDL.answers = [REFUSED, REFUSED]
    _FakeYoutubeDL.seen = []
    real_load = ytdlp_update.load
    monkeypatch.setattr(ytdlp_update, "load", lambda: module)
    monkeypatch.setattr(ytdlp_update, "installed", lambda: OLD)
    monkeypatch.setattr(stage1_audio, "ensure_ffmpeg", lambda report=None: tmp_path)
    monkeypatch.setattr(stage1_audio.time, "sleep", lambda seconds: None)
    return real_load


def _download(tmp_path, report=None, name="audio.wav"):
    destination = tmp_path / name
    stage1_audio.download("https://youtu.be/x", destination, report)
    return destination


def _failure(tmp_path):
    with pytest.raises(RuntimeError) as failure:
        _download(tmp_path)
    return str(failure.value)


def test_a_refused_download_updates_yt_dlp_and_asks_again(
    youtube, source_checkout, tmp_path
):
    youtube.answers = [REFUSED, None]
    calls = _upgrades(source_checkout, result=NEW)
    progress = []

    destination = _download(tmp_path, lambda message, percent: progress.append(message))

    assert destination.is_file()
    assert calls == [OLD]
    assert len(youtube.seen) == 2
    assert any(NEW in message for message in progress)


def test_the_second_attempt_runs_on_the_yt_dlp_the_upgrade_installed(
    youtube, source_checkout, tmp_path
):
    """The upgrade swaps the package under the running process. A yt-dlp
    imported once, at the top of the download, would be the old one."""
    youtube.answers = [REFUSED]

    class Newer(_FakeYoutubeDL):
        answers = [None]

    def upgrade(report=None, stale=None):
        module = types.ModuleType("yt_dlp")
        module.YoutubeDL = Newer
        source_checkout.setitem(sys.modules, "yt_dlp", module)
        return NEW

    source_checkout.setattr(ytdlp_update, "upgrade", upgrade)

    assert _download(tmp_path).is_file()
    assert youtube.seen == ["_FakeYoutubeDL", "Newer"]


def test_being_the_newest_already_is_said_so(youtube, source_checkout, tmp_path):
    youtube.answers = [REFUSED]
    _upgrades(source_checkout, result=None)

    message = _failure(tmp_path)

    assert message.startswith(stage1_audio.STALE_YTDLP)
    assert f"already the newest release ({OLD})" in message
    assert len(youtube.seen) == 1


def test_an_update_that_fails_hands_over_the_command(youtube, source_checkout, tmp_path):
    youtube.answers = [REFUSED]
    _upgrades(source_checkout, error="No matching distribution found for yt-dlp")

    message = _failure(tmp_path)

    assert "No matching distribution found for yt-dlp" in message
    assert ytdlp_update.MANUAL_COMMAND in message


def test_a_refusal_that_survives_the_update_is_youtube_being_ahead(
    youtube, source_checkout, tmp_path
):
    youtube.answers = [REFUSED, REFUSED]
    _upgrades(source_checkout, result=NEW)

    message = _failure(tmp_path)

    assert f"updated yt-dlp to {NEW} and YouTube still refused" in message
    assert len(youtube.seen) == 2


@pytest.mark.parametrize("frozen", [False, True])
def test_with_updating_turned_off_nothing_is_tried_and_a_restart_is_needed(
    youtube, source_checkout, tmp_path, frozen
):
    """A yt-dlp installed by hand is only picked up by a restart, then."""
    youtube.answers = [REFUSED]
    source_checkout.setenv(ytdlp_update.DISABLE_ENV, "1")
    source_checkout.setattr(stage1_audio, "FROZEN", frozen)

    message = _failure(tmp_path)

    assert "turned off" in message and "restart VidiChord" in message
    assert "tried to update" not in message
    assert (ytdlp_update.MANUAL_COMMAND in message) is not frozen


def test_a_refused_probe_is_updated_and_asked_again_too(youtube, source_checkout):
    """A retired client can be refused as early as the video's details."""
    youtube.answers = [REFUSED, None]
    calls = _upgrades(source_checkout, result=NEW)

    assert stage1_audio.probe("https://youtu.be/x")["id"] == "x"
    assert calls == [OLD]


def test_two_refusals_share_one_upgrade_and_both_songs_download(
    source_checkout, tmp_path
):
    """Songs added together are refused together. The first upgrades; the
    second must retry on that copy - not run pip again, and not be told
    yt-dlp is already the newest release and to wait days."""
    state = {"version": OLD}
    both_refused = threading.Barrier(2, timeout=5)
    commands = []

    class Versioned(_FakeYoutubeDL):
        def download(self, urls):
            if state["version"] == OLD:
                both_refused.wait()
                raise RuntimeError(REFUSED)
            Path(self.options["outtmpl"].replace("%(ext)s", "wav")).write_bytes(b"RIFF")
            return 0

    def pip(command, **kwargs):
        commands.append(command)
        state["version"] = NEW
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = Versioned
    source_checkout.setitem(sys.modules, "yt_dlp", module)
    for name in ("installed", "_current", "available"):
        source_checkout.setattr(ytdlp_update, name, lambda: state["version"])
    source_checkout.setattr(ytdlp_update, "_unload", lambda: None)
    source_checkout.setattr(ytdlp_update.subprocess, "run", pip)
    source_checkout.setattr(stage1_audio, "ensure_ffmpeg", lambda report=None: tmp_path)

    errors = []

    def song(name):
        try:
            _download(tmp_path, name=name)
        except Exception as error:
            errors.append(str(error))

    workers = [threading.Thread(target=song, args=(f"{n}.wav",)) for n in "ab"]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)

    assert errors == []
    assert (tmp_path / "a.wav").is_file() and (tmp_path / "b.wav").is_file()
    assert len(commands) == 1


# -- what is not a stale yt-dlp ----------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        # A song folder is named after the song, and errors quote paths.
        "ERROR: unable to open for writing: [Errno 13] Permission denied: "
        "'C:\\library\\Nick Cave - Forbidden Fruit [2c1988]\\audio.webm.part'",
        "ERROR: Postprocessing: 'C:\\library\\Band - 403 Miles [ab12cd]\\audio.wav': "
        "Invalid argument",
        "No space left on device",
    ],
)
def test_an_unrelated_failure_is_not_an_update(youtube, source_checkout, tmp_path, failure):
    youtube.answers = [failure]
    _no_upgrade(source_checkout)

    assert _failure(tmp_path) == failure


def test_a_sign_in_demand_is_not_a_stale_yt_dlp(youtube, source_checkout, tmp_path):
    """A 403 with the bot wording around it wants a cookie, not an update."""
    youtube.answers = [
        "ERROR: HTTP Error 403: Forbidden. Sign in to confirm you're not a bot"
    ]
    _no_upgrade(source_checkout)

    assert _failure(tmp_path).startswith(stage1_audio.HEADLINE)


def test_a_faltering_connection_is_still_retried_before_any_of_this(
    youtube, source_checkout, tmp_path
):
    youtube.answers = ["The read operation timed out", None]
    _no_upgrade(source_checkout)

    assert _download(tmp_path).is_file()
    assert len(youtube.seen) == 2
