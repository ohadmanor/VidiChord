"""Sources of official lyrics.

Whisper supplies timing, these supply the words. Two providers, tried in order:

``LRClib``
    A lyrics API. Best case it returns *synced* LRC, which gives per-line
    timestamps directly and makes alignment unnecessary.

``Genius`` (via a DuckDuckGo search)
    HTML scrape. No timing, but the pages carry ``[Verse 1]`` / ``[Chorus]``
    headers, which is where the song structure comes from.

Ported from ``lyric_extractor/core.py`` with the network calls given explicit
timeouts and a truthful User-Agent.
"""

from __future__ import annotations

import difflib
import re
import urllib.parse
from dataclasses import dataclass
from html import unescape
from typing import Sequence

import requests

from ..models import LyricsSource

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_API_HEADERS = {"User-Agent": "VidiChord/1.0 (+https://github.com/ohadmanor/VidiChord)"}

_LRCLIB_SEARCH = "https://lrclib.net/api/search"
_DDG_HTML = "https://html.duckduckgo.com/html/"

_TIMEOUT = 20
_ATTEMPTS = 3

#: Artist-name similarity required when a query is filtered by artist.
_ARTIST_SIMILARITY = 0.6


@dataclass
class LyricsMatch:
    """Lyrics found for a song, with whatever timing the provider had."""

    title: str
    artist: str
    lyrics: str
    source: LyricsSource
    #: Raw LRC text when the provider had per-line timings.
    synced_lyrics: str | None = None


def check_overlap(transcript: str, lyrics: str) -> float:
    """Fraction of shared vocabulary between a transcript and candidate lyrics.

    Used to reject a plausible-looking but wrong search hit. The smaller word
    set is the denominator, which keeps the measure meaningful when Whisper has
    mangled a lot of words.
    """

    def vocabulary(text: str) -> set[str]:
        words = {"".join(c for c in word.lower() if c.isalnum()) for word in text.split()}
        words.discard("")
        return words

    transcript_words = vocabulary(transcript)
    lyric_words = vocabulary(lyrics)
    if not transcript_words or not lyric_words:
        return 0.0

    shared = transcript_words & lyric_words
    return len(shared) / min(len(transcript_words), len(lyric_words))


# ---------------------------------------------------------------------------
# LRClib
# ---------------------------------------------------------------------------


def fetch_lrclib(query: str, expected_artist: str | None = None) -> LyricsMatch | None:
    """Search LRClib, optionally requiring the artist name to roughly match."""
    if not query.strip():
        return None

    url = f"{_LRCLIB_SEARCH}?q={urllib.parse.quote(query)}"
    for attempt in range(_ATTEMPTS):
        try:
            response = requests.get(url, headers=_API_HEADERS, timeout=_TIMEOUT)
        except requests.RequestException:
            if attempt == _ATTEMPTS - 1:
                return None
            continue

        if response.status_code != 200:
            return None

        try:
            results = response.json()
        except ValueError:
            return None

        for item in results:
            plain = item.get("plainLyrics")
            if not plain:
                continue
            if expected_artist:
                similarity = difflib.SequenceMatcher(
                    None,
                    expected_artist.lower(),
                    (item.get("artistName") or "").lower(),
                ).ratio()
                if similarity <= _ARTIST_SIMILARITY:
                    continue
            return LyricsMatch(
                title=item.get("trackName") or "",
                artist=item.get("artistName") or "",
                lyrics=plain,
                synced_lyrics=item.get("syncedLyrics"),
                source=LyricsSource.LRCLIB,
            )
        return None
    return None


# ---------------------------------------------------------------------------
# Genius, reached through a DuckDuckGo search
# ---------------------------------------------------------------------------

