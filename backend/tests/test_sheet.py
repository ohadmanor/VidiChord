"""Sheet assembly: chord placement, instrumental detection and export.

Two user requirements live here: chords must sit over the right syllables in
both text directions, and stretches with no singing must be written as bar
charts rather than as chords floating over nothing.
"""

import pytest

from vidichord.models import (
    Bar,
    Beat,
    InstrumentalBlock,
    LyricBlock,
    LyricLine,
    LyricsDoc,
    NO_CHORD,
    Section,
    SectionBlock,
    SectionKind,
    Word,
)
from vidichord.pipeline.stage4_sheet import build
from vidichord.sheet import bars as bars_mod
from vidichord.sheet import export, instrumental
from vidichord.sheet.layout import TimedChord, render_chord_row


def make_bar(index: int, start: float, chords: list[str], beat_length: float = 0.5) -> Bar:
    beats = [
        Beat(
            index=index * len(chords) + position,
            beat_in_bar=position + 1,
            start=start + position * beat_length,
            end=start + (position + 1) * beat_length,
            chord=chord,
        )
        for position, chord in enumerate(chords)
    ]
    return Bar(index=index, start=beats[0].start, end=beats[-1].end, beats=beats)


class TestBarNotation:
    def test_renders_one_group_per_bar(self):
        bars = [
            make_bar(1, 0.0, ["C", "C", "C", "C"]),
            make_bar(2, 2.0, ["G", "G", "Am", "Am"]),
        ]
        assert bars_mod.render(bars) == "// C / G Am //"

    def test_empty_bar_renders_as_a_dash(self):
        bars = [make_bar(1, 0.0, [NO_CHORD] * 4)]
        assert bars_mod.render(bars) == "// - //"

    def test_collapses_repeats_within_a_bar(self):
        bars = [make_bar(1, 0.0, ["C", "C", "G", "G"])]
        assert bars_mod.render(bars) == "// C G //"

    def test_wraps_long_passages(self):
        bars = [make_bar(i, i * 2.0, ["C"] * 4) for i in range(1, 11)]
        rendered = bars_mod.render(bars, max_bars_per_line=8)
        assert len(rendered.splitlines()) == 2

    def test_no_bars(self):
        assert bars_mod.render([]) == ""


class TestInstrumentalDetection:
    def build_lyrics(self, spans: list[tuple[float, float]]) -> LyricsDoc:
        lines = [
            LyricLine(
                index=i,
                time=start,
                end=end,
                text=f"line {i}",
                words=[Word(text=f"line{i}", start=start, end=end, offset=0)],
            )
            for i, (start, end) in enumerate(spans)
        ]
        return LyricsDoc(lines=lines)

    def test_finds_an_intro_before_the_first_vocal(self):
        bars = [make_bar(i, (i - 1) * 2.0, ["C"] * 4) for i in range(1, 9)]
        # Singing starts at 8 s, which is bar 5.
        lyrics = self.build_lyrics([(8.0, 10.0), (10.0, 12.0), (12.0, 14.0), (14.0, 16.0)])

        runs = instrumental.detect(bars, lyrics)
        assert runs, "expected an intro to be detected"
        assert runs[0].kind is SectionKind.INTRO
        assert runs[0].bar_positions[0] == 0

    def test_finds_an_outro_after_the_last_vocal(self):
        bars = [make_bar(i, (i - 1) * 2.0, ["C"] * 4) for i in range(1, 9)]
        lyrics = self.build_lyrics([(0.0, 2.0), (2.0, 4.0)])

        runs = instrumental.detect(bars, lyrics)
        assert any(run.kind is SectionKind.OUTRO for run in runs)

    def test_long_interior_gap_is_a_solo(self):
        bars = [make_bar(i, (i - 1) * 2.0, ["C"] * 4) for i in range(1, 21)]
        # Sing at the start and again at the end, leaving 16 bars in between.
        lyrics = self.build_lyrics([(0.0, 2.0), (36.0, 38.0)])

        runs = instrumental.detect(bars, lyrics)
        interior = [
            run for run in runs
            if run.kind in (SectionKind.SOLO, SectionKind.INTERLUDE)
        ]
        assert interior and interior[0].kind is SectionKind.SOLO

    def test_short_interior_gap_is_an_interlude(self):
        bars = [make_bar(i, (i - 1) * 2.0, ["C"] * 4) for i in range(1, 9)]
        lyrics = self.build_lyrics([(0.0, 2.0), (10.0, 12.0)])

        runs = instrumental.detect(bars, lyrics)
        interior = [run for run in runs if run.kind is SectionKind.INTERLUDE]
        assert interior

    def test_breathing_room_between_lines_is_not_instrumental(self):
        """A one-bar gap between phrases must not become a solo.

        The previous implementation marked any bar without a lyric line, which
        turned every breath into a spurious instrumental passage.
        """
        bars = [make_bar(i, (i - 1) * 2.0, ["C"] * 4) for i in range(1, 9)]
        lyrics = self.build_lyrics(
            [(0.0, 1.5), (4.0, 5.5), (8.0, 9.5), (12.0, 13.5)]
        )
        runs = instrumental.detect(bars, lyrics, min_bars=2)
        interior = [
            run for run in runs
            if run.kind in (SectionKind.SOLO, SectionKind.INTERLUDE)
        ]
        assert interior == []

    def test_no_bars_gives_no_runs(self):
        assert instrumental.detect([], LyricsDoc()) == []


