"""HTTP API behaviour.

Covers the routes the frontend depends on, and the two things the previous
server got wrong: it would stream any file on disk, and it advertised byte
ranges without honouring them.
"""

import pytest
from fastapi.testclient import TestClient

from vidichord.config import Settings
from vidichord.jobs import Job
from vidichord.models import (
    Bar,
    Beat,
    ChordsDoc,
    LyricLine,
    LyricsDoc,
    SheetDoc,
    SourceDoc,
)
from vidichord.project import SongProject
from vidichord.server import create_app

SONG_ID = "Test Artist - Test Song [abc123]"


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
    project.write(SheetDoc(title="Test Song", artist="Test Artist"))
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

    def test_fails_clearly_when_no_sheets_directory_is_set(self, client, app_and_library):
        app, library, _project = app_and_library
        app.state.settings = Settings(library_dir=library, sheets_dir=None)
        response = client.post(f"/api/songs/{SONG_ID}/export")
        assert response.status_code == 400
        assert "sheets directory" in response.json()["detail"].lower()


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
        assert client.get(f"/api/songs/{SONG_ID}/sheet").json()["blocks"] == []

    def test_saving_lyrics_for_review_leaves_the_sheet_alone(self, client):
        lyrics = client.get(f"/api/songs/{SONG_ID}/lyrics").json()
        lyrics["lines"][0]["text"] = "changed words"

        response = client.put(
            f"/api/songs/{SONG_ID}/lyrics?rebuild=false", json=lyrics
        )
        assert response.status_code == 200
        assert client.get(f"/api/songs/{SONG_ID}/sheet").json()["blocks"] == []

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
