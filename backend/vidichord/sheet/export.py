"""Render a sheet to songbook's plain-text format, and write it for songbook.

songbook stores a song as one raw text blob plus an ``isRTL`` flag, and renders
it monospace. So the chord rows built by :mod:`.layout` transfer across
unchanged - the alignment is already carried in the spacing. Its song editor
imports one song from a JSON file (title, artist, key, isRTL, rawText), which
is what :func:`write_export` writes: one file per song, in a folder the user
picks the file from.

Writing it has to survive whatever a song is called and wherever the folder
is. A title is not a filename - it can hold characters Windows refuses, run
past the length a name may have, or be a device name - and two songs can
clean to the same name. So names are cleaned, capped and made unique, a
song's own earlier export is updated in place, and the file is written to a
temporary name and swapped in, so a failure never leaves a broken file where
a good one was.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Any

from ..models import InstrumentalBlock, LyricBlock, SheetDoc

#: Characters Windows forbids in a filename.
_ILLEGAL = re.compile(r'[\\/*?:"<>|]')

#: Control characters, which Windows refuses too, and the invisible marks -
#: zero-width spaces, direction marks and embeddings, the byte-order mark -
#: that would make two names that look the same differ. Not the zero-width
#: joiner and non-joiner: emoji sequences and Persian spelling need them.
_INVISIBLE = re.compile(r"[\x00-\x1f\x7f\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

_WHITESPACE = re.compile(r"\s+")

#: Names Windows keeps for devices: "CON.json" cannot be created at all.
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}

#: The longest a name may be, in the UTF-16 units Windows counts - an emoji is
#: two of them.
_MAX_NAME = 255

_SUFFIX = ".json"

#: How many "Title (2).json" variants to try before giving up on a name.
_MAX_VARIANTS = 99

#: Marks a file as this app's export of one song, so a re-export updates it
#: rather than adding another. songbook's import reads only the fields it
#: knows, so it ignores this one.
SONG_ID_FIELD = "vidichordSongId"


def render_text(sheet: SheetDoc) -> str:
    """Render the sheet as songbook raw text.

    Sections are left unnamed: a blank line between them is the whole of the
    structure the reader sees, which is also how songbook reads a pasted lyric
    back in.
    """
    lines: list[str] = []

    for block in sheet.blocks:
        if block.starts_section and lines:
            lines.append("")

        if isinstance(block, InstrumentalBlock):
            lines.append(block.text)
        elif isinstance(block, LyricBlock):
            if (block.chord_line or "").strip():
                lines.append(block.chord_line)
            lines.append(block.text)

    # Collapse any run of blank lines down to one.
    output: list[str] = []
    for line in lines:
        if not line.strip() and output and not output[-1].strip():
            continue
        output.append(line)

    # Drop blank lines at either end - but not the spaces that open the first
    # chord row: they are what puts its first chord over the right word, and
    # a plain strip() moved it to the start of the line.
    while output and not output[0].strip():
        output.pop(0)
    while output and not output[-1].strip():
        output.pop()
    return "\n".join(output) + "\n"


def _clean(text: str | None) -> str:
    """A title or artist with nothing in it a filename cannot hold."""
    text = unicodedata.normalize("NFC", _text(text))
    text = _INVISIBLE.sub(" ", text)
    text = _ILLEGAL.sub("", text)
    # Windows drops trailing dots and spaces from a name, so a name ending in
    # one is not the name the file actually gets.
    return _WHITESPACE.sub(" ", text).strip().rstrip(". ")


def _units(text: str) -> int:
    """Length in UTF-16 code units, which is how Windows measures a name."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _fit(stem: str, room: int) -> str:
    """Cut ``stem`` to ``room`` UTF-16 units, on a character boundary."""
    if _units(stem) <= room:
        return stem
    cut = ""
    for char in stem:
        if _units(cut + char) > room:
            break
        cut += char
    # Do not leave an accent without its letter, or a dangling separator.
    while cut and unicodedata.combining(cut[-1]):
        cut = cut[:-1]
    return cut.rstrip(" .-")


