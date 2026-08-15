"""Read Chordify print-to-PDF exports as a beat-indexed chord reference.

These are the hand-verified sheets used as ground truth when tuning chord
extraction. The layout is not a table - it is a CSS grid printed to PDF - so
it has to be recovered geometrically:

  * A row is 4 bars laid out as 16 beat slots at x = 21 + 35.85*slot. Songs in
    6/8 use 12 slots at 47.75 instead; which one applies is decided by fitting
    both and seeing where the labels actually land.
  * A slot carries a label only where the chord CHANGES. A blank slot means the
    previous chord is still sounding.
  * A row in which nothing changes emits no text at all. Rows therefore have to
    be addressed by y-position (pitch 34 px, or 45 px in the 6/8 layout) rather
    than by counting what is visible, and non-final pages assumed full - miss
    this and a song with long held chords parses to half its true length.
  * The trailing "chords used" legend sits after a gap far larger than the row
    pitch, at x positions that fit no slot.

Each export's footer records the source video, so the reference is tied to the
exact audio it was made from.

Usage::

    python -m tools.chordify_reference <pdf-dir> [-o reference.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import pdfplumber
except ImportError as exc:                                  # pragma: no cover
    raise SystemExit("pdfplumber is required: pip install pdfplumber") from exc

#: (slots per row, x pitch). 16 slots is 4 bars of 4/4; 12 is the 6/8 layout.
LATTICES = ((16, 35.85), (12, 47.75))
SLOT0 = 21.0
#: How far off its slot a label may sit, in slot widths.
LATTICE_TOL = 0.25
#: A gap this much larger than the row pitch means the legend has started.
LEGEND_GAP = 100.0
#: Rows run from y=78 on page 0 (below the title) to about y=700.
FIRST_ROW = {0: 78.0, 1: 54.0}
PAGE_BOTTOM = 700.0

REST = "\U0001D13D"
_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹ᵐᵃʲˢᵘ⁺ᵈⁱᵒ", "0123456789majsu+dio")


def normalise(label: str | None) -> str | None:
    """Chordify's typographic label -> the spelling the app uses."""
    if label is None:
        return None
    if label == REST:
        return "N"
    return (label.replace("♯", "#").replace("♭", "b").replace("ₘ", "m")
                 .replace("₅", "5").replace("₉", "9").translate(_SUPERSCRIPTS))


def _page_rows(page, pno: int) -> dict[float, list]:
    """Text rows on a page, with the header, footer and legend removed."""
    buckets: dict[float, list] = {}
    for word in page.extract_words():
        if word["text"].startswith("https://") or re.fullmatch(r"\d+/\d+", word["text"]):
            continue
        buckets.setdefault(round(word["top"] / 6) * 6, []).append(word)

    floor = 70 if pno == 0 else 40
    kept: dict[float, list] = {}
    previous = None
    for top in sorted(t for t in buckets if t >= floor):
        if previous is not None and top - previous > LEGEND_GAP:
            break
        kept[top] = buckets[top]
        previous = top
    return kept


def _choose_lattice(pages: list[dict]) -> tuple[int, float]:
    """Pick the slot lattice the labels sit on.

    The 12- and 16-slot lattices coincide on every third/fourth slot, so a song
    that only ever changes chord on a downbeat fits both equally. 4/4 is
    therefore the default, and only labels that genuinely miss it argue for 6/8.
    """
    def score(nslots: int, pitch: float) -> float:
        hits = total = 0
        for rows in pages:
            for words in rows.values():
                for word in words:
                    pos = ((word["x0"] + word["x1"]) / 2 - SLOT0) / pitch
                    total += 1
                    if 0 <= round(pos) < nslots and abs(pos - round(pos)) <= LATTICE_TOL:
                        hits += 1
        return hits / total if total else 0.0

    wide, narrow = LATTICES
    return wide if score(*wide) >= 0.97 or score(*wide) >= score(*narrow) else narrow


def _slot_rows(rows: dict, nslots: int, pitch: float) -> list[tuple[float, dict]]:
    out = []
    for top in sorted(rows):
        slots, fits = {}, True
        for word in rows[top]:
            pos = ((word["x0"] + word["x1"]) / 2 - SLOT0) / pitch
            slot = round(pos)
            if abs(pos - slot) > LATTICE_TOL or not (0 <= slot < nslots):
                fits = False
                break
            slots[slot] = word["text"]
        if fits and slots:
            out.append((top, slots))
    return out


def parse(path: Path) -> dict:
    """Parse one export into ``{video_id, nslots, rows, beats}``."""
    with pdfplumber.open(path) as pdf:
        pages = [_page_rows(page, pno) for pno, page in enumerate(pdf.pages)]
        first_text = pdf.pages[0].extract_text() or ""

    nslots, xpitch = _choose_lattice(pages)
    laid_out = [_slot_rows(rows, nslots, xpitch) for rows in pages]

    # Row pitch is the SMALLEST plausible gap: anything larger is one or more
    # rows in which no chord changed, and so emitted nothing.
    gaps = []
    for rows in laid_out:
        ys = [y for y, _ in rows]
        gaps += [round(b - a) for a, b in zip(ys, ys[1:]) if 20 < b - a < 120]
    pitch = 34 if (min(gaps) if gaps else 34) <= 40 else 45

    with_rows = [i for i, rows in enumerate(laid_out) if rows]
    last = max(with_rows) if with_rows else 0

    grid: list[dict] = []
    for pno, rows in enumerate(laid_out):
        if not rows:
            continue
        first = rows[0][0]
        indexed = {round((y - first) / pitch): slots for y, slots in rows}
        count = (int((PAGE_BOTTOM - first) // pitch) + 1 if pno < last
                 else max(indexed) + 1)
        grid.extend(indexed.get(i, {}) for i in range(count))

    beats, current = [], "N"
    for row in grid:
        for slot in range(nslots):
            label = normalise(row.get(slot))
            if label is not None:
                current = label
            beats.append(current)

    found = re.search(r"youtube:([A-Za-z0-9_-]{11})", first_text)
    return {"video_id": found.group(1) if found else "",
            "nslots": nslots, "rows": len(grid), "beats": beats}


def main() -> None:
    # Hebrew titles are unprintable on a cp1252 console otherwise.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="folder of Chordify PDFs")
    parser.add_argument("-o", "--output", type=Path, default=Path("reference.json"))
    args = parser.parse_args()

    out = {}
    for pdf in sorted(args.directory.glob("*.pdf")):
        name = pdf.stem.replace(" - Chordify", "")
        parsed = parse(pdf)
        out[name] = parsed
        print(f"{name[:52]:52} {parsed['nslots']:2d} slots  "
              f"{parsed['rows']:3d} rows  {len(parsed['beats']):5d} beats  "
              f"{parsed['video_id']}")
    args.output.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {args.output} ({len(out)} songs)")


if __name__ == "__main__":
    main()
