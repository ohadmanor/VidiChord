"""HTTP API behaviour.

Covers the routes the frontend depends on, and the two things the previous
server got wrong: it would stream any file on disk, and it advertised byte
ranges without honouring them.
"""

import pytest
from fastapi.testclient import TestClient

from vidichord.config import Settings
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
    app.state.settings = Settings(library_dir=library, sheets_dir=tmp_path / "sheets")
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