def safe_stem(title: str, artist: str, song_id: str = "") -> str:
    """The name of a song's export, without the extension.

    "Title - Artist" as far as they survive cleaning; the artist when the
    title does not; the song's own id when neither does. Never empty, never a
    device name, and short enough to leave room for a " (2)" and ".json".
    """
    clean_title, clean_artist = _clean(title), _clean(artist)
    if clean_title and clean_artist:
        stem = f"{clean_title} - {clean_artist}"
    elif clean_title:
        stem = clean_title
    elif clean_artist:
        stem = f"Untitled - {clean_artist}"
    else:
        stem = _clean(song_id) or "Untitled song"

    if stem.split(".")[0].strip().upper() in _RESERVED:
        stem = f"_{stem}"
    room = _MAX_NAME - _units(_SUFFIX) - _units(f" ({_MAX_VARIANTS})")
    return _fit(stem, room) or "Untitled song"


def safe_filename(title: str, artist: str, song_id: str = "") -> str:
    """Build a songbook filename from the song's title and artist."""
    return safe_stem(title, artist, song_id) + _SUFFIX


def songbook_id(song_id: str) -> str:
    """A songbook id for a song: the same on every export, different per song."""
    return "song_vc_" + hashlib.sha1(song_id.encode("utf-8")).hexdigest()[:12]


def _text(value: str | None) -> str:
    """Text that encodes as UTF-8: a stray surrogate from a scraped lyric
    would otherwise fail the write half-way."""
    return (value or "").encode("utf-8", "replace").decode("utf-8")


#: songbook's key menu offers sharps only: its 24 keys are these roots, with
#: "m" after them for minor.
_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#",
          "Cb": "B", "Fb": "E", "E#": "F", "B#": "C"}
_KEY = re.compile(r"^\s*([A-Ga-g])([#b]?)\s*(.*)$")


def songbook_key(key: str | None) -> str:
    """A key as songbook's key field takes it: "Ab major" -> "G#", "E minor" -> "Em".

    songbook's import puts the key straight into a menu whose options are
    spelled that way, so anything else - "A major", as VidiChord writes it -
    matched no option and the key was dropped on import. A key that cannot be
    read becomes empty, which is songbook's "no key".
    """
    match = _KEY.match(key or "")
    if not match:
        return ""
    root = _SHARP.get(match[1].upper() + match[2], match[1].upper() + match[2])
    mode = match[3].strip().lower()
    minor = mode in ("m", "min") or mode.startswith("minor")
    if mode and not minor and not mode.startswith("maj"):
        return ""
    return root + ("m" if minor else "")


def songbook_payload(
    sheet: SheetDoc, raw_text: str | None = None, song_id: str = ""
) -> dict[str, Any]:
    """Build the JSON document songbook expects for one song."""
    return {
        "id": songbook_id(song_id or f"{sheet.title} - {sheet.artist}"),
        "title": _text(sheet.title),
        "artist": _text(sheet.artist),
        "key": songbook_key(sheet.key),
        "isRTL": bool(sheet.is_rtl),
        "rawText": _text(raw_text if raw_text is not None else render_text(sheet)),
        "modifiedByUser": True,
        SONG_ID_FIELD: song_id,
    }


