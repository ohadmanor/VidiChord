"""HTTP API behaviour.

Covers the routes the frontend depends on, and the two things the previous
server got wrong: it would stream any file on disk, and it advertised byte
ranges without honouring them.
"""

import json
import os
import stat
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vidichord.config import Settings
from vidichord.jobs import Job
from vidichord.models import (
    Bar,
    Beat,
    ChordsDoc,
    LyricBlock,
    LyricLine,
    LyricsDoc,
    SheetDoc,
    SourceDoc,
)
from vidichord.project import SongProject
from vidichord.server import create_app
from vidichord.sheet import export

SONG_ID = "Test Artist - Test Song [abc123]"

#: One sung line, so a sheet has something in it to export.
_LINE = LyricBlock(chord_line="C", text="hello", start=0.0, end=2.0, line_index=0)


@pytest.fixture
def app_and_library(tmp_path):
    library = tmp_path / "library"
    library.mkdir()

    app = create_app()
    # Point `path` at a throwaway file: PUT /api/config persists to disk, and
    # a test must never write over the real backend/config.json.
    app.state.settings = Settings(
        library_dir=library,
        sheets_dir=tmp_path / "sheets",
        path=tmp_path / "config.json",
    )
    (tmp_path / "sheets").mkdir()

    project = SongProject.create(library, SONG_ID)
    project.write(SourceDoc(title="Test Song", artist="Test Artist", duration=10.0))
    project.write(LyricsDoc(
        title="Test Song", artist="Test Artist", language="en",
        lines=[LyricLine(index=0, time=0.0, end=2.0, text="hello")],
    ))
    project.write(ChordsDoc(bpm=120.0, key="C major", bars=[
        Bar(index=1, start=0.0, end=2.0, beats=[
            Beat(index=0, beat_in_bar=1, start=0.0, end=2.0, chord="C")
        ])
    ]))
    project.write(SheetDoc(title="Test Song", artist="Test Artist", blocks=[_LINE]))
    project.audio_path.write_bytes(b"RIFF" + bytes(range(256)) * 4)

    return app, library, project


@pytest.fixture
def client(app_and_library):
    app, _library, _project = app_and_library
    return TestClient(app)


class TestLibrary:
    def test_lists_songs(self, client):
        response = client.get("/api/songs")
        assert response.status_code == 200
        assert [s["song_id"] for s in response.json()] == [SONG_ID]

    def test_song_detail_includes_the_manifest(self, client):
        response = client.get(f"/api/songs/{SONG_ID}")
        assert response.status_code == 200
        assert response.json()["manifest"]["song_id"] == SONG_ID

    def test_unknown_song_is_404(self, client):
        assert client.get("/api/songs/nope").status_code == 404

    def test_a_run_paused_for_lyrics_says_so_without_its_job(self, client, app_and_library):
        """The pause has to outlive the job that raised it.

        Jobs are held in memory, so restarting the server - or simply running
        fifty more - forgets that a song is waiting to be told what its lyrics
        are. The app finds those songs again by reading the stage back, which
        only works if the state and the reason both survive on disk.
        """
        from vidichord.models import StageState

        _app, _library, project = app_and_library
        project.update_stage(
            2, StageState.NEEDS_INPUT, message="No lyrics found for this song."
        )

        body = client.get(f"/api/songs/{SONG_ID}").json()

        assert body["job"] is None
        assert body["stages"]["lyrics"] == "needs_input"
        assert body["manifest"]["stages"]["lyrics"]["message"] == (
            "No lyrics found for this song."
        )

    def test_a_paused_stage_is_not_reported_done_because_its_file_exists(
        self, client, app_and_library
    ):
        """A stale 02_lyrics.json from an earlier run must not hide the pause."""
        from vidichord.models import StageState

        _app, _library, project = app_and_library
        assert project.has(LyricsDoc)
        project.update_stage(2, StageState.NEEDS_INPUT, message="No lyrics found.")

        assert client.get("/api/songs").json()[0]["stages"]["lyrics"] == "needs_input"

    def test_delete_removes_the_folder(self, client, app_and_library):
        _app, _library, project = app_and_library
        assert client.delete(f"/api/songs/{SONG_ID}").status_code == 204
        assert not project.root.exists()


