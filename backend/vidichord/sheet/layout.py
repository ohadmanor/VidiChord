"""Place chords above lyrics as a monospace text row.

A chord belongs over the syllable it lands on, so its column is found by
interpolating between the known times of the words either side of it. Word
timings come from stage 2, where the transcript's timings have already been
transferred onto the official lyric words, so a word's character offset is
known exactly rather than guessed by searching the text.

Right-to-left text needs the columns mirrored. The lyric row is rendered by the
browser with ``dir="rtl"``, which reverses it visually, so the chord row - which
must stay ``dir="ltr"`` to preserve its spacing - is built with each chord's
column measured from the opposite edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import Word

#: Columns of padding to allow per second of trailing instrumental time, so
#: chords played after the last word are not all crushed onto the line end.
_COLUMNS_PER_SECOND = 3.0

#: How long a lyric line is assumed to last when nothing follows it.
_DEFAULT_LINE_SECONDS = 5.0


@dataclass(frozen=True)
class TimedChord:
    """A chord change at a point in time."""

    chord: str
    time: float


@dataclass(frozen=True)
class _Anchor:
    """A known correspondence between a time and a column."""

    time: float
    column: int


def _word_offset(text: str, word: Word, cursor: int) -> int:
    """Character offset of a word within a line."""
    if word.offset >= 0:
        return word.offset
    found = text.find(word.text, cursor)
    return found if found >= 0 else cursor


def build_anchors(
    text: str, words: Sequence[Word], start: float, end: float
) -> list[_Anchor]:
    """Map times to columns using the line's word timings.

    Anchors are forced to be strictly increasing in time and non-decreasing in
    column, so the interpolation below is always well defined.
    """
    anchors: list[_Anchor] = [_Anchor(start, 0)]

    cursor = 0
    last_time = start
    for word in words:
        if not word.text:
            continue
        offset = _word_offset(text, word, cursor)
        anchors.append(_Anchor(word.start, offset))
        finish = offset + len(word.text)
        anchors.append(_Anchor(max(word.end, word.start), finish))
        cursor = finish
        last_time = max(word.end, word.start)

    # Reserve columns for any time left over after the final word.
    trailing = max(0.0, end - last_time)
    anchors.append(_Anchor(end, len(text) + int(trailing * _COLUMNS_PER_SECOND)))

    cleaned: list[_Anchor] = [anchors[0]]
    for anchor in anchors[1:]:
        previous = cleaned[-1]
        if anchor.time > previous.time:
            cleaned.append(_Anchor(anchor.time, max(anchor.column, previous.column)))
        elif anchor.column > previous.column:
            # One word's end and the next word's start share a timestamp. Keep
            # the later column: at that instant the singer is on the new word,
            # so a chord landing there belongs over it.
            cleaned[-1] = _Anchor(previous.time, anchor.column)
    return cleaned


def column_for(anchors: Sequence[_Anchor], time: float) -> int:
    """Interpolate the column a given time falls on."""
    if not anchors:
        return 0
    if time <= anchors[0].time:
        return anchors[0].column
    if time >= anchors[-1].time:
        return anchors[-1].column

    for left, right in zip(anchors, anchors[1:]):
        if left.time <= time <= right.time:
            span = right.time - left.time
            if span <= 0:
                return left.column
            ratio = (time - left.time) / span
            return int(left.column + ratio * (right.column - left.column))
    return anchors[-1].column


def render_chord_row(
    text: str,
    words: Sequence[Word],
    chords: Sequence[TimedChord],
    start: float,
    end: float | None = None,
    rtl: bool = False,
) -> str:
    """Build the monospace chord row that sits above ``text``.

    Returns an empty string when no chords fall on this line.
    """
    if not chords:
        return ""

    finish = end if end is not None and end > start else start + _DEFAULT_LINE_SECONDS
    anchors = build_anchors(text, words, start, finish)

    placements = [
        (column_for(anchors, chord.time), chord.chord)
        for chord in sorted(chords, key=lambda c: c.time)
    ]

    # The row may need to run past the end of the lyric text.
    width = max([len(text)] + [column + len(chord) for column, chord in placements])

    if rtl:
        # Mirror columns so the row reads correctly once reversed for display.
        placements = [
            (max(0, width - column - len(chord)), chord) for column, chord in placements
        ]
    placements.sort()

    row: list[str] = []
    length = 0
    for column, chord in placements:
        target = max(column, length)
        if target > length:
            row.append(" " * (target - length))
            length = target
        elif length > 0:
            # Never let two chords run together.
            row.append(" ")
            length += 1
        row.append(chord)
        length += len(chord)

    rendered = "".join(row)
    if rtl and len(rendered) < width:
        # Pad the right so the reversed row lines up with the lyric text.
        rendered += " " * (width - len(rendered))
    return rendered.rstrip() if not rtl else rendered