_GENIUS_DIRECT = re.compile(r"https://genius\.com/[A-Za-z0-9][A-Za-z0-9\-]+-lyrics")
_GENIUS_REDIRECT = re.compile(r"uddg=(https?%3A%2F%2Fgenius\.com%2F[^&\"<]+)")
_TITLE_TAG = re.compile(r"<title>([^<]+)</title>")
_TITLE_PARTS = re.compile(r"^(.+?)\s+[–-]\s+(.+?)\s+Lyrics")
_LYRICS_CONTAINER = re.compile(
    r'data-lyrics-container="true"[^>]*>(.+?)</div>\s*(?=<div|$)', re.DOTALL
)
_LYRICS_CONTAINER_LOOSE = re.compile(
    r'data-lyrics-container="true"[^>]*>(.*?)</div>', re.DOTALL
)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HEADER_LINK = re.compile(r"<a[^>]*>\s*<h3[^>]*>([^<]+)</h3>\s*</a>")
_HEADING = re.compile(r"<h[1-6][^>]*>([^<]+)</h[1-6]>")
_TAG = re.compile(r"<[^>]+>")

#: Below this the scrape almost certainly grabbed the wrong element.
_MIN_LYRIC_CHARS = 50


def _find_genius_url(html: str) -> str | None:
    direct = _GENIUS_DIRECT.findall(html)
    if direct:
        return direct[0]
    for encoded in _GENIUS_REDIRECT.findall(html):
        decoded = urllib.parse.unquote(encoded)
        if "-lyrics" in decoded:
            return decoded
    return None


def search_web_lyrics(snippets: Sequence[str]) -> LyricsMatch | None:
    """Find a Genius page for any of these transcript snippets, then scrape it."""
    for snippet in snippets:
        if not snippet.strip():
            continue
        for query in (
            f"{snippet} lyrics genius",
            f"{snippet} song lyrics",
            f'"{snippet}" lyrics',
        ):
            try:
                response = requests.get(
                    f"{_DDG_HTML}?q={urllib.parse.quote(query)}",
                    headers=_BROWSER_HEADERS,
                    timeout=15,
                )
            except requests.RequestException:
                continue

            url = _find_genius_url(response.text)
            if url:
                match = scrape_genius(url)
                if match:
                    return match
    return None


def scrape_genius(url: str) -> LyricsMatch | None:
    """Extract title, artist and structured lyrics from a Genius song page."""
    try:
        response = requests.get(url, headers=_BROWSER_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException:
        return None

    html = response.text

    title_tag = _TITLE_TAG.search(html)
    page_title = unescape(title_tag.group(1)) if title_tag else ""
    parts = _TITLE_PARTS.match(page_title)
    if parts:
        artist, track = parts.group(1).strip(), parts.group(2).strip()
    else:
        artist = "Unknown"
        track = url.rstrip("/").split("/")[-1].replace("-lyrics", "").replace("-", " ").title()

    containers = _LYRICS_CONTAINER.findall(html) or _LYRICS_CONTAINER_LOOSE.findall(html)
    if not containers:
        return None

    parts_out: list[str] = []
    for container in containers:
        text = _BR.sub("\n", container)
        # Section headers live in <a><h3>...</h3></a>; keep them as [tags].
        text = _HEADER_LINK.sub(r"\n[\1]\n", text)
        text = _HEADING.sub(r"\n[\1]\n", text)
        text = _TAG.sub("", text)
        text = unescape(text).strip()
        if text:
            parts_out.append(text)

    lyrics = "\n\n".join(parts_out).strip()
    if len(lyrics) < _MIN_LYRIC_CHARS:
        return None

    return LyricsMatch(
        title=track,
        artist=artist,
        lyrics=lyrics,
        synced_lyrics=None,
        source=LyricsSource.GENIUS,
    )


# ---------------------------------------------------------------------------
# LRC parsing
# ---------------------------------------------------------------------------

_LRC_LINE = re.compile(r"^\[(\d{1,2}):(\d{2}(?:\.\d+)?)\]")


def parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """Parse LRC text into ``(seconds, text)`` pairs, skipping empty lines."""
    out: list[tuple[float, str]] = []
    for raw in lrc.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw.strip()
        match = _LRC_LINE.match(stripped)
        if not match:
            continue
        text = stripped[match.end():].strip()
        if text:
            seconds = int(match.group(1)) * 60 + float(match.group(2))
            out.append((seconds, text))
    return out
