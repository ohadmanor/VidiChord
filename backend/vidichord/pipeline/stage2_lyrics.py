"""Stage 2 - lyrics with timestamps.

Whisper hears *when* words are sung accurately but *what* is sung poorly, and
published lyrics are the reverse. So the transcript is used purely as a timing
reference and the words come from LRClib or Genius, aligned onto it.

Three things this stage guarantees for later stages:

* the language is identified once, and only the matching model transcribes;
* verse/chorus structure survives as data, not as tags inside a text blob;
* every lyric word carries a timestamp and its column in its line, which is
  what lets stage 4 put chords over the right syllables.

The raw transcript is cached beside the artifact, so choosing to paste lyrics
after an automatic lookup fails does not re-run the slowest step.
"""

from __future__ import annotations

import json
import re

from ..lyrics import align as align_mod
from ..lyrics import providers, structure
from ..lyrics.normalize import normalize_word
from ..lyrics.whisper_engine import WhisperEngine
from ..models import LyricLine, LyricsDoc, LyricsSource, SourceDoc, Word
from . import NeedsUserInput, StageContext

#: Cache of the Whisper transcript, so a re-run need not transcribe again.
TRANSCRIPT_FILENAME = "02_transcript.json"

#: Bracketed parts of a video title, which describe the upload and not the
#: song. See :func:`_title_variants`.
_TITLE_NOISE = re.compile(r"[(\[][^)\]]*[)\]]")

#: A trailing annotation after a vertical bar - "| Official Video",
#: "| הקליפ הרשמי". The song's name is whatever precedes it.
_TITLE_TAIL = re.compile(r"\s*\|.*$")

#: Vocabulary overlap required before a search hit is accepted.
_MIN_OVERLAP_FILENAME = 0.30
_MIN_OVERLAP_SNIPPET = 0.25

#: Filenames made only of these words carry no useful search signal.
_GENERIC_TERMS = {
    "test", "audio", "sample", "track", "song", "recording", "voice", "mic",
    "he", "en", "untitled", "unknown",
}

#: Assumed length of the final lyric line, which has nothing after it.
_TRAILING_LINE_SECONDS = 4.0

#: A gap this short between lines is treated as part of the earlier line;
#: anything longer is a genuine break in the singing.
_MAX_GAP_TO_ABSORB = 4.0


# ---------------------------------------------------------------------------
# Transcript caching
# ---------------------------------------------------------------------------


def _load_transcript(context: StageContext) -> tuple[str, list[dict]] | None:
    path = context.project.root / TRANSCRIPT_FILENAME
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("language", ""), data.get("segments", [])
    except (OSError, ValueError):
        return None


def _save_transcript(context: StageContext, language: str, segments: list[dict]) -> None:
    path = context.project.root / TRANSCRIPT_FILENAME
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"language": language, "segments": segments}, handle, ensure_ascii=False)


def _transcribe(context: StageContext) -> tuple[str, list[dict]]:
    """Transcribe the audio, reusing a cached transcript when one exists."""
    requested = context.param("language")

    if not context.param("retranscribe", False):
        cached = _load_transcript(context)
        # A cached transcript in the wrong language is worse than useless: it
        # was produced by the wrong model. Asking for a specific language has
        # to override the cache.
        if cached is not None and (requested is None or cached[0] == requested):
            context.report("Reusing cached transcript.", 30.0)
            return cached
        if cached is not None:
            context.report(
                f"Cached transcript is {cached[0]}, but {requested} was asked "
                "for - transcribing again.",
                None,
            )

    engine = WhisperEngine()
    prompt = context.param("lyrics") if context.param("choice") == "manual" else None

    transcript = engine.transcribe(
        str(context.project.audio_path),
        language=context.param("language"),
        initial_prompt=prompt,
        on_progress=lambda message: context.report(message, None),
    )
    segments = transcript.as_dicts()
    _save_transcript(context, transcript.language, segments)
    return transcript.language, segments


# ---------------------------------------------------------------------------
# Finding official lyrics
# ---------------------------------------------------------------------------


