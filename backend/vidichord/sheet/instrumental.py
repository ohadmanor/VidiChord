"""Find the parts of a song where nobody is singing.

Intros, solos, interludes and outros need to be written as bar charts rather
than as chords hovering over absent lyrics, so they have to be identified
first.

Detection uses the *word* timestamps from stage 2 rather than the lyric lines:
a bar counts as sung if any transcribed word overlaps it. The old approach -
"a bar with no lyric line covering it" - marked the gap between every pair of
lines as instrumental, which turned ordinary breathing room into spurious
one-bar solos.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import Bar, LyricsDoc, SectionKind

#: A gap must span at least this many bars to count as instrumental.
MIN_INSTRUMENTAL_BARS = 2

#: An interior passage at least this long is a solo rather than an interlude.
SOLO_BARS = 8


@dataclass
class InstrumentalRun:
    """A maximal run of consecutive bars with no singing."""

    #: Indices into the bar list passed to :func:`detect`.
    bar_positions: list[int]
    kind: SectionKind
    start: float
    end: float

    @property
    def bar_count(self) -> int:
        return len(self.bar_positions)


def vocal_bars(bars: Sequence[Bar], lyrics: LyricsDoc) -> list[bool]:
    """Mark each bar as sung or not, from the transcript's word timings.

    Falls back to line spans for lines that carry no word-level timing.
    """
    spans: list[tuple[float, float]] = []
    for line in lyrics.lines:
        if line.words:
            spans.extend((word.start, word.end) for word in line.words)
        elif line.end > line.time:
            spans.append((line.time, line.end))

    spans.sort()
    marks = [False] * len(bars)
    if not spans:
        return marks

    cursor = 0
    for index, bar in enumerate(bars):
        # Skip spans that finished before this bar started.
        while cursor < len(spans) and spans[cursor][1] < bar.start:
            cursor += 1
        probe = cursor
        while probe < len(spans) and spans[probe][0] < bar.end:
            if spans[probe][1] > bar.start:
                marks[index] = True
                break
            probe += 1
    return marks


def classify_run(
    positions: list[int], total_bars: int, first_vocal: int, last_vocal: int
) -> SectionKind:
    """Name a silent run by where it sits and how long it lasts."""
    start, end = positions[0], positions[-1]

    if first_vocal < 0:
        # Purely instrumental track.
        return SectionKind.SOLO
    if end < first_vocal:
        return SectionKind.INTRO
    if start > last_vocal:
        return SectionKind.OUTRO
    return SectionKind.SOLO if len(positions) >= SOLO_BARS else SectionKind.INTERLUDE


def detect(
    bars: Sequence[Bar],
    lyrics: LyricsDoc,
    min_bars: int = MIN_INSTRUMENTAL_BARS,
) -> list[InstrumentalRun]:
    """Locate every instrumental passage, in playing order."""
    if not bars:
        return []

    sung = vocal_bars(bars, lyrics)
    vocal_positions = [index for index, is_sung in enumerate(sung) if is_sung]
    first_vocal = vocal_positions[0] if vocal_positions else -1
    last_vocal = vocal_positions[-1] if vocal_positions else -1

    runs: list[InstrumentalRun] = []
    current: list[int] = []

    def flush() -> None:
        # An intro or outro is worth showing even if it is a single bar; an
        # interior gap that short is just space between phrases.
        if not current:
            return
        is_edge = current[-1] < first_vocal or current[0] > last_vocal or first_vocal < 0
        if len(current) >= min_bars or is_edge:
            runs.append(
                InstrumentalRun(
                    bar_positions=list(current),
                    kind=classify_run(current, len(bars), first_vocal, last_vocal),
                    start=bars[current[0]].start,
                    end=bars[current[-1]].end,
                )
            )

    for index in range(len(bars)):
        if sung[index]:
            flush()
            current = []
        else:
            current.append(index)
    flush()

    return runs
