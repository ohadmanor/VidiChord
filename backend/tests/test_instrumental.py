"""Songs with no lyrics.

The regular procedure never changes: every song is transcribed and the lyrics
are searched for online. What these tests pin down is the ending. When nothing
is found online the run asks the user - continue with the transcript as is,
paste the lyrics, or mark the song instrumental - rather than deciding for
them, and whichever way they answer must work end to end: continuing on an
empty transcript is a chords-only sheet, not "no lyrics could be produced",
and a silent track is never quietly dressed in a title-matched song's lyrics.
A lyrics document with no lines is the instrumental result, which stage 4
renders as bar charts.
"""

import json

import pytest

from vidichord.config import Settings
from vidichord.lyrics import providers
from vidichord.lyrics.whisper_engine import Segment, Transcript
from vidichord.models import Bar, Beat, ChordsDoc, LyricsDoc, LyricsSource, SourceDoc
from vidichord.pipeline import NeedsUserInput, StageContext, stage2_lyrics, stage4_sheet
from vidichord.project import SongProject

SONG_ID = "Some Band - Prelude [xyz789]"


def _sung(text: str) -> Segment:
    return Segment(start=0.0, end=2.0, text=text)


class _StubEngine:
    """Stands in for WhisperEngine; the real one needs audio and models."""

    def __init__(self, transcript: Transcript) -> None:
        self._transcript = transcript

    def transcribe(self, audio_path, language=None, initial_prompt=None, on_progress=None):
        return self._transcript


class _ExplodingEngine:
    """Proves a path never transcribes: constructing this is the failure."""

    def __init__(self) -> None:
        raise AssertionError("The instrumental choice must not transcribe")