class TestChordLayout:
    def line_words(self, text: str, timings: list[tuple[str, float, float]]) -> list[Word]:
        words = []
        cursor = 0
        for token, start, end in timings:
            offset = text.find(token, cursor)
            cursor = offset + len(token)
            words.append(Word(text=token, start=start, end=end, offset=offset))
        return words

    def test_places_chords_over_the_right_words(self):
        text = "Hello darkness my old friend"
        words = self.line_words(
            text,
            [("Hello", 0.0, 0.4), ("darkness", 0.5, 1.0),
             ("my", 1.2, 1.4), ("old", 1.6, 1.8), ("friend", 2.0, 2.5)],
        )
        row = render_chord_row(
            text, words,
            [TimedChord("C", 0.0), TimedChord("Am", 1.2), TimedChord("G", 2.0)],
            start=0.0, end=3.0, rtl=False,
        )
        assert row.startswith("C")
        assert row.index("Am") == text.index("my")
        assert row.index("G") == text.index("friend")

    def test_chords_never_run_together(self):
        text = "ab"
        words = [Word(text="ab", start=0.0, end=1.0, offset=0)]
        row = render_chord_row(
            text, words,
            [TimedChord("Cmaj7", 0.0), TimedChord("Am7", 0.05)],
            start=0.0, end=1.0, rtl=False,
        )
        assert "Cmaj7 Am7" in row or "Cmaj7  Am7" in row
        assert "Cmaj7Am7" not in row

    def test_rtl_mirrors_the_columns(self):
        """Hebrew rows are built so they read correctly once reversed.

        The lyric row is reversed by the browser at dir="rtl"; the chord row is
        not, so its columns are measured from the opposite edge.
        """
        text = "שלום לך ידידי הטוב"
        words = self.line_words(
            text,
            [("שלום", 0.0, 0.4), ("לך", 0.6, 1.0),
             ("ידידי", 1.0, 1.4), ("הטוב", 1.8, 2.2)],
        )
        row = render_chord_row(
            text, words,
            [TimedChord("Am", 0.0), TimedChord("Dm", 1.0), TimedChord("E7", 1.8)],
            start=0.0, end=2.5, rtl=True,
        )
        width = len(text)
        # Am belongs over the first word, which sits at the right-hand edge.
        assert row.index("Am") == width - len("Am")
        assert row.index("E7") == width - text.index("הטוב") - len("E7")
        assert row.index("Dm") == width - text.index("ידידי") - len("Dm")

    def test_no_chords_gives_an_empty_row(self):
        assert render_chord_row("anything", [], [], start=0.0, end=1.0) == ""

    def test_a_line_with_no_word_timings_still_places_chords(self):
        row = render_chord_row(
            "some words here", [], [TimedChord("C", 0.0), TimedChord("G", 2.0)],
            start=0.0, end=4.0, rtl=False,
        )
        assert "C" in row and "G" in row


