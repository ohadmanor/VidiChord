"""Project folders, artifact round-trips and song identifiers."""

import pytest

from vidichord.models import (
    Bar,
    Beat,
    ChordsDoc,
    LyricLine,
    LyricsDoc,
    LyricsSource,
    Manifest,
    Section,
    SectionKind,
    SheetDoc,
    SourceDoc,
    StageState,
    Word,
)
from vidichord.project import SongProject, make_song_id, summarise


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    return root


class TestSongId:
    def test_builds_a_readable_identifier(self):
        song_id = make_song_id("Elvis Presley", "Always On My Mind", seed="u1")
        assert song_id.startswith("Elvis Presley - Always On My Mind [")

    def test_preserves_hebrew(self):
        song_id = make_song_id("רמי קלינשטיין", "עוד לא תמו", seed="u1")
        assert "רמי קלינשטיין" in song_id

    def test_strips_characters_windows_forbids(self):
        song_id = make_song_id('A:B*C?', 'D"E<F>G|H', seed="u1")
        assert not any(ch in song_id for ch in ':*?"<>|\\/')

    def test_same_song_different_sources_do_not_collide(self):
        a = make_song_id("Artist", "Title", seed="https://youtu.be/aaa")
        b = make_song_id("Artist", "Title", seed="https://youtu.be/bbb")
        assert a != b

    def test_is_stable_for_the_same_input(self):
        assert make_song_id("A", "B", seed="x") == make_song_id("A", "B", seed="x")

    def test_long_titles_are_truncated(self):
        song_id = make_song_id("x" * 200, "y" * 200, seed="u")
        assert len(song_id) < 100

    def test_empty_input_still_yields_an_id(self):
        assert make_song_id("", "", seed="u").startswith("untitled")


class TestArtifactRoundTrip:
    def test_writes_and_reads_every_artifact(self, library):
        project = SongProject.create(library, "test-song")

        source = SourceDoc(kind="youtube", url="https://x", title="T", artist="A",
                           duration=123.4)
        lyrics = LyricsDoc(
            language="he", source=LyricsSource.GENIUS, title="T", artist="A",
            sections=[Section(index=0, name="Verse 1", kind=SectionKind.VERSE,
                              line_start=0, line_end=0)],
            lines=[LyricLine(index=0, section_index=0, time=1.0, end=2.0, text="שלום",
                             words=[Word(text="שלום", start=1.0, end=1.5, offset=0)])],
        )
        chords = ChordsDoc(
            bpm=90.5, time_signature=3, key="G major", downbeats_tracked=True,
            bars=[Bar(index=1, start=0.0, end=2.0, beats=[
                Beat(index=0, beat_in_bar=1, start=0.0, end=1.0, chord="G",
                     sources={"librosa": "G"})
            ])],
        )
        sheet = SheetDoc(title="T", artist="A", is_rtl=True)

        for document in (source, lyrics, chords, sheet):
            project.write(document)

        assert project.read(SourceDoc).duration == 123.4
        restored = project.read(LyricsDoc)
        assert restored.language == "he"
        assert restored.is_rtl
        assert restored.lines[0].words[0].text == "שלום"
        assert restored.sections[0].kind is SectionKind.VERSE

        restored_chords = project.read(ChordsDoc)
        assert restored_chords.time_signature == 3
        assert restored_chords.bars[0].beats[0].sources == {"librosa": "G"}
        assert project.read(SheetDoc).is_rtl

    def test_missing_artifact_reads_as_none(self, library):
        project = SongProject.create(library, "empty-song")
        assert project.read_optional(LyricsDoc) is None
        assert not project.has(ChordsDoc)

    def test_bar_chords_collapse_consecutive_repeats(self):
        bar = Bar(index=1, start=0.0, end=2.0, beats=[
            Beat(index=i, beat_in_bar=i + 1, start=float(i), end=i + 1.0, chord=chord)
            for i, chord in enumerate(["C", "C", "G", "N"])
        ])
        assert bar.chords == ["C", "G"]


class TestManifest:
    def test_records_stage_transitions(self, library):
        project = SongProject.create(library, "song")
        project.update_stage(2, StageState.RUNNING, message="working")
        manifest = project.read_manifest()
        assert manifest.stages["lyrics"].state is StageState.RUNNING
        assert manifest.stages["lyrics"].message == "working"

        project.update_stage(2, StageState.DONE)
        assert project.read_manifest().stages["lyrics"].state is StageState.DONE

    def test_summary_lists_stage_states(self, library):
        project = SongProject.create(library, "song")
        project.update_stage(1, StageState.DONE)
        summary = summarise(project)
        assert summary["song_id"] == "song"
        assert summary["stages"]["audio"] == StageState.DONE.value

    def test_an_artifact_on_disk_counts_as_a_finished_stage(self, library):
        """The folder is the source of truth, not a possibly-stale manifest."""
        project = SongProject.create(library, "song")
        project.write(SheetDoc(title="built outside the job system"))
        assert summarise(project)["stages"]["sheet"] == StageState.DONE.value

    def test_a_failed_stage_is_not_overridden_by_a_stale_artifact(self, library):
        project = SongProject.create(library, "song")
        project.write(SheetDoc())
        project.update_stage(4, StageState.FAILED, error="boom")
        assert summarise(project)["stages"]["sheet"] == StageState.FAILED.value

    def test_new_manifest_starts_pending(self):
        manifest = Manifest(song_id="x")
        assert all(s.state is StageState.PENDING for s in manifest.stages.values())


class TestPathSafety:
    def test_open_rejects_traversal(self, library):
        SongProject.create(library, "real-song")
        for attempt in ("../..", "..", "../real-song", "../../Windows"):
            with pytest.raises(KeyError):
                SongProject.open(library, attempt)

    def test_open_rejects_unknown_songs(self, library):
        with pytest.raises(KeyError):
            SongProject.open(library, "not-there")

    def test_iter_all_finds_only_real_projects(self, library):
        SongProject.create(library, "one")
        SongProject.create(library, "two")
        (library / "stray").mkdir()  # no manifest, so not a project
        assert {p.song_id for p in SongProject.iter_all(library)} == {"one", "two"}


def test_atomic_write_leaves_no_partial_file(library):
    project = SongProject.create(library, "song")
    project.write(SheetDoc(title="first"))
    project.write(SheetDoc(title="second"))
    assert project.read(SheetDoc).title == "second"
    assert not list(project.root.glob("*.tmp"))