def _snippets(segments: list[dict]) -> list[str]:
    """Short phrases from across the transcript, for searching by."""
    words: list[str] = []
    for segment in segments:
        words.extend(str(segment.get("text", "")).split())

    total = len(words)
    if not total:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for position in (0, total // 4, total // 2):
        snippet = " ".join(words[position: position + 6]).strip()
        if snippet and snippet not in seen:
            candidates.append(snippet)
            seen.add(snippet)

    longer = " ".join(words[:12]).strip()
    if longer and longer not in seen:
        candidates.append(longer)
    return candidates


def _title_variants(title: str) -> list[str]:
    """Search forms of a video title, most specific first.

    An uploader's additions - "(Official Video)", "[HD]", "| הקליפ הרשמי", the
    name of the project a recording came from - describe the upload rather
    than the song, and no lyrics database is indexed under them. Searching
    them away as well is often the difference between one hit and none.
    """
    variants: list[str] = []
    for candidate in (title, _TITLE_TAIL.sub("", title)):
        for form in (candidate, _TITLE_NOISE.sub(" ", candidate)):
            cleaned = " ".join(form.split())
            if cleaned and cleaned not in variants:
                variants.append(cleaned)
    return variants


def _lookup(
    context: StageContext, segments: list[dict], source: SourceDoc
) -> providers.LyricsMatch | None:
    """Search for official lyrics, most reliable signal first."""
    transcript_text = " ".join(str(s.get("text", "")) for s in segments)

    # 1. The title and artist we already know from the video metadata.
    for title in _title_variants(source.title) if source.title else []:
        query = f"{title} {source.artist}".strip()
        context.report(f"Searching LRClib for '{query}'...", None)
        match = providers.fetch_lrclib(query)
        if match is None and source.artist and source.artist != "Unknown":
            match = providers.fetch_lrclib(title, expected_artist=source.artist)
        if match is None:
            # A database may hold only another artist's recording of the same
            # song, whose words are still the right ones - so the artist is
            # not required here. Overlap with the transcript is what decides
            # whether this is the same song at all.
            match = providers.fetch_lrclib(title)
        if match is not None:
            overlap = providers.check_overlap(transcript_text, match.lyrics)
            if overlap >= _MIN_OVERLAP_FILENAME or not transcript_text.strip():
                context.report(f"Found '{match.title}' by {match.artist}.", None)
                return match
            context.report(f"Rejected weak match ({overlap:.0%} overlap).", None)

    candidates = _snippets(segments)

    # 2. Phrases lifted from the transcript.
    for snippet in candidates:
        context.report(f"Searching LRClib for '{snippet}'...", None)
        match = providers.fetch_lrclib(snippet)
        if match is None:
            continue
        overlap = providers.check_overlap(transcript_text, match.lyrics)
        if overlap >= _MIN_OVERLAP_SNIPPET:
            context.report(f"Found '{match.title}' by {match.artist}.", None)
            return match

    # 3. The open web. Genius pages also carry the section structure, and
    #    overlap is not checked here because Whisper transcribes non-Latin
    #    scripts phonetically, which drives the measure to near zero even for
    #    a correct match.
    queries = candidates or ([f"{source.artist} {source.title}"] if source.title else [])
    if queries:
        context.report("Searching the web for lyrics...", None)
        match = providers.search_web_lyrics(queries)
        if match is not None:
            context.report(f"Found '{match.title}' on Genius.", None)
            return match

    return None


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def _tokenise(lines: list[str]) -> tuple[list[str], list[tuple[int, int, str]]]:
    """Split lyric lines into words, keeping each word's line and column."""
    keys: list[str] = []
    positions: list[tuple[int, int, str]] = []

    for line_index, line in enumerate(lines):
        cursor = 0
        for token in line.split():
            offset = line.find(token, cursor)
            if offset < 0:
                offset = cursor
            cursor = offset + len(token)

            key = normalize_word(token)
            if key:
                keys.append(key)
                positions.append((line_index, offset, token))
    return keys, positions


def _interpolate_missing(times: list[float | None], fallback_end: float) -> list[float]:
    """Fill gaps in a monotonic time series by even spacing."""
    known = [index for index, value in enumerate(times) if value is not None]
    if not known:
        step = fallback_end / max(1, len(times))
        return [index * step for index in range(len(times))]

    filled: list[float] = [0.0] * len(times)
    for index, value in enumerate(times):
        if value is not None:
            filled[index] = value

    # Before the first known value.
    for index in range(known[0]):
        filled[index] = filled[known[0]]
    # Between known values.
    for left, right in zip(known, known[1:]):
        gap = right - left
        if gap > 1:
            step = (filled[right] - filled[left]) / gap
            for offset in range(1, gap):
                filled[left + offset] = filled[left] + offset * step
    # After the last known value.
    tail_start = filled[known[-1]]
    remaining = len(times) - known[-1] - 1
    if remaining > 0:
        step = max(0.2, (fallback_end - tail_start) / remaining)
        for offset in range(1, remaining + 1):
            filled[known[-1] + offset] = tail_start + offset * step
    return filled


def _build_lines(
    lines: list[str],
    raw_words: list[align_mod.TimedWord],
    duration: float,
    lrc_times: list[float] | None,
) -> list[LyricLine]:
    """Attach timings to every lyric line and word."""
    keys, positions = _tokenise(lines)

    starts: list[float | None] = [None] * len(keys)
    ends: list[float | None] = [None] * len(keys)

    if keys and raw_words:
        matches = align_mod.align(raw_words, keys)
        for index, raw_index in enumerate(matches):
            if raw_index is not None:
                starts[index] = raw_words[raw_index].start
                ends[index] = raw_words[raw_index].end

    horizon = duration or (raw_words[-1].end if raw_words else float(len(keys)))
    word_starts = _interpolate_missing(starts, horizon)
    word_ends = _interpolate_missing(ends, horizon)

    # Group words back onto their lines.
    per_line: list[list[Word]] = [[] for _ in lines]
    for index, (line_index, offset, token) in enumerate(positions):
        start = word_starts[index]
        end = max(word_ends[index], start)
        per_line[line_index].append(
            Word(text=token, start=start, end=end, offset=offset)
        )

    built: list[LyricLine] = []
    for index, text in enumerate(lines):
        words = per_line[index]
        if lrc_times is not None and index < len(lrc_times):
            # LRC line timings are authoritative; they beat anything inferred.
            start = lrc_times[index]
        elif words:
            start = words[0].start
        elif built:
            start = built[-1].end
        else:
            start = 0.0

        end = words[-1].end if words else start + _TRAILING_LINE_SECONDS
        built.append(
            LyricLine(index=index, time=start, end=max(end, start), text=text, words=words)
        )

    # Close a small gap to the next line so a chord landing just after the last
    # word still belongs to it. A large gap is left open: nobody is singing
    # there, and stage 4 will render it as an instrumental passage. Stretching
    # every line to meet the next one instead would crowd a whole interlude's
    # worth of chords onto one short lyric.
    for index in range(len(built) - 1):
        next_start = built[index + 1].time
        if next_start - built[index].end <= _MAX_GAP_TO_ABSORB:
            built[index].end = max(built[index].end, next_start)
        built[index].end = min(built[index].end, next_start)

    if built:
        built[-1].end = max(built[-1].end, built[-1].time + _TRAILING_LINE_SECONDS)
        if duration:
            built[-1].end = min(built[-1].end, duration)

    return built


def _lrc_line_times(lrc: str, line_count: int) -> list[float] | None:
    """Map LRC timestamps onto lyric lines, one for one."""
    entries = providers.parse_lrc(lrc)
    if not entries:
        return None
    times = [seconds for seconds, _text in entries]
    if len(times) < line_count:
        # Structure differs from the LRC; trust alignment instead.
        return None
    return times[:line_count]


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run(context: StageContext) -> None:
    project = context.project
    source = project.read_optional(SourceDoc) or SourceDoc()

    if not project.audio_path.is_file():
        raise RuntimeError("Stage 1 must run before lyrics can be extracted")

    context.report("Transcribing audio...", 5.0)
    language, segments = _transcribe(context)
    raw_words = align_mod.flatten_words(segments)

    choice = context.param("choice", "auto")
    official: str | None = None
    lrc: str | None = None
    lyrics_source = LyricsSource.RAW
    title, artist = source.title, source.artist

    if choice == "manual":
        official = context.param("lyrics") or ""
        lyrics_source = LyricsSource.MANUAL
        context.report("Aligning pasted lyrics...", 60.0)
    elif choice == "ai":
        context.report("Using the transcript as the lyrics.", 60.0)
    else:
        context.report("Looking up official lyrics...", 40.0)
        match = _lookup(context, segments, source)
        if match is not None:
            official = match.lyrics
            lrc = match.synced_lyrics
            lyrics_source = match.source
            # The provider's title is the song's, without whatever the
            # uploader added to name their own video - so it is the better of
            # the two. Its artist is not: the words may have come from another
            # artist's recording of the same song, while the performance being
            # transcribed is the one in the video.
            title = match.title or title
            artist = artist if artist and artist != "Unknown" else match.artist
        else:
            raise NeedsUserInput(
                "No lyrics found for this song. Transcribe with Whisper alone, "
                "or paste the lyrics.",
                options=["ai", "manual"],
            )

    if official and official.strip():
        lines, sections = structure.analyse(official)
    else:
        # No official lyrics: fall back to the transcript, one line per phrase.
        lines = [str(s.get("text", "")).strip() for s in segments]
        lines = [line for line in lines if line]
        sections = structure.infer_sections_from_lines(lines)
        lyrics_source = LyricsSource.RAW

    if not lines:
        raise RuntimeError("No lyrics could be produced for this song")

    context.report(f"Timing {len(lines)} lines...", 75.0)
    lrc_times = _lrc_line_times(lrc, len(lines)) if lrc else None
    built = _build_lines(lines, raw_words, source.duration, lrc_times)

    for line in built:
        section = next(
            (s for s in sections if s.line_start <= line.index <= s.line_end), None
        )
        line.section_index = section.index if section else None

    document = LyricsDoc(
        language=language or "en",
        source=lyrics_source,
        title=title,
        artist=artist,
        sections=sections,
        lines=built,
    )
    project.write(document)

    manifest = project.read_manifest()
    manifest.title = title or manifest.title
    manifest.artist = artist or manifest.artist
    manifest.language = document.language
    project.write_manifest(manifest)

    context.report(
        f"{len(built)} lines across {len(sections)} sections "
        f"({lyrics_source.value}).",
        100.0,
    )
