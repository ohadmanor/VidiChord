"""Assign timestamps to official lyrics by aligning them to a raw transcript.

Whisper hears the audio accurately in *time* but poorly in *text*; the lyrics
fetched from LRClib or Genius are the reverse. Aligning the two gives correct
words at correct times.

The matcher is a Needleman-Wunsch global alignment over normalised words. It is
tolerant of insertions, deletions and near-misses, which matters because a
transcript routinely drops repeated lines or invents filler words.

This is a port of the aligner that previously lived in
``lyric_extractor/core.py``. The scoring weights are unchanged; the similarity
lookup is memoised, which removes ~150k redundant ``SequenceMatcher``
constructions on a typical song.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

from .normalize import normalize_word

# Alignment scoring. Lower is better; these are costs, not rewards.
_COST_EXACT = 0.0
_COST_FUZZY = 0.3
_COST_MISMATCH = 1.9
_COST_GAP = 1.0

#: Similarity at or above which two different words count as the same word.
_FUZZY_THRESHOLD = 0.75

# Backtracking directions.
_DIAG, _UP, _LEFT = 0, 1, 2


@dataclass(frozen=True)
class TimedWord:
    """A transcript word with its normalised comparison key."""

    key: str
    start: float
    end: float


@lru_cache(maxsize=1 << 16)
def _similar(a: str, b: str) -> bool:
    """True if two normalised words are close enough to count as a match."""
    if a == b:
        return True
    if len(a) <= 1 or len(b) <= 1:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= _FUZZY_THRESHOLD


def _cost(a: str, b: str) -> float:
    if a == b:
        return _COST_EXACT
    return _COST_FUZZY if _similar(a, b) else _COST_MISMATCH


def align(raw: Sequence[TimedWord], official: Sequence[str]) -> list[int | None]:
    """Align official words to transcript words.

    Returns one entry per official word: the index of the transcript word it
    matched, or None where no match was found.
    """
    n, m = len(raw), len(official)
    if n == 0 or m == 0:
        return [None] * m

    # dp[i][j] = best cost aligning raw[:i] against official[:j].
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    ptr = [[0] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        dp[i][0] = i * _COST_GAP
        ptr[i][0] = _UP
    for j in range(1, m + 1):
        dp[0][j] = j * _COST_GAP
        ptr[0][j] = _LEFT

    for i in range(1, n + 1):
        raw_key = raw[i - 1].key
        dp_prev, dp_cur = dp[i - 1], dp[i]
        ptr_cur = ptr[i]
        for j in range(1, m + 1):
            diag = dp_prev[j - 1] + _cost(raw_key, official[j - 1])
            up = dp_prev[j] + _COST_GAP
            left = dp_cur[j - 1] + _COST_GAP

            if diag <= up and diag <= left:
                dp_cur[j], ptr_cur[j] = diag, _DIAG
            elif up <= left:
                dp_cur[j], ptr_cur[j] = up, _UP
            else:
                dp_cur[j], ptr_cur[j] = left, _LEFT

    matches: list[int | None] = [None] * m
    i, j = n, m
    while i > 0 and j > 0:
        direction = ptr[i][j]
        if direction == _DIAG:
            if _similar(raw[i - 1].key, official[j - 1]):
                matches[j - 1] = i - 1
            i -= 1
            j -= 1
        elif direction == _UP:
            i -= 1
        else:
            j -= 1
    return matches


def flatten_words(segments: Sequence[dict]) -> list[TimedWord]:
    """Flatten transcript segments into a timed word list.

    Segments that carry per-word timings use them directly. For segments
    without them, word times are interpolated evenly across the segment.
    """
    words: list[TimedWord] = []
    for segment in segments:
        segment_words = segment.get("words") or []
        if segment_words:
            for word in segment_words:
                key = normalize_word(word.get("text") or word.get("word", ""))
                if key:
                    words.append(
                        TimedWord(key, float(word["start"]), float(word["end"]))
                    )
            continue

        tokens = str(segment.get("text", "")).split()
        if not tokens:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        step = (end - start) / len(tokens)
        for position, token in enumerate(tokens):
            key = normalize_word(token)
            if key:
                word_start = start + position * step
                words.append(TimedWord(key, word_start, word_start + step))
    return words


def line_times(
    raw_words: Sequence[TimedWord],
    lines: Sequence[str],
    default_line_seconds: float = 2.0,
) -> list[float]:
    """Assign a start time to every official lyric line.

    Lines that align to a transcript word take that word's start time. Lines
    with no match inherit the previous line's time and are then spread evenly
    across the gap, so a run of unmatched lines does not collapse onto a single
    instant.
    """
    if not lines:
        return []

    # Flatten to words, remembering which line each came from.
    words: list[str] = []
    word_line: list[int] = []
    for index, line in enumerate(lines):
        for token in line.split():
            key = normalize_word(token)
            if key:
                words.append(key)
                word_line.append(index)

    times: list[float | None] = [None] * len(lines)
    if words and raw_words:
        matches = align(raw_words, words)
        # Walk backwards so the earliest matched word in a line wins.
        for word_index in range(len(words) - 1, -1, -1):
            raw_index = matches[word_index]
            if raw_index is not None:
                times[word_line[word_index]] = raw_words[raw_index].start

    # Forward-fill unmatched lines from the last known time.
    last = raw_words[0].start if raw_words else 0.0
    for index in range(len(lines)):
        if times[index] is None:
            times[index] = last
        else:
            last = times[index]

    return _spread_duplicates(
        [float(t) for t in times], default_line_seconds=default_line_seconds
    )


def _spread_duplicates(times: list[float], default_line_seconds: float) -> list[float]:
    """Space runs of identical timestamps evenly up to the next distinct one."""
    total = len(times)
    start = 0
    while start < total:
        end = start
        while end + 1 < total and times[end + 1] == times[start]:
            end += 1

        run_length = end - start + 1
        if run_length > 1:
            begin = times[start]
            if end + 1 < total:
                finish = times[end + 1]
            else:
                finish = begin + run_length * default_line_seconds
            step = (finish - begin) / run_length
            for offset in range(run_length):
                times[start + offset] = begin + offset * step
        start = end + 1
    return times