class TestArtifacts:
    @pytest.mark.parametrize("name", ["source", "lyrics", "chords", "sheet"])
    def test_each_artifact_is_served(self, client, name):
        assert client.get(f"/api/songs/{SONG_ID}/{name}").status_code == 200

    def test_missing_artifact_is_404(self, client, app_and_library):
        _app, library, _project = app_and_library
        SongProject.create(library, "bare-song")
        assert client.get("/api/songs/bare-song/lyrics").status_code == 404

    def test_saving_chords_rebuilds_the_sheet(self, client):
        chords = client.get(f"/api/songs/{SONG_ID}/chords").json()
        chords["bars"][0]["beats"][0]["chord"] = "Am"

        assert client.put(f"/api/songs/{SONG_ID}/chords", json=chords).status_code == 200
        assert client.get(f"/api/songs/{SONG_ID}/chords").json()["bars"][0]["beats"][0][
            "chord"
        ] == "Am"

    def test_saving_lyrics_rebuilds_the_sheet(self, client):
        lyrics = client.get(f"/api/songs/{SONG_ID}/lyrics").json()
        lyrics["lines"][0]["text"] = "changed words"

        assert client.put(f"/api/songs/{SONG_ID}/lyrics", json=lyrics).status_code == 200
        sheet = client.get(f"/api/songs/{SONG_ID}/sheet").json()
        assert any(
            block.get("text") == "changed words"
            for block in sheet["blocks"]
        )

    def test_saving_a_sheet_does_not_rebuild_it(self, client):
        sheet = client.get(f"/api/songs/{SONG_ID}/sheet").json()
        sheet["title"] = "Hand Edited"
        assert client.put(f"/api/songs/{SONG_ID}/sheet", json=sheet).status_code == 200
        assert client.get(f"/api/songs/{SONG_ID}/sheet").json()["title"] == "Hand Edited"

    def test_rejects_a_malformed_document(self, client):
        response = client.put(f"/api/songs/{SONG_ID}/chords", json={"bpm": "not a number"})
        assert response.status_code == 422


class TestAudio:
    def test_serves_the_whole_file_without_a_range(self, client, app_and_library):
        _app, _library, project = app_and_library
        response = client.get(f"/api/songs/{SONG_ID}/audio")
        assert response.status_code == 200
        assert len(response.content) == project.audio_path.stat().st_size

    def test_honours_a_byte_range(self, client):
        """Seeking depends on this; the old server advertised it but ignored it."""
        response = client.get(
            f"/api/songs/{SONG_ID}/audio", headers={"Range": "bytes=10-19"}
        )
        assert response.status_code == 206
        assert len(response.content) == 10
        assert response.headers["content-range"].startswith("bytes 10-19/")

    def test_open_ended_range(self, client, app_and_library):
        _app, _library, project = app_and_library
        size = project.audio_path.stat().st_size
        response = client.get(
            f"/api/songs/{SONG_ID}/audio", headers={"Range": "bytes=100-"}
        )
        assert response.status_code == 206
        assert len(response.content) == size - 100

    def test_out_of_bounds_range_is_clamped(self, client):
        response = client.get(
            f"/api/songs/{SONG_ID}/audio", headers={"Range": "bytes=999999-"}
        )
        assert response.status_code == 206

    @pytest.mark.parametrize(
        "song_id",
        [
            "..%2F..%2Fsecret",
            "%2e%2e%2f%2e%2e%2fsecret",
            "..",
            "real%2F..%2F..%2Fx",
        ],
    )
    def test_will_not_serve_files_outside_the_library(self, client, song_id):
        """A traversal attempt must not reach the filesystem at large.

        Only percent-encoded forms are worth asserting on: an HTTP client
        normalises a literal "../.." out of the path before sending it, so
        that form never reaches the route at all.
        """
        assert client.get(f"/api/songs/{song_id}/audio").status_code in (403, 404)


