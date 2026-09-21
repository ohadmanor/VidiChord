"""Source separation, and what the rest of the pipeline does with it.

Two rules are what these tests exist to hold. Separation never fails a run:
Demucs is optional, heavy, and absent from the release executable, so "there
are no stems" has to be an ordinary outcome that leaves the transcription and
the chords to carry on over the full mix. And a stem is only used while it
still describes the audio on disk - re-adding a local file must not leave
stage 2 transcribing the vocal of a different recording.

Nothing here needs Demucs, torch or any audio: the separator is stubbed, which
is also what keeps the suite honest on a machine where separation does work.
"""

import json

import pytest
from fastapi.testclient import TestClient

from vidichord import stems as stems_mod
from vidichord.stems import demucs_engine
from vidichord.config import Settings
from vidichord.lyrics.whisper_engine import Segment, Transcript
from vidichord.models import SourceDoc, StageState, StemFile, StemsDoc
from vidichord.pipeline import (
    DEFAULT_ORDER,
    StageContext,
    run_stage,
    stage2_lyrics,
    stages_from,
)
from vidichord.project import SongProject, audio_fingerprint
from vidichord.server import create_app

SONG_ID = "Some Band - Separated [abc123]"

STEM_FILES = [
    StemFile(name=name, filename=f"stems/{name}.ogg", bytes=64)
    for name in ("vocals", "drums", "bass", "other")
]


