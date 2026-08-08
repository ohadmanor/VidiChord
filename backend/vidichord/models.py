"""Schemas for the four pipeline artifacts.

Every stage reads the artifacts produced before it and writes exactly one file,
so these models are the contract between stages. They are also the wire format
for the HTTP API - the frontend renders ``SheetDoc`` directly.

    01_source.json  -> SourceDoc    stage 1, audio acquisition
    02_lyrics.json  -> LyricsDoc    stage 2, transcription + alignment
    03_chords.json  -> ChordsDoc    stage 3, beat/bar-aligned chords
    04_sheet.json   -> SheetDoc     stage 4, the rendered song sheet

All times are floating-point seconds from the start of the audio.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Artifact(BaseModel):
    """Base for every persisted document."""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Stage 1 - source
# ---------------------------------------------------------------------------


class SourceDoc(Artifact):
    kind: Literal["youtube", "local"] = "youtube"
    url: str = ""
    video_id: str = ""
    title: str = ""
    artist: str = ""
    uploader: str = ""
    duration: float = 0.0
    audio_filename: str = "audio.wav"
    downloaded_at: str = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Stage 2 - lyrics
# ---------------------------------------------------------------------------


class SectionKind(str, Enum):
    """Structural role of a block of lyrics.

    ``intro``/``solo``/``outro`` only appear on instrumental blocks in the
    sheet; the lyrics document uses the vocal kinds.
    """

    INTRO = "intro"
    VERSE = "verse"
    PRE_CHORUS = "pre_chorus"
    CHORUS = "chorus"
    BRIDGE = "bridge"
    SOLO = "solo"
    INTERLUDE = "interlude"
    OUTRO = "outro"
    OTHER = "other"


class Word(Artifact):
    """A single word with its timing.

    ``offset`` is the character position of the word within its line, filled in
    for lyric words so the sheet can place chords without re-searching the
    text. It is -1 for raw transcript words, which belong to no final line.
    """

    text: str
    start: float
    end: float
    offset: int = -1


class LyricLine(Artifact):
    index: int
    #: Index into ``LyricsDoc.sections``; None if the line sits outside any.
    section_index: int | None = None
    time: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)


class Section(Artifact):
    """A named run of lyric lines - the verse/chorus structure to preserve."""

    index: int
    name: str
    kind: SectionKind = SectionKind.OTHER
    #: Inclusive line range within ``LyricsDoc.lines``.
    line_start: int
    line_end: int

    def contains(self, line_index: int) -> bool:
        return self.line_start <= line_index <= self.line_end


class LyricsSource(str, Enum):
    LRCLIB = "lrclib"
    GENIUS = "genius"
    MANUAL = "manual"
    CACHE = "cache"
    #: No official lyrics found - the raw transcript is used as-is.
    RAW = "raw"


#: Hebrew block, including presentation forms.
_RTL_SCRIPT = re.compile(r"[֐-׿יִ-ﭏ]")

#: Languages written right to left that this app can produce.
RTL_LANGUAGES = frozenset({"he", "iw", "yi"})


class LyricsDoc(Artifact):
    language: str = "en"
    source: LyricsSource = LyricsSource.RAW
    title: str = ""
    artist: str = ""
    sections: list[Section] = Field(default_factory=list)
    lines: list[LyricLine] = Field(default_factory=list)

    @property
    def is_rtl(self) -> bool:
        """Whether the sheet should be laid out right to left.

        Decided from the lyrics themselves rather than the detected language
        code. The two can disagree: Whisper sometimes transliterates Hebrew
        into Latin script and reports a European language, and lyrics fetched
        from the web can be in Hebrew script even when the audio was
        misidentified. What is actually going to be rendered is what matters.
        """
        for line in self.lines:
            if _RTL_SCRIPT.search(line.text):
                return True
        if any(line.text.strip() for line in self.lines):
            # There is real text and none of it is Hebrew.
            return False
        return self.language in RTL_LANGUAGES

    def section_for(self, line_index: int) -> Section | None:
        for section in self.sections:
            if section.contains(line_index):
                return section
        return None


# ---------------------------------------------------------------------------
# Stage 3 - chords
# ---------------------------------------------------------------------------

#: Label used for "no chord sounding".
NO_CHORD = "N"


class Beat(Artifact):
    index: int
    #: Position within the bar, 1-based (1 is the downbeat).
    beat_in_bar: int
    start: float
    end: float
    chord: str = NO_CHORD
    #: Raw per-engine predictions, kept for debugging and weight tuning.
    sources: dict[str, str] = Field(default_factory=dict)


class Bar(Artifact):
    index: int
    start: float
    end: float
    beats: list[Beat] = Field(default_factory=list)

    @property
    def chords(self) -> list[str]:
        """Chords in this bar, in order, with consecutive repeats collapsed."""
        out: list[str] = []
        for beat in self.beats:
            if beat.chord != NO_CHORD and (not out or out[-1] != beat.chord):
                out.append(beat.chord)
        return out


class ChordsDoc(Artifact):
    bpm: float = 0.0
    time_signature: int = 4
    key: str = ""
    #: Beats before the first downbeat, i.e. the pickup measure length.
    pickup_beats: int = 0
    #: True when beat positions came from madmom's downbeat tracker rather
    #: than a fallback, so the bar grid can be trusted.
    downbeats_tracked: bool = False
    bars: list[Bar] = Field(default_factory=list)

    def iter_beats(self):
        for bar in self.bars:
            yield from bar.beats


# ---------------------------------------------------------------------------
# Stage 4 - sheet
# ---------------------------------------------------------------------------


class SectionBlock(Artifact):
    type: Literal["section"] = "section"
    name: str
    kind: SectionKind = SectionKind.OTHER
    start: float = 0.0


class InstrumentalBlock(Artifact):
    """A run of bars with no singing, rendered as ``// C / G / Am F //``."""

    type: Literal["instrumental"] = "instrumental"
    kind: SectionKind = SectionKind.INTERLUDE
    text: str
    start: float
    end: float
    bar_indices: list[int] = Field(default_factory=list)


