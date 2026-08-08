"""Recover the verse/chorus structure of a set of lyrics.

Two sources of structure, in order of preference:

1. **Explicit headers.** Genius pages already mark ``[Verse 1]``, ``[Chorus]``,
   ``[Bridge]`` and so on. Those are parsed into a section list rather than
   being left inline in the text, so nothing downstream has to re-derive them
   with regexes.

2. **Inferred from repetition.** LRClib plain lyrics and pasted lyrics have
   blank-line stanzas but no names. Stanzas are clustered by *normalised*
   similarity - not exact string equality, which is what the old
   ``inject_structure_tags`` used and which split a single Hebrew chorus in two
   whenever a word was spelled ktiv haser one time and ktiv male the next.

Sections are line ranges over the cleaned lyric lines, never markers embedded
in the text.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ..models import Section, SectionKind
from .normalize import normalize_line

#: A line consisting solely of a bracketed tag, e.g. "[Chorus]".
#: The negative lookahead keeps LRC timestamps such as "[00:12.34]" out.
_HEADER = re.compile(r"^\[(?!\d)([^\]]+)\]$")

#: Genius writes "[Verse 1: Some Artist]" - everything after the colon is noise.
_HEADER_ANNOTATION = re.compile(r"\s*:.*$")

#: Trailing "1", "2" ... on a header, so "Verse 1" and "Verse 2" cluster alike.
_HEADER_NUMBER = re.compile(r"\s*\d+\s*$")

_KIND_KEYWORDS: tuple[tuple[str, SectionKind], ...] = (
    ("pre-chorus", SectionKind.PRE_CHORUS),
    ("pre chorus", SectionKind.PRE_CHORUS),
    ("prechorus", SectionKind.PRE_CHORUS),
    ("chorus", SectionKind.CHORUS),
    ("refrain", SectionKind.CHORUS),
    ("hook", SectionKind.CHORUS),
    ("verse", SectionKind.VERSE),
    ("bridge", SectionKind.BRIDGE),
    ("intro", SectionKind.INTRO),
    ("outro", SectionKind.OUTRO),
    ("ending", SectionKind.OUTRO),
    ("coda", SectionKind.OUTRO),
    ("solo", SectionKind.SOLO),
    ("instrumental", SectionKind.SOLO),
    ("interlude", SectionKind.INTERLUDE),
)

#: Similarity at or above which two stanzas are considered the same stanza.
_STANZA_SIMILARITY = 0.80

#: A "stanza" longer than this means the source had unreliable blank lines, so
#: structure is better recovered from repeated lines than from stanza breaks.
_MAX_TRUSTED_STANZA = 8


def classify(name: str) -> SectionKind:
    """Map a section name onto its structural kind."""
    lowered = name.strip().lower()
    for keyword, kind in _KIND_KEYWORDS:
        if keyword in lowered:
            return kind
    return SectionKind.OTHER


def _clean_header(raw: str) -> str:
    return _HEADER_ANNOTATION.sub("", raw).strip() or "Section"


@dataclass
class _Stanza:
    """A run of consecutive non-blank lines."""

    lines: list[str] = field(default_factory=list)
    #: Inclusive range over the cleaned line list.
    start: int = 0
    end: int = 0
    #: Index of the similarity cluster this stanza belongs to.
    cluster: int = -1

    @property
    def key(self) -> tuple[str, ...]:
        """Normalised words, compared as a sequence.

        Comparing words rather than characters matters: two different verses
        often share their skeleton ("I was ... and you were ..."), which scores
        as near-identical at the character level but clearly differs word by
        word.
        """
        return tuple(normalize_line(" ".join(self.lines)).split())


def _split_stanzas(text: str) -> tuple[list[str], list[_Stanza]]:
    """Split raw lyrics into cleaned lines plus blank-line-delimited stanzas."""
    lines: list[str] = []
    stanzas: list[_Stanza] = []
    current: _Stanza | None = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            current = None
            continue
        if _HEADER.match(stripped):
            # A header also terminates the previous stanza.
            current = None
            continue

        if current is None:
            current = _Stanza(start=len(lines), end=len(lines))
            stanzas.append(current)
        current.lines.append(stripped)
        current.end = len(lines)
        lines.append(stripped)

    return lines, stanzas


def _cluster(stanzas: list[_Stanza]) -> int:
    """Group similar stanzas, writing cluster ids back onto them.

    Returns the number of clusters found.
    """
    representatives: list[tuple[str, ...]] = []
    for stanza in stanzas:
        key = stanza.key
        for index, representative in enumerate(representatives):
            if key == representative or (
                difflib.SequenceMatcher(None, key, representative).ratio()
                >= _STANZA_SIMILARITY
            ):
                stanza.cluster = index
                break
        else:
            stanza.cluster = len(representatives)
            representatives.append(key)
    return len(representatives)


def parse_headers(text: str) -> tuple[list[str], list[Section]]:
    """Parse lyrics that already carry explicit ``[Section]`` headers."""
    lines: list[str] = []
    sections: list[Section] = []
    pending_name: str | None = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue

        header = _HEADER.match(stripped)
        if header:
            pending_name = _clean_header(header.group(1))
            continue

        if pending_name is not None:
            sections.append(
                Section(
                    index=len(sections),
                    name=pending_name,
                    kind=classify(pending_name),
                    line_start=len(lines),
                    line_end=len(lines),
                )
            )
            pending_name = None
        if sections:
            sections[-1].line_end = len(lines)
        lines.append(stripped)

    return lines, sections


def infer_sections(stanzas: list[_Stanza]) -> list[Section]:
    """Name stanzas by how often they repeat.

    The most-repeated stanza is the chorus. Other repeated stanzas are a
    pre-chorus when they consistently lead into it, otherwise a refrain. Unique
    stanzas are numbered verses, except the one following the second chorus,
    which is treated as the bridge - the usual pop arrangement.
    """
    if not stanzas:
        return []

    cluster_count = _cluster(stanzas)
    occurrences: dict[int, list[int]] = {index: [] for index in range(cluster_count)}
    for position, stanza in enumerate(stanzas):
        occurrences[stanza.cluster].append(position)

    repeated = {c: pos for c, pos in occurrences.items() if len(pos) > 1}

    chorus_cluster = -1
    if repeated:
        # Most repetitions wins, then the longer stanza. A late first
        # appearance breaks any remaining tie, because songs open on a verse
        # far more often than on a chorus.
        chorus_cluster = max(
            repeated,
            key=lambda c: (
                len(repeated[c]),
                len(stanzas[repeated[c][0]].lines),
                repeated[c][0],
            ),
        )

    chorus_positions = set(occurrences.get(chorus_cluster, []))

    # A repeated non-chorus stanza that always immediately precedes a chorus is
    # a pre-chorus.
    pre_chorus_clusters = {
        cluster
        for cluster, positions in repeated.items()
        if cluster != chorus_cluster
        and all(position + 1 in chorus_positions for position in positions)
    }

    # The bridge, if any: the first unique stanza after the second chorus.
    bridge_position = -1
    if len(chorus_positions) >= 2:
        second_chorus = sorted(chorus_positions)[1]
        for position in range(second_chorus + 1, len(stanzas)):
            if len(occurrences[stanzas[position].cluster]) == 1:
                bridge_position = position
                break

    sections: list[Section] = []
    verse_number = 1
    for position, stanza in enumerate(stanzas):
        if stanza.cluster == chorus_cluster:
            name, kind = "Chorus", SectionKind.CHORUS
        elif stanza.cluster in pre_chorus_clusters:
            name, kind = "Pre-Chorus", SectionKind.PRE_CHORUS
        elif position == bridge_position:
            name, kind = "Bridge", SectionKind.BRIDGE
        elif len(occurrences[stanza.cluster]) > 1:
            name, kind = "Refrain", SectionKind.CHORUS
        else:
            name, kind = f"Verse {verse_number}", SectionKind.VERSE
            verse_number += 1

        sections.append(
            Section(
                index=len(sections),
                name=name,
                kind=kind,
                line_start=stanza.start,
                line_end=stanza.end,
            )
        )
    return sections


def _longest_repeated_block(keys: list[str]) -> tuple[int, int, int]:
    """Find the longest run of lines that occurs at least twice.

    Returns ``(first_start, second_start, length)``, or a zero length when no
    run repeats. Comparing every pair of start positions is O(n^2) in lines,
    which is nothing at song scale.
    """
    total = len(keys)
    best = (0, 0, 0)
    for first in range(total):
        for second in range(first + 1, total):
            length = 0
            # Stop before the two occurrences would overlap.
            limit = min(second - first, total - second)
            while length < limit and keys[first + length] == keys[second + length]:
                length += 1
            if length > best[2]:
                best = (first, second, length)
    return best


def _all_occurrences(keys: list[str], block: list[str]) -> list[int]:
    """Every non-overlapping start position of ``block`` within ``keys``."""
    size = len(block)
    found: list[int] = []
    position = 0
    while position + size <= len(keys):
        if keys[position:position + size] == block:
            found.append(position)
            position += size
        else:
            position += 1
    return found


def infer_sections_by_repetition(lines: list[str]) -> list[Section]:
    """Find the chorus from repeated *lines* rather than repeated stanzas.

    Published lyrics are often supplied with erratic blank lines - or none at
    all - which leaves stanza clustering with one huge block and no chorus.
    Looking for the longest repeated run of lines recovers the structure
    regardless of how the source was formatted.
    """
    if len(lines) < 4:
        return []

    keys = [normalize_line(line) for line in lines]
    first, second, length = _longest_repeated_block(keys)
    #: A single repeated line is a refrain within a verse, not a section.
    if length < 2:
        return []

    block = keys[first:first + length]
    starts = _all_occurrences(keys, block)
    if len(starts) < 2:
        return []

    chorus_spans = {(start, start + length - 1) for start in starts}

    sections: list[Section] = []
    verse_number = 1
    position = 0
    while position < len(lines):
        span = next((s for s in sorted(chorus_spans) if s[0] == position), None)
        if span is not None:
            sections.append(
                Section(
                    index=len(sections),
                    name="Chorus",
                    kind=SectionKind.CHORUS,
                    line_start=span[0],
                    line_end=span[1],
                )
            )
            position = span[1] + 1
            continue

        # Run forward until the next chorus starts.
        next_start = min(
            (s[0] for s in chorus_spans if s[0] > position), default=len(lines)
        )
        sections.append(
            Section(
                index=len(sections),
                name=f"Verse {verse_number}",
                kind=SectionKind.VERSE,
                line_start=position,
                line_end=next_start - 1,
            )
        )
        verse_number += 1
        position = next_start

    return sections


def infer_sections_from_lines(lines: list[str], stanza_size: int = 4) -> list[Section]:
    """Infer structure for lyrics that have no stanza breaks at all.

    A raw Whisper transcript is one long run of phrases, so it is chunked into
    fixed-size stanzas before clustering. That is crude, but it still finds a
    chorus that the singer repeated, which is the part worth labelling.
    """
    if not lines:
        return []

    by_repetition = infer_sections_by_repetition(lines)
    if by_repetition:
        return by_repetition

    stanzas: list[_Stanza] = []
    for start in range(0, len(lines), stanza_size):
        chunk = lines[start:start + stanza_size]
        stanzas.append(
            _Stanza(lines=list(chunk), start=start, end=start + len(chunk) - 1)
        )
    return infer_sections(stanzas)


def analyse(text: str) -> tuple[list[str], list[Section]]:
    """Split lyrics into clean lines and the sections spanning them.

    Uses explicit headers when the text has them, otherwise infers structure
    from stanza repetition.
    """
    if not text or not text.strip():
        return [], []

    has_headers = any(_HEADER.match(line.strip()) for line in text.splitlines())
    if has_headers:
        lines, sections = parse_headers(text)
        if sections:
            return lines, sections
        # Bracketed lines existed but yielded nothing usable; fall through.

    lines, stanzas = _split_stanzas(text)
    sections = infer_sections(stanzas)

    # Stanza clustering only works when the source has reliable blank lines.
    # If it found no chorus, or lumped the song into one or two huge blocks,
    # look for repeated runs of lines instead.
    needs_retry = not any(
        section.kind is SectionKind.CHORUS for section in sections
    ) or any(
        section.line_end - section.line_start + 1 > _MAX_TRUSTED_STANZA
        for section in sections
    )
    if needs_retry:
        by_repetition = infer_sections_by_repetition(lines)
        if by_repetition:
            return lines, by_repetition

    return lines, sections


def render(lines: list[str], sections: list[Section]) -> str:
    """Render lines and sections back to tagged text, for display or export."""
    starts = {section.line_start: section.name for section in sections}
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in starts:
            if out:
                out.append("")
            out.append(f"[{starts[index]}]")
        out.append(line)
    return "\n".join(out)