class TestExport:
    def test_writes_a_songbook_file(self, client, app_and_library):
        _app, _library, _project = app_and_library
        response = client.post(f"/api/songs/{SONG_ID}/export")
        assert response.status_code == 200
        assert response.json()["filename"] == "Test Song - Test Artist.json"

    # Export used to refuse outright until a folder was set in Settings -
    # which on a fresh install is always - and every other thing that went
    # wrong while writing came back as a bare "500 Internal Server Error".

    def test_with_no_folder_chosen_it_exports_to_the_default_one(
        self, client, app_and_library, tmp_path, monkeypatch
    ):
        import vidichord.config as config_mod

        app, library, _project = app_and_library
        default = tmp_path / "default songbook"
        monkeypatch.setattr(config_mod, "DEFAULT_SHEETS_DIR", default)
        app.state.settings = Settings(library_dir=library, sheets_dir=None)

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 200
        assert (default / "Test Song - Test Artist.json").is_file()
        assert response.json()["folder"] == str(default)

    def test_the_settings_say_where_the_default_folder_is(self, client):
        from vidichord.config import DEFAULT_SHEETS_DIR

        assert client.get("/api/config").json()["sheets_dir_default"] == str(DEFAULT_SHEETS_DIR)

    def test_a_chosen_folder_that_does_not_exist_yet_is_created(self, client, app_and_library, tmp_path):
        app, library, _project = app_and_library
        folder = tmp_path / "new" / "songbook"
        app.state.settings = Settings(library_dir=library, sheets_dir=folder)

        assert client.post(f"/api/songs/{SONG_ID}/export").status_code == 200
        assert (folder / "Test Song - Test Artist.json").is_file()

    @pytest.mark.parametrize("typed", ['"{path}"', "  {path}  ", "'{path}'"])
    def test_a_folder_pasted_with_quotes_or_spaces_is_used(self, client, app_and_library, tmp_path, typed):
        """Explorer's "Copy as path" adds the quotes; a paste often adds a space."""
        _app, _library, _project = app_and_library
        folder = tmp_path / "pasted"
        saved = client.put(
            "/api/config", json={"library_dir": "", "sheets_dir": typed.format(path=folder)}
        ).json()
        assert saved["sheets_dir"] == str(folder)

        # PUT replaced the settings, library included - point it back.
        client.app.state.settings.library_dir = _library
        assert client.post(f"/api/songs/{SONG_ID}/export").status_code == 200
        assert (folder / "Test Song - Test Artist.json").is_file()

    def test_a_song_with_no_sheet_yet_is_told_to_sync_first(self, client, app_and_library):
        _app, _library, project = app_and_library
        (project.root / "04_sheet.json").unlink()

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 409
        assert "Sync" in response.json()["detail"]

    def test_an_empty_sheet_is_not_exported_as_a_blank_song(self, client, app_and_library, tmp_path):
        _app, _library, project = app_and_library
        project.write(SheetDoc(title="Test Song", artist="Test Artist", blocks=[]))

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 409
        assert "empty" in response.json()["detail"]
        assert not any((tmp_path / "sheets").iterdir())

    def test_a_folder_that_is_really_a_file_is_explained(self, client, app_and_library, tmp_path):
        app, library, _project = app_and_library
        not_a_folder = tmp_path / "songbook.txt"
        not_a_folder.write_text("x", encoding="utf-8")
        app.state.settings = Settings(library_dir=library, sheets_dir=not_a_folder)

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 400
        assert "not a folder" in response.json()["detail"]

    def test_exporting_again_updates_the_same_file_with_the_same_id(self, client, app_and_library, tmp_path):
        first = client.post(f"/api/songs/{SONG_ID}/export").json()
        second = client.post(f"/api/songs/{SONG_ID}/export").json()

        assert first["path"] == second["path"]
        assert [p.name for p in (tmp_path / "sheets").iterdir()] == ["Test Song - Test Artist.json"]
        written = json.loads(Path(second["path"]).read_text(encoding="utf-8"))
        assert written["id"] == export.songbook_id(SONG_ID)
        assert written[export.SONG_ID_FIELD] == SONG_ID

    def test_another_song_with_the_same_name_does_not_overwrite_it(self, client, app_and_library, tmp_path):
        """Two songs can clean to the same filename; neither may silently lose its export."""
        _app, library, _project = app_and_library
        other_id = "Test Artist - Test Song [other9]"
        other = SongProject.create(library, other_id)
        other.write(SheetDoc(title="Test Song", artist="Test Artist", blocks=[_LINE]))

        mine = client.post(f"/api/songs/{SONG_ID}/export").json()
        theirs = client.post(f"/api/songs/{urllib.parse.quote(other_id)}/export").json()

        assert mine["filename"] == "Test Song - Test Artist.json"
        assert theirs["filename"] == "Test Song - Test Artist (2).json"
        assert json.loads(Path(mine["path"]).read_text(encoding="utf-8"))[export.SONG_ID_FIELD] == SONG_ID

    def test_a_read_only_export_is_explained_and_left_alone(self, client, app_and_library, tmp_path):
        existing = tmp_path / "sheets" / "Test Song - Test Artist.json"
        client.post(f"/api/songs/{SONG_ID}/export")
        before = existing.read_bytes()
        os.chmod(existing, stat.S_IREAD)
        try:
            response = client.post(f"/api/songs/{SONG_ID}/export")

            assert response.status_code == 400
            assert "read-only" in response.json()["detail"]
            assert existing.read_bytes() == before
        finally:
            os.chmod(existing, stat.S_IWRITE | stat.S_IREAD)

    def test_a_failed_write_leaves_the_previous_export_intact(
        self, client, app_and_library, tmp_path, monkeypatch
    ):
        """No truncated or half-written file where a good one was, and no litter."""
        existing = tmp_path / "sheets" / "Test Song - Test Artist.json"
        client.post(f"/api/songs/{SONG_ID}/export")
        before = existing.read_bytes()

        def full_disk(src, dst):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(export.os, "replace", full_disk)
        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 400
        assert "no room" in response.json()["detail"]
        assert existing.read_bytes() == before
        assert [p.name for p in (tmp_path / "sheets").iterdir()] == ["Test Song - Test Artist.json"]

    def test_anything_unexpected_still_answers_in_words(self, client, app_and_library, monkeypatch):
        def broken(folder, sheet, song_id):
            raise ValueError("something odd")

        monkeypatch.setattr(export, "write_export", broken)
        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 500
        assert "something odd" in response.json()["detail"]

    @pytest.mark.parametrize(
        "title",
        ["Line one\nline two", "Tab\there", "CON", "nul.song", "?*<>|", "🎸" * 200, "ש" * 300],
    )
    def test_any_title_exports(self, client, app_and_library, tmp_path, title):
        _app, _library, project = app_and_library
        project.write(SheetDoc(title=title, artist="Test Artist", blocks=[_LINE]))

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 200, response.json()
        written = Path(response.json()["path"])
        assert written.is_file()
        assert json.loads(written.read_text(encoding="utf-8"))["title"] == title