class LyricBlock(Artifact):
    """One lyric line plus the monospace chord row that sits above it."""

    type: Literal["lyric"] = "lyric"
    #: Space-padded chord row aligned to ``text`` when rendered monospace.
    chord_line: str = ""
    text: str
    start: float
    end: float
    #: Index into ``LyricsDoc.lines``, so edits can be written back.
    line_index: int


SheetBlock = Annotated[
    SectionBlock | InstrumentalBlock | LyricBlock,
    Field(discriminator="type"),
]


class SheetDoc(Artifact):
    title: str = ""
    artist: str = ""
    key: str = ""
    bpm: float = 0.0
    is_rtl: bool = False
    blocks: list[SheetBlock] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class StageState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    #: Stage 2 pauses here when no lyrics could be found automatically.
    NEEDS_INPUT = "needs_input"


class StageStatus(Artifact):
    state: StageState = StageState.PENDING
    updated_at: str = Field(default_factory=utcnow)
    message: str = ""
    error: str = ""

    def mark(self, state: StageState, message: str = "", error: str = "") -> None:
        self.state = state
        self.message = message
        self.error = error
        self.updated_at = utcnow()


class Manifest(Artifact):
    song_id: str
    title: str = ""
    artist: str = ""
    language: str = ""
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    stages: dict[str, StageStatus] = Field(
        default_factory=lambda: {name: StageStatus() for name in STAGE_NAMES}
    )

    def stage(self, number: int) -> StageStatus:
        return self.stages.setdefault(STAGE_NAMES[number - 1], StageStatus())


#: Stage keys in ``Manifest.stages``, indexed by stage number - 1.
STAGE_NAMES = ("audio", "lyrics", "chords", "sheet")
