"""Lyric transcription, official-lyric lookup, structure and alignment."""

# Note: the `align` function is deliberately not re-exported here - doing so
# would shadow the `vidichord.lyrics.align` submodule for anyone importing it.
from .align import TimedWord, flatten_words, line_times
from .normalize import detect_language, is_hebrew, normalize_line, normalize_word
from .providers import (
    LyricsMatch,
    check_overlap,
    fetch_lrclib,
    parse_lrc,
    scrape_genius,
    search_web_lyrics,
)
from .structure import analyse, classify, render
from .whisper_engine import Segment, Transcript, WhisperEngine

__all__ = [
    "TimedWord",
    "flatten_words",
    "line_times",
    "normalize_word",
    "normalize_line",
    "is_hebrew",
    "detect_language",
    "LyricsMatch",
    "fetch_lrclib",
    "search_web_lyrics",
    "scrape_genius",
    "check_overlap",
    "parse_lrc",
    "analyse",
    "classify",
    "render",
    "WhisperEngine",
    "Transcript",
    "Segment",
]