@pytest.fixture
def project(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    project = SongProject.create(library, SONG_ID)
    project.write(SourceDoc(title="Separated", artist="Some Band", duration=12.0))
    project.audio_path.write_bytes(b"RIFF" + bytes(range(256)) * 4)
    return project


@pytest.fixture
def context(project):
    return StageContext(
        project=project, settings=Settings(library_dir=project.root.parent)
    )


def _write_stems(project, *, fingerprint=None, model="htdemucs", vocals_db=-14.0):
    """Put a finished separation on disk, as stage 5 would leave it."""
    project.stems_dir.mkdir(exist_ok=True)
    for stem in STEM_FILES:
        (project.root / stem.filename).write_bytes(b"OggS" + bytes(60))
    document = StemsDoc(
        model=model,
        format="opus",
        duration=12.0,
        vocals_rms_db=vocals_db,
        audio_fingerprint=fingerprint
        if fingerprint is not None
        else audio_fingerprint(project.audio_path),
        stems=list(STEM_FILES),
    )
    project.write(document)
    return document


class _RecordingSeparator:
    """Stands in for Demucs, which needs torch, a model and real audio."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, audio_path, out_dir, settings=None, report=None):
        self.calls += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        files = {}
        for stem in STEM_FILES:
            path = out_dir / f"{stem.name}.ogg"
            path.write_bytes(b"OggS" + bytes(60))
            files[stem.name] = path
        return stems_mod.Separation(
            model="htdemucs",
            device="cpu",
            format="opus",
            duration=12.0,
            vocals_rms_db=-14.0,
            files=files,
        )


def _explode(*args, **kwargs):
    raise AssertionError("This path must not separate anything")


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_the_environment_can_switch_separation_off(self, monkeypatch):
        monkeypatch.setenv("VIDICHORD_DEMUCS", "0")
        assert "VIDICHORD_DEMUCS=0" in stems_mod.unavailable_reason()

    def test_settings_can_switch_separation_off(self, monkeypatch):
        monkeypatch.delenv("VIDICHORD_DEMUCS", raising=False)
        settings = Settings(stems_enabled=False)
        assert "switched off in settings" in stems_mod.unavailable_reason(settings)

    def test_a_missing_demucs_says_how_to_install_it(self, monkeypatch):
        monkeypatch.delenv("VIDICHORD_DEMUCS", raising=False)
        monkeypatch.setattr(demucs_engine, "demucs_installed", lambda: False)
        assert "pip install demucs" in stems_mod.unavailable_reason()


# ---------------------------------------------------------------------------
# Stage 5
# ---------------------------------------------------------------------------


class TestStageFive:
    def test_an_unavailable_separator_records_why_and_leaves_the_stage_done(
        self, context, monkeypatch
    ):
        # The whole point of the optional dependency: a machine without it
        # imports songs exactly as it always did.
        monkeypatch.setattr(stems_mod, "unavailable_reason", lambda settings=None: "no demucs here")
        monkeypatch.setattr(stems_mod, "separate", _explode)

        run_stage(5, context)

        document = context.project.read(StemsDoc)
        assert document.unavailable == "no demucs here"
        assert document.stems == []
        assert not document.separated
        state = context.project.read_manifest().stage(5).state
        assert state == StageState.DONE

    def test_a_separator_that_raises_degrades_rather_than_failing_the_run(
        self, context, monkeypatch
    ):
        monkeypatch.setattr(stems_mod, "unavailable_reason", lambda settings=None: "")

        def boom(*args, **kwargs):
            raise RuntimeError("out of memory")

        monkeypatch.setattr(stems_mod, "separate", boom)

        run_stage(5, context)

        document = context.project.read(StemsDoc)
        assert "out of memory" in document.unavailable
        assert not document.separated
        # Half-written stems would be worse than none at all.
        assert not context.project.stems_dir.exists()
        assert context.project.read_manifest().stage(5).state == StageState.DONE

    def test_a_successful_separation_is_recorded_with_its_files(
        self, context, monkeypatch
    ):
        separator = _RecordingSeparator()
        monkeypatch.setattr(stems_mod, "unavailable_reason", lambda settings=None: "")
        monkeypatch.setattr(stems_mod, "separate", separator)

        run_stage(5, context)

        document = context.project.read(StemsDoc)
        assert document.separated
        assert document.unavailable == ""
        assert [stem.name for stem in document.stems] == [
            "vocals", "drums", "bass", "other"
        ]
        assert document.path_for("vocals") == "stems/vocals.ogg"
        assert document.audio_fingerprint == audio_fingerprint(context.project.audio_path)

    def test_stems_are_not_separated_twice(self, context, monkeypatch):
        _write_stems(context.project)
        monkeypatch.setattr(stems_mod, "unavailable_reason", lambda settings=None: "")
        monkeypatch.setattr(stems_mod, "separate", _explode)

        run_stage(5, context)  # must not raise

    def test_replaced_audio_is_separated_again(self, context, monkeypatch):
        _write_stems(context.project, fingerprint="not-this-audio")
        separator = _RecordingSeparator()
        monkeypatch.setattr(stems_mod, "unavailable_reason", lambda settings=None: "")
        monkeypatch.setattr(stems_mod, "separate", separator)

        run_stage(5, context)

        assert separator.calls == 1

    def test_a_different_model_is_separated_again(self, context, monkeypatch):
        _write_stems(context.project, model="htdemucs_ft")
        separator = _RecordingSeparator()
        monkeypatch.setattr(stems_mod, "unavailable_reason", lambda settings=None: "")
        monkeypatch.setattr(stems_mod, "separate", separator)

        run_stage(5, context)

        assert separator.calls == 1

    def test_force_separates_again(self, context, monkeypatch):
        _write_stems(context.project)
        separator = _RecordingSeparator()
        monkeypatch.setattr(stems_mod, "unavailable_reason", lambda settings=None: "")
        monkeypatch.setattr(stems_mod, "separate", separator)
        context.params["force"] = True

        run_stage(5, context)

        assert separator.calls == 1

    def test_separation_needs_the_audio(self, context, monkeypatch):
        context.project.audio_path.unlink()
        monkeypatch.setattr(stems_mod, "separate", _explode)

        with pytest.raises(RuntimeError, match="Stage 1"):
            run_stage(5, context)


# ---------------------------------------------------------------------------
# What stage 2 transcribes
# ---------------------------------------------------------------------------


class TestTranscriptionInput:
    def test_the_vocal_is_transcribed_when_there_is_one(self, context):
        _write_stems(context.project)
        path, kind = stage2_lyrics._transcription_input(context)
        assert kind == "vocals"
        assert path.endswith("vocals.ogg")

    def test_the_mix_is_transcribed_without_stems(self, context):
        path, kind = stage2_lyrics._transcription_input(context)
        assert kind == "mix"
        assert path == str(context.project.audio_path)

    def test_replaced_audio_falls_back_to_the_mix(self, context):
        # The stems describe a recording that is no longer here; timing the
        # words against them would time the wrong performance.
        _write_stems(context.project, fingerprint="not-this-audio")
        _path, kind = stage2_lyrics._transcription_input(context)
        assert kind == "mix"

    def test_an_unavailable_document_falls_back_to_the_mix(self, context):
        context.project.write(StemsDoc(unavailable="no demucs here"))
        _path, kind = stage2_lyrics._transcription_input(context)
        assert kind == "mix"

    def test_a_missing_stem_file_falls_back_to_the_mix(self, context):
        _write_stems(context.project)
        (context.project.root / "stems" / "vocals.ogg").unlink()
        _path, kind = stage2_lyrics._transcription_input(context)
        assert kind == "mix"


class TestTranscriptCache:
    def test_a_mix_transcript_is_not_reused_for_the_vocal(self, context):
        stage2_lyrics._save_transcript(
            context, "en", [{"text": "hello"}], vocals_detected=True, audio_input="mix"
        )
        assert stage2_lyrics._load_transcript(context, expected_input="mix") is not None
        assert stage2_lyrics._load_transcript(context, expected_input="vocals") is None

    def test_a_transcript_from_before_separation_reads_as_the_mix(self, context):
        from vidichord.lyrics import whisper_engine

        path = context.project.root / stage2_lyrics.TRANSCRIPT_FILENAME
        path.write_text(
            json.dumps(
                {
                    "detector": whisper_engine.DETECTION_MODEL,
                    "language": "en",
                    "segments": [],
                    "vocals_detected": True,
                }
            ),
            encoding="utf-8",
        )
        assert stage2_lyrics._load_transcript(context, expected_input="mix") is not None
        assert stage2_lyrics._load_transcript(context, expected_input="vocals") is None

    def test_transcribing_the_vocal_records_that_it_did(self, context, monkeypatch):
        _write_stems(context.project)
        seen = {}

        class _Engine:
            def transcribe(self, audio_path, language=None, initial_prompt=None, on_progress=None):
                seen["path"] = audio_path
                return Transcript(
                    language="en",
                    segments=[Segment(start=0.0, end=1.0, text="hello")],
                )

        monkeypatch.setattr(stage2_lyrics, "WhisperEngine", _Engine)

        stage2_lyrics._transcribe(context)

        assert seen["path"].endswith("vocals.ogg")
        stored = json.loads(
            (context.project.root / stage2_lyrics.TRANSCRIPT_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        assert stored["input"] == "vocals"


class TestInstrumentalHint:
    """The wording changes with the evidence; the question never does."""

    def test_a_silent_separated_vocal_is_said_plainly(self, context):
        _write_stems(context.project, vocals_db=-70.0)
        hint = stage2_lyrics._instrumental_hint(
            context, [{"text": "la la"}], vocals_detected=True
        )
        assert "separated vocal track is silent" in hint

    def test_a_loud_separated_vocal_is_not_called_instrumental(self, context):
        _write_stems(context.project, vocals_db=-14.0)
        hint = stage2_lyrics._instrumental_hint(
            context, [{"text": "la la"}], vocals_detected=True
        )
        assert "instrumental" not in hint

    def test_without_stems_the_voice_activity_verdict_still_speaks(self, context):
        hint = stage2_lyrics._instrumental_hint(context, [], vocals_detected=False)
        assert "may be instrumental" in hint


# ---------------------------------------------------------------------------
# Running order
# ---------------------------------------------------------------------------


class TestRunningOrder:
    def test_separation_runs_between_the_audio_and_the_lyrics(self):
        assert DEFAULT_ORDER == (1, 5, 2, 3, 4)
        assert stages_from(1) == (1, 5, 2, 3, 4)

    def test_re_separating_carries_on_into_the_lyrics(self):
        # Stage 2 reads what stage 5 wrote, so it cannot be left behind.
        assert stages_from(5) == (5, 2, 3, 4)

    def test_a_reviewed_run_still_stops_before_the_sheet(self):
        assert stages_from(1, review=True) == (1, 5, 2, 3)
        assert stages_from(5, review=True) == (5, 2, 3)

    def test_a_later_stage_does_not_go_backwards(self):
        assert stages_from(4) == (4,)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    app = create_app()
    app.state.settings = Settings(library_dir=library, path=tmp_path / "config.json")

    project = SongProject.create(library, SONG_ID)
    project.write(SourceDoc(title="Separated", artist="Some Band"))
    project.audio_path.write_bytes(b"RIFF" + bytes(range(256)) * 4)
    return TestClient(app), project


class TestApi:
    def test_the_stems_document_is_404_until_the_stage_runs(self, client):
        http, _project = client
        assert http.get(f"/api/songs/{SONG_ID}/stems").status_code == 404

    def test_the_stems_document_is_served_once_it_exists(self, client):
        http, project = client
        _write_stems(project)

        response = http.get(f"/api/songs/{SONG_ID}/stems")

        assert response.status_code == 200
        assert response.json()["model"] == "htdemucs"
        assert len(response.json()["stems"]) == 4

    def test_a_stem_streams_with_byte_ranges(self, client):
        http, project = client
        _write_stems(project)

        response = http.get(
            f"/api/songs/{SONG_ID}/stems/vocals", headers={"Range": "bytes=0-3"}
        )

        assert response.status_code == 206
        assert response.content == b"OggS"
        assert response.headers["content-range"].startswith("bytes 0-3/")

    def test_an_unknown_stem_name_is_rejected(self, client):
        http, project = client
        _write_stems(project)

        assert http.get(f"/api/songs/{SONG_ID}/stems/guitar").status_code == 404

    def test_a_stem_path_cannot_escape_the_song_folder(self, client):
        # The document is on disk, so a hand-edited or migrated one must not
        # be able to name a file outside the song's own stems directory.
        http, project = client
        project.write(
            StemsDoc(
                model="htdemucs",
                stems=[StemFile(name="vocals", filename="../../secrets.txt")],
            )
        )

        assert http.get(f"/api/songs/{SONG_ID}/stems/vocals").status_code == 403

    def test_a_missing_stem_file_is_404(self, client):
        http, project = client
        _write_stems(project)
        (project.root / "stems" / "vocals.ogg").unlink()

        assert http.get(f"/api/songs/{SONG_ID}/stems/vocals").status_code == 404

    def test_stage_five_can_be_re_run(self, client, monkeypatch):
        http, _project = client
        monkeypatch.setattr(stems_mod, "unavailable_reason", lambda settings=None: "nope")

        response = http.post(
            f"/api/songs/{SONG_ID}/stages/5/rerun", json={"cascade": False}
        )

        assert response.status_code == 200
        assert response.json()["song_id"] == SONG_ID

    def test_the_library_says_whether_a_song_can_be_mixed(self, client):
        http, project = client
        assert http.get("/api/songs").json()[0]["has_stems"] is False

        _write_stems(project)
        assert http.get("/api/songs").json()[0]["has_stems"] is True

    def test_separation_settings_survive_a_client_that_omits_them(self, client):
        http, _project = client
        http.put("/api/config", json={"library_dir": "", "sheets_dir": "", "stems_enabled": False})

        # An older client sends neither field; neither may be wiped.
        saved = http.put("/api/config", json={"library_dir": "", "sheets_dir": ""}).json()

        assert saved["stems_enabled"] is False