class TestConfig:
    def test_reads_and_writes_settings(self, client, tmp_path):
        assert client.get("/api/config").status_code == 200
        response = client.put(
            "/api/config",
            json={"library_dir": str(tmp_path / "new"), "sheets_dir": ""},
        )
        assert response.status_code == 200
        assert response.json()["library_dir"] == str(tmp_path / "new")

    def test_saving_does_not_touch_the_real_config_file(self, client, tmp_path):
        from vidichord.config import CONFIG_PATH

        before = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.is_file() else None
        client.put(
            "/api/config",
            json={"library_dir": str(tmp_path / "elsewhere"), "sheets_dir": ""},
        )
        after = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.is_file() else None
        assert after == before


class TestRouting:
    def test_unknown_api_route_is_404_not_the_app_shell(self, client):
        response = client.get("/api/does-not-exist")
        assert response.status_code == 404

    def test_stage_number_is_validated(self, client):
        assert client.post(f"/api/songs/{SONG_ID}/stages/9/rerun", json={}).status_code == 400

    def test_lyrics_choice_is_validated(self, client):
        assert client.post(
            f"/api/songs/{SONG_ID}/lyrics/choice", json={"choice": "wrong"}
        ).status_code == 400
        assert client.post(
            f"/api/songs/{SONG_ID}/lyrics/choice", json={"choice": "manual", "lyrics": ""}
        ).status_code == 400

    def test_unknown_job_is_404(self, client):
        assert client.get("/api/jobs/nope").status_code == 404