def _belongs_to(path: Path, sheet: SheetDoc, song_id: str) -> bool:
    """Whether the file at ``path`` is this song's own earlier export.

    Ours if it carries this song's id. A file with no id at all is how every
    export made before the id was added looks, so one of those with this
    song's title and artist is taken as ours too - otherwise the first
    re-export after an upgrade would leave it stale beside a "(2)" copy.

    A file that cannot be read at all - open without sharing in another
    program - is reported, not guessed at: taking it for another song's file
    quietly made a duplicate. One that reads but is not JSON (edited by hand)
    is somebody else's and is left alone.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, UnicodeDecodeError):
        return False
    except OSError as error:
        raise ExportError(
            f"{path.name} in {path.parent} is open in another program, so VidiChord "
            "cannot tell whether it is this song's. Close it there and export again."
        ) from error
    if not isinstance(data, dict):
        return False
    if SONG_ID_FIELD in data:
        return data[SONG_ID_FIELD] == song_id
    return (
        "rawText" in data
        and data.get("title") == sheet.title
        and data.get("artist") == sheet.artist
    )


def export_path(folder: Path, sheet: SheetDoc, song_id: str) -> Path:
    """Where this song's export goes in ``folder``.

    Its own earlier export if there is one under its name; otherwise the
    first free "Title - Artist (n).json". Another song's file - two songs can
    clean to the same name, and Windows compares names without case - is
    never written over.
    """
    stem = safe_stem(sheet.title, sheet.artist, song_id)
    for n in range(1, _MAX_VARIANTS + 1):
        candidate = folder / (f"{stem}{_SUFFIX}" if n == 1 else f"{stem} ({n}){_SUFFIX}")
        # A folder of that name is as taken as another song's file.
        if candidate.is_dir():
            continue
        if not candidate.exists() or _belongs_to(candidate, sheet, song_id):
            return candidate
    raise ExportError(
        f'There are already {_MAX_VARIANTS} songs named "{stem}" in {folder}. '
        "Move some of them out, or choose another folder in Settings."
    )


#: One export at a time. Choosing the name and swapping the file in have to
#: happen together: two at once could pick the same free name, and Windows
#: refuses to replace a file while another replace of it is under way.
_write_lock = threading.Lock()


class ExportError(Exception):
    """An export that could not be written, in words worth showing the user."""


def write_export(folder: Path, sheet: SheetDoc, song_id: str) -> Path:
    """Write ``sheet`` into ``folder`` for songbook, and return the file.

    Raises :class:`ExportError`, with the reason in plain words, when it
    cannot: the folder cannot be created or written to, or the file is
    read-only or open in another program.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        raise ExportError(f"The songbook folder {folder} is a file, not a folder. Choose another in Settings.")
    except OSError as error:
        raise ExportError(
            f"Could not create the songbook folder {folder}: {_reason(error)}. "
            "Choose another folder in Settings."
        ) from error
    if not folder.is_dir():
        raise ExportError(f"The songbook folder {folder} is not a folder. Choose another in Settings.")

    data = json.dumps(songbook_payload(sheet, song_id=song_id), indent=2, ensure_ascii=False)

    with _write_lock:
        destination = export_path(folder, sheet, song_id)

        # A temporary file beside it, swapped in once it is complete: a
        # failure part-way - a full disk, a file locked by another program -
        # then leaves the previous export as it was, never a truncated one.
        # Its name is unique to this export, so none can write into another's.
        try:
            handle, tmp_name = tempfile.mkstemp(dir=folder, prefix=".vidichord-", suffix=".tmp")
        except OSError as error:
            raise ExportError(_create_failure(error, folder)) from error
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            _discard(tmp)
            raise ExportError(_create_failure(error, folder)) from error
        except BaseException:
            _discard(tmp)
            raise
        try:
            os.replace(tmp, destination)
        except OSError as error:
            _discard(tmp)
            raise ExportError(_write_failure(error, folder, destination)) from error
    return destination


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _reason(error: OSError) -> str:
    return error.strerror or str(error)


def _create_failure(error: OSError, folder: Path) -> str:
    """Why the new file could not be written in ``folder`` at all."""
    if isinstance(error, PermissionError):
        return (
            f"VidiChord is not allowed to create files in {folder}. "
            "Choose another folder in Settings."
        )
    if error.errno == 28:
        return f"There is no room left on the disk holding {folder}."
    return f"Could not write to {folder}: {_reason(error)}."


def _write_failure(error: OSError, folder: Path, destination: Path) -> str:
    """Why the finished file could not take the place of the old one."""
    name = destination.name
    if isinstance(error, PermissionError):
        # 32: the file is open in another program without sharing.
        if getattr(error, "winerror", None) == 32:
            return f"{name} is open in another program. Close it there and export again."
        if destination.exists() and not os.access(destination, os.W_OK):
            return (
                f"{name} in {folder} is read-only, so it cannot be updated. "
                "Clear its read-only setting, or delete it, and export again."
            )
        if destination.exists():
            return f"{name} is in use by another program. Close it there and export again."
        return (
            f"VidiChord is not allowed to write to {folder}. "
            "Choose another folder in Settings."
        )
    if getattr(error, "winerror", None) == 206 or error.errno == 36:
        return f"The path to {name} is too long for Windows. Choose a shorter folder in Settings."
    if error.errno == 28:
        return f"There is no room left on the disk holding {folder}."
    return f"Could not write {name} to {folder}: {_reason(error)}."
