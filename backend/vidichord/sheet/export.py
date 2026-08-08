"""Render a sheet to songbook's plain-text format.

songbook stores a song as one raw text blob plus an ``isRTL`` flag, and renders
it monospace. So the chord rows built by :mod:`.layout` transfer across
unchanged - the alignment is already carried in the spacing.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..models import InstrumentalBlock, LyricBlock, SectionBlock, SheetDoc

#: Characters Windows forbids in a filename.
_ILLEGAL = re.compile(r'[\\/*?:"<>|]')


def render_text(sheet: SheetDoc) -> str:
    """Render the sheet as songbook raw text, preserving section structure."""
    lines: list[str] = []

    for block in sheet.blocks:
        if isinstance(block, SectionBlock):
            if lines:
                lines.append("")
            lines.append(f"[{block.name}]")
        elif isinstance(block, InstrumentalBlock):
            lines.append(block.text)
        elif isinstance(block, LyricBlock):
            if block.chord_line.strip():
                lines.append(block.chord_line)
            lines.append(block.text)

    # Collapse any run of blank lines down to one.
    output: list[str] = []
    for line in lines:
        if not line.strip() and output and not output[-1].strip():
            continue
        output.append(line)

    return "\n".join(output).strip() + "\n"


def safe_filename(title: str, artist: str) -> str:
    """Build a songbook filename from the song's title and artist."""
    clean_title = _ILLEGAL.sub("", title or "").strip()
    clean_artist = _ILLEGAL.sub("", artist or "").strip()

    if clean_title and clean_artist:
        stem = f"{clean_title} - {clean_artist}"
    elif clean_title:
        stem = clean_title
    else:
        stem = f"song_{int(time.time())}"
    return f"{stem}.json"


def songbook_payload(sheet: SheetDoc, raw_text: str | None = None) -> dict[str, Any]:
    """Build the JSON document songbook expects for one song."""
    return {
        "id": f"song_{int(time.time())}",
        "title": sheet.title,
        "artist": sheet.artist,
        "key": sheet.key,
        "isRTL": sheet.is_rtl,
        "rawText": raw_text if raw_text is not None else render_text(sheet),
        "modifiedByUser": True,
    }
