"""Per-song project folders and typed artifact I/O.

A song is a directory under the library root holding its audio and one JSON
file per pipeline stage::

    VidiChord_Files/<song_id>/
        audio.wav
        manifest.json
        01_source.json
        02_lyrics.json
        03_chords.json
        04_sheet.json
        sheet.txt

This replaces the old scheme of four type-directories whose files were
re-associated by stripping regexes off the WAV basename. Because each stage
reads the artifacts before it and writes exactly one file, any stage can be
re-run on its own without repeating the ones ahead of it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Iterator, TypeVar

from .models import (
    STAGE_NAMES,
    ChordsDoc,
    LyricsDoc,
    Manifest,
    SheetDoc,
    SourceDoc,
    utcnow,
)

ArtifactT = TypeVar("ArtifactT", SourceDoc, LyricsDoc, ChordsDoc, SheetDoc, Manifest)

AUDIO_FILENAME = "audio.wav"
MANIFEST_FILENAME = "manifest.json"
SHEET_TEXT_FILENAME = "sheet.txt"

_ARTIFACT_FILENAMES = {
    SourceDoc: "01_source.json",
    LyricsDoc: "02_lyrics.json",
    ChordsDoc: "03_chords.json",
    SheetDoc: "04_sheet.json",
}

# Characters Windows forbids in a path component, plus control characters.
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")

#: Keep folder names comfortably inside Windows' 260-character path limit.
_MAX_SLUG_LEN = 70


def make_song_id(artist: str, title: str, seed: str = "") -> str:
    """Build a filesystem-safe, stable identifier for a song.

    Unicode is preserved so Hebrew titles stay readable; only characters that
    are illegal in a Windows path component are stripped. A short hash of
    ``seed`` (the source URL, when there is one) is appended so two different
    videos sharing a title never collide.
    """
    raw = f"{artist} - {title}".strip(" -") or "untitled"
    slug = unicodedata.normalize("NFC", raw)
    slug = _ILLEGAL.sub(" ", slug)
    slug = _WHITESPACE.sub(" ", slug).strip()
    # Windows silently drops trailing dots and spaces from directory names.
    slug = slug.rstrip(". ") or "untitled"
    if len(slug) > _MAX_SLUG_LEN:
        slug = slug[:_MAX_SLUG_LEN].rstrip(". ")

    digest = hashlib.blake2b(
        (seed or raw).encode("utf-8"), digest_size=3
    ).hexdigest()
    return f"{slug} [{digest}]"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` without leaving a truncated file behind."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


class SongProject:
    """A single song's folder, with typed read/write helpers."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.song_id = self.root.name

    # -- construction ------------------------------------------------------

    @classmethod
    def create(cls, library_dir: Path, song_id: str) -> "SongProject":
        project = cls(Path(library_dir) / song_id)
        project.root.mkdir(parents=True, exist_ok=True)
        if not project.manifest_path.exists():
            project.write_manifest(Manifest(song_id=project.song_id))
        return project

    @classmethod
    def open(cls, library_dir: Path, song_id: str) -> "SongProject":
        """Open an existing project, raising if it is absent or escapes the library."""
        root = (Path(library_dir) / song_id).resolve()
        library = Path(library_dir).resolve()
        # song_id arrives from the URL, so reject anything that walks upward.
        if root == library or library not in root.parents:
            raise KeyError(song_id)
        if not root.is_dir():
            raise KeyError(song_id)
        return cls(root)

    @classmethod
    def iter_all(cls, library_dir: Path) -> Iterator["SongProject"]:
        library = Path(library_dir)
        if not library.is_dir():
            return
        for entry in sorted(library.iterdir()):
            if (entry / MANIFEST_FILENAME).is_file():
                yield cls(entry)

    # -- paths -------------------------------------------------------------

    @property
    def audio_path(self) -> Path:
        return self.root / AUDIO_FILENAME

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_FILENAME

    @property
    def sheet_text_path(self) -> Path:
        return self.root / SHEET_TEXT_FILENAME

    def artifact_path(self, model: type) -> Path:
        return self.root / _ARTIFACT_FILENAMES[model]

    def has(self, model: type) -> bool:
        return self.artifact_path(model).is_file()

    # -- artifact I/O ------------------------------------------------------

    def read(self, model: type[ArtifactT]) -> ArtifactT:
        """Load an artifact. Raises FileNotFoundError if the stage hasn't run."""
        path = self.artifact_path(model)
        with path.open("r", encoding="utf-8") as handle:
            return model.model_validate(json.load(handle))

    def read_optional(self, model: type[ArtifactT]) -> ArtifactT | None:
        try:
            return self.read(model)
        except (FileNotFoundError, ValueError):
            return None

    def write(self, doc: ArtifactT) -> None:
        path = self.artifact_path(type(doc))
        _atomic_write(path, doc.model_dump_json(indent=2))

    # -- manifest ----------------------------------------------------------

    def read_manifest(self) -> Manifest:
        try:
            with self.manifest_path.open("r", encoding="utf-8") as handle:
                return Manifest.model_validate(json.load(handle))
        except (FileNotFoundError, ValueError):
            return Manifest(song_id=self.song_id)

    def write_manifest(self, manifest: Manifest) -> None:
        manifest.updated_at = utcnow()
        _atomic_write(self.manifest_path, manifest.model_dump_json(indent=2))

    def update_stage(self, number: int, state, message: str = "", error: str = "") -> Manifest:
        """Record a stage transition in the manifest and persist it."""
        manifest = self.read_manifest()
        manifest.stage(number).mark(state, message=message, error=error)
        self.write_manifest(manifest)
        return manifest

    # -- text output -------------------------------------------------------

    def write_sheet_text(self, text: str) -> None:
        _atomic_write(self.sheet_text_path, text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SongProject({self.song_id!r})"


def summarise(project: SongProject) -> dict:
    """Compact description of a project for the library listing."""
    manifest = project.read_manifest()
    return {
        "song_id": project.song_id,
        "title": manifest.title,
        "artist": manifest.artist,
        "language": manifest.language,
        "updated_at": manifest.updated_at,
        "has_audio": project.audio_path.is_file(),
        "stages": {
            name: manifest.stages.get(name).state if manifest.stages.get(name) else "pending"
            for name in STAGE_NAMES
        },
    }
