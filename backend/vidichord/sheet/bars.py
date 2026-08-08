"""Bar-chart notation for stretches of music with no singing.

An instrumental passage has no words to hang chords over, so it is written as a
compact bar chart instead::

    // C / G / Am F / G //

One group per bar, separated by ``/`` and wrapped in ``//``. A bar with two
chords lists both; a bar with none is written ``-``.
"""

from __future__ import annotations

from typing import Sequence

from ..models import Bar

#: Placeholder for a bar in which nothing is sounding.
EMPTY_BAR = "-"

_OPEN = "//"
_CLOSE = "//"
_SEPARATOR = " / "


def render_bar(bar: Bar) -> str:
    """Render one bar's chords, collapsing consecutive repeats."""
    chords = bar.chords
    return " ".join(chords) if chords else EMPTY_BAR


def render(bars: Sequence[Bar], max_bars_per_line: int = 8) -> str:
    """Render a run of bars as one or more bar-chart lines.

    Long passages are wrapped so a sixteen-bar solo does not become a single
    unreadable line.
    """
    if not bars:
        return ""

    cells = [render_bar(bar) for bar in bars]
    lines: list[str] = []
    for start in range(0, len(cells), max_bars_per_line):
        chunk = cells[start:start + max_bars_per_line]
        lines.append(f"{_OPEN} {_SEPARATOR.join(chunk)} {_CLOSE}")
    return "\n".join(lines)