@pytest.fixture
def context(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    project = SongProject.create(library, SONG_ID)
    project.write(SourceDoc(title="Prelude", artist="Some Band", duration=8.0))
    project.audio_path.write_bytes(b"RIFF fake audio")

    # No test may touch the network; a hit here is a test bug.
    monkeypatch.setattr(providers, "fetch_lrclib", lambda *args, **kwargs: None)
    monkeypatch.setattr(providers, "search_web_lyrics", lambda *args, **kwargs: None)

    settings = Settings(
        library_dir=library, sheets_dir=None, path=tmp_path / "config.json"
    )
    return StageContext(project=project, settings=settings)


def _install_engine(monkeypatch, transcript: Transcript) -> None:
    monkeypatch.setattr(
        stage2_lyrics, "WhisperEngine", lambda: _StubEngine(transcript)
    )


class TestAutomaticLookup:
    def test_an_empty_transcript_is_still_searched_then_asked_about(
        self, context, monkeypatch
    ):
        # The regular procedure runs even when nothing was sung: the title can
        # still be searched. Only after the lookup fails is the user asked,
        # with a hint that the song is probably instrumental.
        _install_engine(
            monkeypatch, Transcript(language="en", segments=[], vocals_detected=False)
        )
        searched: list = []
        monkeypatch.setattr(
            providers, "fetch_lrclib", lambda *a, **k: searched.append(a) or None
        )

        with pytest.raises(NeedsUserInput) as caught:
            stage2_lyrics.run(context)

        assert searched, "the online lookup must still run"
        assert caught.value.options == ["ai", "manual", "instrumental"]
        assert "may be instrumental" in str(caught.value)
        assert context.project.read_optional(LyricsDoc) is None

    def test_vad_silence_with_a_failed_lookup_asks_with_the_hint(
        self, context, monkeypatch
    ):
        # The no-VAD fallback decoded pure music into words. The search still
        # runs on them - the procedure does not change - but when it finds
        # nothing, the question says the song is probably instrumental.
        _install_engine(
            monkeypatch,
            Transcript(
                language="en",
                segments=[_sung("thank you"), _sung("thank you")],
                vocals_detected=False,
            ),
        )

        with pytest.raises(NeedsUserInput) as caught:
            stage2_lyrics.run(context)
        assert caught.value.options == ["ai", "manual", "instrumental"]
        assert "may be instrumental" in str(caught.value)

    def test_a_title_match_with_nothing_sung_is_not_attached(self, context, monkeypatch):
        # An instrumental cover of a known song: the title finds the original's
        # lyrics, but with nothing sung there is no overlap to justify them.
        # The user is asked instead of the track coming back wearing words.
        _install_engine(
            monkeypatch, Transcript(language="en", segments=[], vocals_detected=False)
        )
        original = providers.LyricsMatch(
            title="Prelude",
            artist="Someone Else",
            lyrics="all the original words that nobody is singing here",
            source=LyricsSource.LRCLIB,
        )
        monkeypatch.setattr(providers, "fetch_lrclib", lambda *a, **k: original)

        with pytest.raises(NeedsUserInput):
            stage2_lyrics.run(context)
        assert context.project.read_optional(LyricsDoc) is None

    def test_a_sung_song_with_no_lyrics_found_still_asks(self, context, monkeypatch):
        # Real vocals with no published lyrics is a decision, not a verdict -
        # but marking the song instrumental must be one of the offered answers.
        _install_engine(
            monkeypatch,
            Transcript(
                language="en",
                segments=[_sung("obscure sung words")],
                vocals_detected=True,
            ),
        )

        with pytest.raises(NeedsUserInput) as caught:
            stage2_lyrics.run(context)
        assert caught.value.options == ["ai", "manual", "instrumental"]
        assert "may be instrumental" not in str(caught.value)


class TestExplicitChoice:
    def test_the_instrumental_choice_skips_transcription(self, context, monkeypatch):
        monkeypatch.setattr(stage2_lyrics, "WhisperEngine", _ExplodingEngine)
        context.params["choice"] = "instrumental"

        stage2_lyrics.run(context)

        document = context.project.read_optional(LyricsDoc)
        assert document.source == LyricsSource.INSTRUMENTAL
        assert document.lines == []

    def test_the_instrumental_choice_keeps_a_requested_language(self, context, monkeypatch):
        monkeypatch.setattr(stage2_lyrics, "WhisperEngine", _ExplodingEngine)
        context.params.update(choice="instrumental", language="he")

        stage2_lyrics.run(context)

        assert context.project.read_optional(LyricsDoc).language == "he"

    def test_the_instrumental_choice_keeps_the_cached_language(self, context, monkeypatch):
        monkeypatch.setattr(stage2_lyrics, "WhisperEngine", _ExplodingEngine)
        stage2_lyrics._save_transcript(context, "he", [], vocals_detected=False)
        context.params["choice"] = "instrumental"

        stage2_lyrics.run(context)

        assert context.project.read_optional(LyricsDoc).language == "he"

    def test_a_paste_of_only_section_tags_asks_again(self, context, monkeypatch):
        # "[Chorus]" alone passes every emptiness guard yet parses to zero
        # lyric lines. The user just said the song HAS lyrics - quietly
        # marking it instrumental would overrule them.
        _install_engine(
            monkeypatch,
            Transcript(
                language="en",
                segments=[_sung("real singing here")],
                vocals_detected=True,
            ),
        )
        context.params.update(choice="manual", lyrics="[Chorus]\n\n[Verse 1]")

        with pytest.raises(NeedsUserInput) as caught:
            stage2_lyrics.run(context)
        assert caught.value.options == ["ai", "manual", "instrumental"]
        assert context.project.read_optional(LyricsDoc) is None

    def test_use_transcript_on_an_empty_transcript_is_not_an_error(self, context, monkeypatch):
        # "ai" used to be the only click that needed no typing, and on an
        # instrumental it died with "no lyrics could be produced".
        _install_engine(
            monkeypatch, Transcript(language="en", segments=[], vocals_detected=False)
        )
        context.params["choice"] = "ai"

        stage2_lyrics.run(context)

        document = context.project.read_optional(LyricsDoc)
        assert document.source == LyricsSource.INSTRUMENTAL
        assert document.lines == []


class TestTranscriptCache:
    def test_the_vad_verdict_survives_the_cache(self, context):
        stage2_lyrics._save_transcript(context, "en", [], vocals_detected=False)
        assert stage2_lyrics._load_transcript(context) == ("en", [], False)

    def test_a_cache_from_before_the_flag_never_claims_silence(self, context):
        # An old cache saying nothing about vocals must not be what declares a
        # song instrumental.
        from vidichord.lyrics import whisper_engine

        path = context.project.root / stage2_lyrics.TRANSCRIPT_FILENAME
        path.write_text(
            json.dumps(
                {
                    "detector": whisper_engine.DETECTION_MODEL,
                    "language": "en",
                    "segments": [],
                }
            ),
            encoding="utf-8",
        )
        assert stage2_lyrics._load_transcript(context) == ("en", [], True)


class TestSheet:
    def test_an_instrumental_song_renders_as_bar_charts(self):
        lyrics = LyricsDoc(source=LyricsSource.INSTRUMENTAL, title="Prelude")
        chords = ChordsDoc(
            bpm=120.0,
            bars=[
                Bar(
                    index=i,
                    start=i * 2.0,
                    end=(i + 1) * 2.0,
                    beats=[
                        Beat(
                            index=i,
                            beat_in_bar=1,
                            start=i * 2.0,
                            end=(i + 1) * 2.0,
                            chord="C" if i % 2 == 0 else "G",
                        )
                    ],
                )
                for i in range(4)
            ],
        )

        sheet = stage4_sheet.build(lyrics, chords)

        assert sheet.blocks, "an instrumental song must still produce a sheet"
        assert all(block.type == "instrumental" for block in sheet.blocks)
        covered = {index for block in sheet.blocks for index in block.bar_indices}
        assert covered == {0, 1, 2, 3}