class TestSheetBuild:
    def test_interleaves_sections_lyrics_and_instrumentals(self):
        bars = [make_bar(i, (i - 1) * 2.0, ["C"] * 4) for i in range(1, 13)]
        lyrics = LyricsDoc(
            title="Test", artist="Someone", language="en",
            sections=[Section(index=0, name="Verse 1", kind=SectionKind.VERSE,
                              line_start=0, line_end=1)],
            lines=[
                LyricLine(index=0, section_index=0, time=8.0, end=10.0, text="first line",
                          words=[Word(text="first", start=8.0, end=8.5, offset=0)]),
                LyricLine(index=1, section_index=0, time=10.0, end=12.0, text="second line",
                          words=[Word(text="second", start=10.0, end=10.5, offset=0)]),
            ],
        )
        from vidichord.models import ChordsDoc

        sheet = build(lyrics, ChordsDoc(bpm=120, key="C major", bars=bars))

        assert sheet.title == "Test"
        assert not sheet.is_rtl
        assert any(isinstance(b, InstrumentalBlock) for b in sheet.blocks)
        assert any(isinstance(b, SectionBlock) and b.name == "Verse 1" for b in sheet.blocks)
        lyric_blocks = [b for b in sheet.blocks if isinstance(b, LyricBlock)]
        assert [b.text for b in lyric_blocks] == ["first line", "second line"]

    def test_every_lyric_line_appears_even_without_chords(self):
        from vidichord.models import ChordsDoc

        lyrics = LyricsDoc(
            lines=[
                LyricLine(index=i, time=i * 2.0, end=i * 2.0 + 2.0, text=f"line {i}")
                for i in range(4)
            ]
        )
        sheet = build(lyrics, ChordsDoc())
        lyric_blocks = [b for b in sheet.blocks if isinstance(b, LyricBlock)]
        assert len(lyric_blocks) == 4

    def test_hebrew_lyrics_mark_the_sheet_rtl(self):
        from vidichord.models import ChordsDoc

        lyrics = LyricsDoc(language="he", lines=[
            LyricLine(index=0, time=0.0, end=2.0, text="שלום")
        ])
        assert build(lyrics, ChordsDoc()).is_rtl


class TestExport:
    def test_renders_songbook_text(self):
        from vidichord.models import SheetDoc

        sheet = SheetDoc(
            title="Song", artist="Artist",
            blocks=[
                SectionBlock(name="Intro", kind=SectionKind.INTRO),
                InstrumentalBlock(kind=SectionKind.INTRO, text="// C / G //",
                                  start=0.0, end=4.0),
                SectionBlock(name="Verse 1", kind=SectionKind.VERSE),
                LyricBlock(chord_line="C    G", text="hello there",
                           start=4.0, end=6.0, line_index=0),
            ],
        )
        text = export.render_text(sheet)
        assert "[Intro]" in text
        assert "// C / G //" in text
        assert "[Verse 1]" in text
        # The chord row must come immediately before its lyric line.
        lines = text.splitlines()
        assert lines[lines.index("hello there") - 1] == "C    G"

    def test_blank_chord_rows_are_dropped(self):
        from vidichord.models import SheetDoc

        sheet = SheetDoc(blocks=[
            LyricBlock(chord_line="   ", text="no chords here",
                       start=0.0, end=1.0, line_index=0)
        ])
        assert export.render_text(sheet).strip() == "no chords here"

    @pytest.mark.parametrize(
        "title,artist,expected",
        [
            ("Song", "Artist", "Song - Artist.json"),
            ("Song: A/B", "Art*ist", "Song AB - Artist.json"),
            ("Only Title", "", "Only Title.json"),
        ],
    )
    def test_filenames_are_safe(self, title, artist, expected):
        assert export.safe_filename(title, artist) == expected

    def test_payload_shape_matches_songbook(self):
        from vidichord.models import SheetDoc

        sheet = SheetDoc(title="T", artist="A", key="G major", is_rtl=True)
        payload = export.songbook_payload(sheet)
        assert payload["title"] == "T"
        assert payload["isRTL"] is True
        assert payload["modifiedByUser"] is True
        assert "rawText" in payload