class _RecordingJobs:
    """Stands in for the JobManager, recording runs instead of starting them.

    Which stages a request would run is the behaviour under test; actually
    running them needs real audio.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def submit(self, song_id, stages, _build_context):
        self.calls.append((song_id, tuple(stages)))
        return Job(job_id="job", song_id=song_id, stages=tuple(stages))

    def for_song(self, _song_id):
        return None

    def get(self, _job_id):
        return None


class TestReview:
    """The review step: correct the lyrics and chords, then sync."""

    @pytest.fixture
    def recorder(self, app_and_library):
        app, _library, _project = app_and_library
        recorder = _RecordingJobs()
        app.state.jobs = recorder
        return recorder

    def test_a_reviewed_run_stops_before_the_sheet(self, client, recorder):
        client.post(f"/api/songs/{SONG_ID}/stages/2/rerun", json={"review": True})
        assert recorder.calls[-1][1] == (2, 3)

    def test_an_ordinary_run_still_reaches_the_sheet(self, client, recorder):
        client.post(f"/api/songs/{SONG_ID}/stages/2/rerun", json={})
        assert recorder.calls[-1][1] == (2, 3, 4)

    def test_review_never_drops_the_stage_that_was_asked_for(self, client, recorder):
        # Reviewing stops before stage 4, but asking for stage 4 outranks that.
        client.post(f"/api/songs/{SONG_ID}/stages/4/rerun", json={"review": True})
        assert recorder.calls[-1][1] == (4,)

    def test_a_reviewed_lyrics_choice_stops_before_the_sheet(self, client, recorder):
        client.post(
            f"/api/songs/{SONG_ID}/lyrics/choice",
            json={"choice": "ai", "review": True},
        )
        assert recorder.calls[-1][1] == (2, 3)

    def test_an_instrumental_choice_is_accepted_without_lyrics(self, client, recorder):
        # The whole point of the choice is that there are no lyrics to send.
        response = client.post(
            f"/api/songs/{SONG_ID}/lyrics/choice",
            json={"choice": "instrumental", "review": True},
        )
        assert response.status_code == 200
        assert recorder.calls[-1][1] == (2, 3)

    def test_saving_chords_for_review_leaves_the_sheet_alone(self, client):
        chords = client.get(f"/api/songs/{SONG_ID}/chords").json()
        chords["bars"][0]["beats"][0]["chord"] = "Am"

        response = client.put(
            f"/api/songs/{SONG_ID}/chords?rebuild=false", json=chords
        )
        assert response.status_code == 200
        assert client.get(f"/api/songs/{SONG_ID}/chords").json()["bars"][0]["beats"][0][
            "chord"
        ] == "Am"
        # The fixture's sheet has no blocks; a rebuild would have given it some.
        # Untouched: still the fixture's one line, not rebuilt from the edit.
        assert [b["text"] for b in client.get(f"/api/songs/{SONG_ID}/sheet").json()["blocks"]] == ["hello"]

    def test_saving_lyrics_for_review_leaves_the_sheet_alone(self, client):
        lyrics = client.get(f"/api/songs/{SONG_ID}/lyrics").json()
        lyrics["lines"][0]["text"] = "changed words"

        response = client.put(
            f"/api/songs/{SONG_ID}/lyrics?rebuild=false", json=lyrics
        )
        assert response.status_code == 200
        # Untouched: still the fixture's one line, not rebuilt from the edit.
        assert [b["text"] for b in client.get(f"/api/songs/{SONG_ID}/sheet").json()["blocks"]] == ["hello"]

    def test_sync_builds_the_sheet_from_both_and_returns_it(self, client):
        lyrics = client.get(f"/api/songs/{SONG_ID}/lyrics").json()
        lyrics["lines"][0]["text"] = "changed words"
        client.put(f"/api/songs/{SONG_ID}/lyrics?rebuild=false", json=lyrics)

        chords = client.get(f"/api/songs/{SONG_ID}/chords").json()
        chords["bars"][0]["beats"][0]["chord"] = "Am"
        client.put(f"/api/songs/{SONG_ID}/chords?rebuild=false", json=chords)

        response = client.post(f"/api/songs/{SONG_ID}/sync")
        assert response.status_code == 200

        blocks = response.json()["blocks"]
        assert any(block.get("text") == "changed words" for block in blocks)
        # Both edits reach the sheet from one sync, not just the last saved.
        assert any("Am" in (block.get("chord_line") or "") for block in blocks)
        # And what came back is what was stored.
        assert client.get(f"/api/songs/{SONG_ID}/sheet").json() == response.json()

    def test_sync_records_the_sheet_stage_as_done(self, client):
        # The library shows a pill per stage; a sheet built by sync is as done
        # as one built by a pipeline run.
        assert client.post(f"/api/songs/{SONG_ID}/sync").status_code == 200
        stages = client.get(f"/api/songs/{SONG_ID}").json()["stages"]
        assert stages["sheet"] == "done"

    def test_sync_without_chords_is_an_error_not_a_crash(self, client, app_and_library):
        _app, library, _project = app_and_library
        SongProject.create(library, "bare-song")
        assert client.post("/api/songs/bare-song/sync").status_code == 400


class TestSettingsPaths:
    """What is typed into Settings, made into the folder it means."""

    @pytest.mark.parametrize(
        "typed,expected",
        [
            (r'"C:\Songs"', r"C:\Songs"),
            (r"  C:\Songs  ", r"C:\Songs"),
            (r"'C:\Songs'", r"C:\Songs"),
            # Drive-relative: the current folder on that drive, which nobody means.
            ("C:", "C:\\"),
            ("C:Songs", r"C:\Songs"),
            ("", None),
            ("   ", None),
        ],
    )
    def test_typed_paths_mean_what_they_look_like(self, typed, expected):
        from vidichord.config import clean_path

        result = clean_path(typed)
        assert (str(result) if result is not None else None) == expected

    def test_a_relative_path_is_taken_from_the_app_folder(self):
        from vidichord.config import DATA_DIR, clean_path

        assert clean_path("songbook") == DATA_DIR / "songbook"

    def test_a_folder_in_the_way_of_the_export_name_is_stepped_around(
        self, client, app_and_library, tmp_path
    ):
        (tmp_path / "sheets" / "Test Song - Test Artist.json").mkdir()

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 200
        assert response.json()["filename"] == "Test Song - Test Artist (2).json"


class TestSavingSettings:
    def test_a_config_file_that_cannot_be_written_is_explained(self, client, app_and_library, tmp_path):
        app, _library, _project = app_and_library
        config = app.state.settings.path
        config.write_text("{}", encoding="utf-8")
        os.chmod(config, stat.S_IREAD)
        try:
            response = client.put(
                "/api/config", json={"library_dir": str(tmp_path / "library"), "sheets_dir": str(tmp_path / "x")}
            )
            assert response.status_code == 400
            assert "could not be saved" in response.json()["detail"]
            # Nothing changed.
            assert app.state.settings.sheets_dir == tmp_path / "sheets"
        finally:
            os.chmod(config, stat.S_IWRITE | stat.S_IREAD)

    def test_a_library_on_a_missing_drive_is_refused_before_anything_is_written(
        self, client, app_and_library, tmp_path, monkeypatch
    ):
        app, _library, _project = app_and_library
        config = app.state.settings.path

        def no_such_drive(self, *args, **kwargs):
            raise FileNotFoundError(3, "The system cannot find the path specified")

        monkeypatch.setattr(Path, "mkdir", no_such_drive)
        response = client.put(
            "/api/config", json={"library_dir": r"Q:\VidiChord_Files", "sheets_dir": ""}
        )

        assert response.status_code == 400
        assert "library folder" in response.json()["detail"]
        assert not config.exists()

    def test_settings_survive_a_start_while_the_library_drive_is_away(self, tmp_path, monkeypatch):
        """They used to be dropped for the defaults - the songbook folder with them."""
        config = tmp_path / "config.json"
        config.write_text(json.dumps({
            "library_dir": r"Q:\VidiChord_Files", "sheets_dir": str(tmp_path / "songbook"),
        }), encoding="utf-8")

        def no_such_drive(self, *args, **kwargs):
            raise FileNotFoundError(3, "The system cannot find the path specified")

        monkeypatch.setattr(Path, "mkdir", no_such_drive)
        loaded = Settings.load(config)

        assert loaded.sheets_dir == tmp_path / "songbook"
        assert str(loaded.library_dir) == r"Q:\VidiChord_Files"


class TestExportRound2:
    """Cases the fix itself had missed or introduced, found by re-running them all."""

    def test_an_export_made_before_the_song_id_was_added_is_updated_not_duplicated(
        self, client, app_and_library, tmp_path
    ):
        legacy = tmp_path / "sheets" / "Test Song - Test Artist.json"
        legacy.write_text(json.dumps({
            "id": "song_1790439802", "title": "Test Song", "artist": "Test Artist",
            "key": "C major", "isRTL": False, "rawText": "old\n", "modifiedByUser": True,
        }), encoding="utf-8")

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.json()["filename"] == "Test Song - Test Artist.json"
        assert [p.name for p in (tmp_path / "sheets").iterdir()] == ["Test Song - Test Artist.json"]
        assert json.loads(legacy.read_text(encoding="utf-8"))[export.SONG_ID_FIELD] == SONG_ID

    def test_a_hand_edited_file_that_is_not_json_is_left_alone(self, client, app_and_library, tmp_path):
        mine = tmp_path / "sheets" / "Test Song - Test Artist.json"
        mine.write_text("my own notes, not JSON", encoding="utf-8")

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.json()["filename"] == "Test Song - Test Artist (2).json"
        assert mine.read_text(encoding="utf-8") == "my own notes, not JSON"

    def test_an_own_export_that_cannot_be_read_is_reported_not_duplicated(
        self, client, app_and_library, tmp_path, monkeypatch
    ):
        client.post(f"/api/songs/{SONG_ID}/export")
        real_open = Path.open

        def locked(self, *args, **kwargs):
            if self.name == "Test Song - Test Artist.json":
                raise PermissionError(13, "Permission denied")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", locked)
        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 400
        assert "open in another program" in response.json()["detail"]
        assert [p.name for p in (tmp_path / "sheets").iterdir()] == ["Test Song - Test Artist.json"]

    def test_exports_at_the_same_moment_do_not_mix_up_their_files(self, app_and_library, tmp_path):
        """They shared one temporary file name; one song could land in another's file."""
        import threading as _threading

        app, library, _project = app_and_library
        base = "Symphony No. 9 in D minor, Op. 125 Choral - Movement"
        ids = []
        for n in range(4):
            song_id = f"Beethoven - Take {n} [t{n}]"
            project = SongProject.create(library, song_id)
            project.write(SheetDoc(title=f"{base} {n}", artist="Beethoven", blocks=[
                LyricBlock(chord_line="C", text=f"take {n}", start=0.0, end=1.0, line_index=0)
            ]))
            ids.append(song_id)
        http = TestClient(app)
        answers = {}

        def export_one(song_id):
            answers[song_id] = http.post(f"/api/songs/{urllib.parse.quote(song_id)}/export")

        workers = [_threading.Thread(target=export_one, args=(i,)) for i in ids for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        for n, song_id in enumerate(ids):
            assert answers[song_id].status_code == 200
            written = json.loads(Path(answers[song_id].json()["path"]).read_text(encoding="utf-8"))
            assert written[export.SONG_ID_FIELD] == song_id
            assert f"take {n}" in written["rawText"]
        assert not list((tmp_path / "sheets").glob("*.tmp"))

    def test_a_folder_that_takes_edits_but_not_new_files_is_named_as_such(
        self, client, app_and_library, monkeypatch
    ):
        def no_new_files(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(export.tempfile, "mkstemp", no_new_files)
        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 400
        assert "not allowed to create files" in response.json()["detail"]

    def test_a_sheet_that_cannot_be_read_is_explained(self, client, app_and_library, monkeypatch):
        from vidichord.project import SongProject as _Project

        def locked(self, model):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(_Project, "read_optional", locked)
        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 409
        assert "could not be read" in response.json()["detail"]

    def test_a_sheet_of_blank_lines_is_empty_too(self, client, app_and_library):
        _app, _library, project = app_and_library
        project.write(SheetDoc(title="Test Song", artist="Test Artist", blocks=[
            LyricBlock(chord_line="", text="", start=0.0, end=1.0, line_index=0)
        ]))

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 409
        assert "empty" in response.json()["detail"]

    def test_a_sheet_older_than_its_lyrics_asks_for_a_sync(self, client, app_and_library):
        _app, _library, project = app_and_library
        manifest = project.read_manifest()
        manifest.stages["sheet"].updated_at = "2026-01-01T00:00:00+00:00"
        manifest.stages["lyrics"].updated_at = "2026-01-02T00:00:00+00:00"
        project.write_manifest(manifest)

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 409
        assert "Sync" in response.json()["detail"]

    def test_a_surrogate_in_the_title_still_exports(self, client, app_and_library):
        """Only a hand-edited sheet gets one - the app cannot save it - but it exports."""
        _app, _library, project = app_and_library
        sheet = SheetDoc(title="PLACEHOLDER", artist="A", blocks=[_LINE]).model_dump_json()
        # A JSON escape for a lone surrogate, as a hand edit would leave it.
        (project.root / "04_sheet.json").write_text(
            sheet.replace("PLACEHOLDER", "Bad\\udc80Title"), encoding="utf-8"
        )

        response = client.post(f"/api/songs/{SONG_ID}/export")

        assert response.status_code == 200
        assert Path(response.json()["path"]).is_file()

    def test_emoji_sequences_and_joiners_survive_in_the_name(self):
        family = "\U0001F468‍\U0001F469‍\U0001F467"
        assert export.safe_filename(family, "A") == f"{family} - A.json"
        assert export.safe_filename("می‌خواهم", "A").startswith(
            "می‌"
        )

    def test_a_deleted_song_says_what_to_do(self, client):
        response = client.post("/api/songs/gone%20%5B000000%5D/export")

        assert response.status_code == 404
        assert "library" in response.json()["detail"]


class TestSettingsCheckTheFolder:
    def test_a_file_given_as_the_songbook_folder_is_refused_on_save(self, client, tmp_path):
        a_file = tmp_path / "songbook.txt"
        a_file.write_text("x", encoding="utf-8")

        response = client.put("/api/config", json={"library_dir": "", "sheets_dir": str(a_file)})

        assert response.status_code == 400
        assert "not a folder" in response.json()["detail"]

    def test_a_new_songbook_folder_is_made_on_save(self, client, tmp_path):
        folder = tmp_path / "brand" / "new"

        response = client.put("/api/config", json={"library_dir": "", "sheets_dir": str(folder)})

        assert response.status_code == 200
        assert folder.is_dir()
