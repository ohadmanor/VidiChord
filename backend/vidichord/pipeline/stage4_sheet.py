"""Stage 4 - assemble the song sheet.

Walks the song in playing order, emitting one block per thing the reader sees:
a section heading, a bar chart for an instrumental passage, or a lyric line
with its chord row.

This used to live in the Angular component, where it could not be tested
alongside the three stages it depends on. The chord-placement algorithm is
carried over unchanged - it was the good part - and now sits in
:mod:`vidichord.sheet.layout` with tests around it.
"""

from __future__ import annotations

from ..models import (
    Bar,
    ChordsDoc,
    InstrumentalBlock,
    LyricBlock,
    LyricsDoc,
    NO_CHORD,
    SectionBlock,
    SheetDoc,
)
from ..sheet import bars as bars_mod
from ..sheet import export, instrumental
from ..sheet.layout import TimedChord, render_chord_row
from . import StageContext


def _chords_in(bars: list[Bar], start: float, end: float) -> list[TimedChord]:
    """Chord changes sounding between two times, in order."""
    found: list[TimedChord] = []
    previous = None
    for bar in bars:
        if bar.end <= start or bar.start >= end:
            continue
        for beat in bar.beats:
            if beat.chord == NO_CHORD or beat.end <= start or beat.start >= end:
                continue
            if beat.chord != previous:
                found.append(TimedChord(chord=beat.chord, time=beat.start))
                previous = beat.chord
    return found


def build(lyrics: LyricsDoc, chords: ChordsDoc) -> SheetDoc:
    """Interleave lyrics and chords into an ordered list of sheet blocks."""
    sheet = SheetDoc(
        title=lyrics.title,
        artist=lyrics.artist,
        key=chords.key,
        bpm=chords.bpm,
        is_rtl=lyrics.is_rtl,
    )

    runs = instrumental.detect(chords.bars, lyrics)
    # Index instrumental runs by the bar they start on, so they can be emitted
    # at the right point while walking the song.
    runs_by_start = {run.bar_positions[0]: run for run in runs}
    instrumental_bars = {position for run in runs for position in run.bar_positions}

    def emit_instrumental(run: instrumental.InstrumentalRun) -> None:
        selected = [chords.bars[position] for position in run.bar_positions]
        sheet.blocks.append(
            SectionBlock(
                name=run.kind.value.replace("_", " ").title(),
                kind=run.kind,
                start=run.start,
            )
        )
        sheet.blocks.append(
            InstrumentalBlock(
                kind=run.kind,
                text=bars_mod.render(selected),
                start=run.start,
                end=run.end,
                bar_indices=[bar.index for bar in selected],
            )
        )

    emitted_runs: set[int] = set()
    open_section: int | None = None

    for bar_position, bar in enumerate(chords.bars):
        run = runs_by_start.get(bar_position)
        if run is not None:
            emit_instrumental(run)
            emitted_runs.add(bar_position)
            # A heading was just written, so the next vocal section re-announces.
            open_section = None
        if bar_position in instrumental_bars:
            continue

        # Emit every lyric line that starts inside this bar.
        for line in lyrics.lines:
            if not (bar.start <= line.time < bar.end):
                continue

            if line.section_index is not None and line.section_index != open_section:
                section = lyrics.sections[line.section_index]
                sheet.blocks.append(
                    SectionBlock(name=section.name, kind=section.kind, start=line.time)
                )
                open_section = line.section_index

            sheet.blocks.append(
                LyricBlock(
                    chord_line=render_chord_row(
                        text=line.text,
                        words=line.words,
                        chords=_chords_in(chords.bars, line.time, line.end),
                        start=line.time,
                        end=line.end,
                        rtl=lyrics.is_rtl,
                    ),
                    text=line.text,
                    start=line.time,
                    end=line.end,
                    line_index=line.index,
                )
            )

    # Lines that fell outside every bar (possible when chords are missing).
    placed = {
        block.line_index for block in sheet.blocks if isinstance(block, LyricBlock)
    }
    for line in lyrics.lines:
        if line.index in placed:
            continue
        sheet.blocks.append(
            LyricBlock(
                chord_line="",
                text=line.text,
                start=line.time,
                end=line.end,
                line_index=line.index,
            )
        )

    return sheet


def run(context: StageContext) -> None:
    project = context.project

    lyrics = project.read_optional(LyricsDoc)
    if lyrics is None:
        raise RuntimeError("Stage 2 must run before the sheet can be built")

    chords = project.read_optional(ChordsDoc)
    if chords is None:
        # A lyrics-only sheet is still worth producing.
        context.report("No chords yet - building a lyrics-only sheet.", 40.0)
        chords = ChordsDoc()

    context.report("Laying out the sheet...", 60.0)
    sheet = build(lyrics, chords)
    project.write(sheet)

    context.report("Rendering text...", 90.0)
    project.write_sheet_text(export.render_text(sheet))

    lyric_blocks = sum(1 for block in sheet.blocks if isinstance(block, LyricBlock))
    instrumental_blocks = sum(
        1 for block in sheet.blocks if isinstance(block, InstrumentalBlock)
    )
    context.report(
        f"{lyric_blocks} lyric lines, {instrumental_blocks} instrumental passages.",
        100.0,
    )
