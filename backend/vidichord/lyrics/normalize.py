"""Text normalisation used when matching a transcript against official lyrics.

Whisper mis-hears words, and Hebrew in particular is written inconsistently
(*ktiv haser* vs *ktiv male*), so the comparison key is deliberately lossy:
punctuation, diacritics, letter-form and optional-vowel differences are all
collapsed away before two words are compared.
"""

from __future__ import annotations

import re
import unicodedata

#: Hebrew niqqud, cantillation marks and the like.
_HEBREW_MARKS = re.compile(r"[֑-ׇ]")

#: Final letter forms mapped to their medial equivalents.
_FINAL_FORMS = str.maketrans({
    "ם": "מ",
    "ן": "נ",
    "ץ": "צ",
    "ף": "פ",
    "ך": "כ",
})

#: Matres lectionis. Dropping them makes ktiv haser and ktiv male compare equal.
_OPTIONAL_VOWELS = str.maketrans({"ו": "", "י": ""})

_HEBREW_RANGE = re.compile(r"[֐-׿]")


def strip_punctuation(text: str) -> str:
    """Remove every Unicode punctuation and symbol character."""
    return "".join(
        ch for ch in text if not unicodedata.category(ch).startswith(("P", "S"))
    )


def normalize_word(word: str) -> str:
    """Reduce a word to its comparison key.

    Lossy by design - two words with the same key are treated as the same word
    during alignment.
    """
    word = unicodedata.normalize("NFC", word)
    word = _HEBREW_MARKS.sub("", word)
    word = strip_punctuation(word).lower().strip()
    word = word.translate(_FINAL_FORMS)
    return word.translate(_OPTIONAL_VOWELS)


def normalize_line(line: str) -> str:
    """Comparison key for a whole line, preserving word boundaries."""
    return " ".join(filter(None, (normalize_word(w) for w in line.split())))


def is_hebrew(text: str) -> bool:
    """True if the text contains any Hebrew character."""
    return bool(_HEBREW_RANGE.search(text))


def detect_language(text: str) -> str:
    """Best-effort language tag for a block of lyrics."""
    return "he" if is_hebrew(text) else "en"
