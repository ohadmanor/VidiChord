"""HTTP API and static hosting for the Angular app.

Replaces the hand-rolled ``http.server`` handler. Three things that were broken
before are handled properly here:

* **Range requests.** Audio is streamed with byte-range support, so seeking in
  a five-minute WAV works and the whole file is never read into memory.
* **Path confinement.** Audio is only served from inside the library, and song
  ids are resolved through :meth:`SongProject.open`, which rejects traversal.
* **CORS.** Limited to the app's own origin and the Angular dev server, instead
  of ``*`` on every response - which let any page you visited read local files.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterator

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import __version__
from .chords.cleanup import CleanupConfig
from .chords.fusion import FusionConfig
from .config import DEFAULT_SHEETS_DIR, PORT, Settings, frontend_dir
from .jobs import JobManager
from .models import STEM_NAMES, ChordsDoc, LyricsDoc, SheetDoc, SourceDoc, StemsDoc
# REVIEW_FINAL_STAGE is stage 3: stage 4 lays the sheet out from the lyrics and
# the chords, so running it before either has been corrected wastes the work -
# the sheet is built on demand instead, by POST /api/songs/{id}/sync. It lives
# in the pipeline, which needs it to work out how far a run should go.
from .pipeline import REVIEW_FINAL_STAGE, StageContext, run_stage, stages_from
from .pipeline import stage1_audio
from .project import SongProject, summarise
from .sheet import export

_ALLOWED_ORIGINS = [
    f"http://localhost:{PORT}",
    f"http://127.0.0.1:{PORT}",
    # Angular dev server.
    "http://localhost:4200",
    "http://127.0.0.1:4200",
]

_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 1 << 18

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CreateSongRequest(BaseModel):
    url: str | None = None
    path: str | None = None
    language: str | None = None
    fusion: FusionConfig | None = None
    cleanup: CleanupConfig | None = None
    #: Stop after the chords so the lyrics and chords can be corrected before
    #: the sheet is laid out from them. See :data:`REVIEW_FINAL_STAGE`.
    review: bool = False


class LyricsChoiceRequest(BaseModel):
    #: "ai" to accept the transcript, "manual" to use pasted lyrics, and
    #: "instrumental" for a song that has no lyrics to find.
    choice: str = "ai"
    lyrics: str = ""
    language: str | None = None
    fusion: FusionConfig | None = None
    cleanup: CleanupConfig | None = None
    review: bool = False


class RerunRequest(BaseModel):
    language: str | None = None
    fusion: FusionConfig | None = None
    cleanup: CleanupConfig | None = None
    retranscribe: bool = False
    force: bool = False
    #: Continue through the later stages after this one.
    cascade: bool = True
    review: bool = False


class SettingsRequest(BaseModel):
    library_dir: str = ""
    sheets_dir: str = ""
    #: None means "leave as it is", so saving settings from a client that does
    #: not know about these fields cannot wipe them.
    cookies_file: str | None = None
    cookies_browser: str | None = None
    stems_enabled: bool | None = None
    stems_model: str | None = None


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(title="VidiChord", version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.settings = Settings.load()
    app.state.jobs = JobManager()

    def settings() -> Settings:
        return app.state.settings

    def jobs() -> JobManager:
        return app.state.jobs

    def open_project(song_id: str) -> SongProject:
        try:
            return SongProject.open(settings().library_dir, song_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"No such song: {song_id}. It is not in the library any more - it may "
                "have been deleted. Open the library to choose another.",
            )

    def make_context_builder(project: SongProject, params: dict):
        def build(report):
            return StageContext(
                project=project, settings=settings(), _report=report, params=params
            )

        return build

    def pipeline_params(request: Any) -> dict:
        """Collect the stage parameters carried on a request body."""
        params: dict = {}
        for name in ("language", "retranscribe", "force"):
            value = getattr(request, name, None)
            if value is not None:
                params[name] = value
        if getattr(request, "fusion", None) is not None:
            params["fusion"] = request.fusion
        if getattr(request, "cleanup", None) is not None:
            params["cleanup"] = request.cleanup
        return params

    # -- library -----------------------------------------------------------

    @app.get("/api/songs")
    def list_songs() -> list[dict]:
        return [summarise(p) for p in SongProject.iter_all(settings().library_dir)]

    @app.post("/api/songs", status_code=201)
    def create_song(request: CreateSongRequest) -> dict:
        if not request.url and not request.path:
            raise HTTPException(status_code=400, detail="Provide either url or path")

        try:
            if request.url:
                project = stage1_audio.prepare_from_youtube(request.url, settings())
            else:
                project = stage1_audio.prepare_from_file(Path(request.path), settings())
        except Exception as error:
            raise HTTPException(status_code=400, detail=str(error))

        # The names and length, so the app can say what it found before any
        # stage has run: the lyrics that carry them later do not exist yet.
        # Read before the job starts, not after - its first act is to replace
        # manifest.json, and on Windows a replace fails with "access denied"
        # while anyone has the file open to read it.
        manifest = project.read_manifest()
        source = project.read_optional(SourceDoc)

        params = pipeline_params(request)
        job = jobs().submit(
            project.song_id,
            stages_from(1, request.review),
            make_context_builder(project, params),
        )
        return {
            "song_id": project.song_id,
            "title": manifest.title,
            "artist": manifest.artist,
            "duration": source.duration if source else 0.0,
            "job": job.snapshot(),
        }

    @app.get("/api/songs/{song_id}")
    def get_song(song_id: str) -> dict:
        project = open_project(song_id)
        job = jobs().for_song(song_id)
        return {
            **summarise(project),
            "manifest": project.read_manifest().model_dump(),
            "job": job.snapshot() if job else None,
        }

    @app.delete("/api/songs/{song_id}", status_code=204)
    def delete_song(song_id: str) -> Response:
        import shutil

        project = open_project(song_id)
        shutil.rmtree(project.root, ignore_errors=True)
        return Response(status_code=204)

    # -- artifacts ---------------------------------------------------------

    def _sheet_is_behind(project: SongProject) -> bool:
        """Whether the sheet was laid out before the lyrics or chords last changed.

        Re-running stage 2 or 3 leaves the sheet on disk, still marked done,
        with the words it was built from - the same test the app makes before
        opening a song on its sheet.
        """
        stages = project.read_manifest().stages
        sheet = stages.get("sheet")
        if sheet is None or not sheet.updated_at:
            return False
        return any(
            name in stages and stages[name].updated_at > sheet.updated_at
            for name in ("lyrics", "chords")
        )

    def _read_artifact(project: SongProject, model):
        document = project.read_optional(model)
        if document is None:
            raise HTTPException(
                status_code=404, detail=f"{model.__name__} has not been produced yet"
            )
        return document

    @app.get("/api/songs/{song_id}/source")
    def get_source(song_id: str) -> SourceDoc:
        return _read_artifact(open_project(song_id), SourceDoc)

    @app.get("/api/songs/{song_id}/stems")
    def get_stems(song_id: str) -> StemsDoc:
        """What separation produced - or, with ``unavailable`` set, why it did not."""
        return _read_artifact(open_project(song_id), StemsDoc)

    @app.get("/api/songs/{song_id}/lyrics")
    def get_lyrics(song_id: str) -> LyricsDoc:
        return _read_artifact(open_project(song_id), LyricsDoc)

    @app.put("/api/songs/{song_id}/lyrics")
    def put_lyrics(song_id: str, document: LyricsDoc, rebuild: bool = True) -> dict:
        """Save edited lyrics, rebuilding the sheet from them unless told not to.

        The review editor passes ``rebuild=false``: it saves the lyrics and the
        chords one after the other and lays the sheet out once, rather than
        twice off half-corrected input.
        """
        project = open_project(song_id)
        project.write(document)
        if rebuild:
            _rebuild_sheet(project)
        return {"status": "ok"}

    @app.get("/api/songs/{song_id}/chords")
    def get_chords(song_id: str) -> ChordsDoc:
        return _read_artifact(open_project(song_id), ChordsDoc)

    @app.put("/api/songs/{song_id}/chords")
    def put_chords(song_id: str, document: ChordsDoc, rebuild: bool = True) -> dict:
        """Save edited chords, rebuilding the sheet unless told not to.

        See :func:`put_lyrics` for why the review editor suppresses the rebuild.
        """
        project = open_project(song_id)
        project.write(document)
        if rebuild:
            _rebuild_sheet(project)
        return {"status": "ok"}

    @app.get("/api/songs/{song_id}/sheet")
    def get_sheet(song_id: str) -> SheetDoc:
        return _read_artifact(open_project(song_id), SheetDoc)

    @app.put("/api/songs/{song_id}/sheet")
    def put_sheet(song_id: str, document: SheetDoc) -> dict:
        """Save a hand-edited sheet verbatim, without rebuilding it."""
        project = open_project(song_id)
        project.write(document)
        project.write_sheet_text(export.render_text(document))
        return {"status": "ok"}

    @app.post("/api/songs/{song_id}/sync")
    def sync_sheet(song_id: str) -> SheetDoc:
        """Lay the sheet out from the lyrics and chords as they stand.

        The end of the review step: stage 4 is cheap next to transcription and
        chord extraction, so it runs inline and the finished sheet comes back
        on this response rather than through a job.
        """
        project = open_project(song_id)
        _rebuild_sheet(project)
        return _read_artifact(project, SheetDoc)

    def _rebuild_sheet(project: SongProject) -> None:
        """Run stage 4 over the artifacts as they stand.

        Goes through ``run_stage`` rather than calling the stage directly so
        the manifest records the result: the library shows a pill per stage,
        and a sheet built here is as done as one built by a pipeline run.
        """
        context = StageContext(project=project, settings=settings())
        try:
            run_stage(4, context)
        except Exception as error:
            raise HTTPException(status_code=400, detail=f"Could not rebuild sheet: {error}")

    # -- stages ------------------------------------------------------------

    @app.post("/api/songs/{song_id}/stages/{number}/rerun")
    def rerun_stage(song_id: str, number: int, request: RerunRequest) -> dict:
        if number not in (1, 2, 3, 4, 5):
            raise HTTPException(status_code=400, detail="Stage must be 1-5")

        project = open_project(song_id)
        # Cascading follows the running order rather than counting upwards, so
        # re-separating a song carries on into the transcription that reads the
        # vocals stem. A reviewed re-run stops short of the sheet, but never
        # short of the stage that was actually asked for.
        stages = stages_from(number, request.review) if request.cascade else (number,)
        job = jobs().submit(
            song_id, stages, make_context_builder(project, pipeline_params(request))
        )
        return job.snapshot()

    @app.post("/api/songs/{song_id}/lyrics/choice")
    def lyrics_choice(song_id: str, request: LyricsChoiceRequest) -> dict:
        """Resume a run that paused because no lyrics could be found."""
        if request.choice not in ("ai", "manual", "instrumental"):
            raise HTTPException(
                status_code=400,
                detail="choice must be 'ai', 'manual' or 'instrumental'",
            )
        if request.choice == "manual" and not request.lyrics.strip():
            raise HTTPException(status_code=400, detail="No lyrics provided")

        project = open_project(song_id)
        params = pipeline_params(request)
        params["choice"] = request.choice
        params["lyrics"] = request.lyrics

        job = jobs().submit(
            song_id,
            stages_from(2, request.review),
            make_context_builder(project, params),
        )
        return job.snapshot()

    # -- jobs --------------------------------------------------------------

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = jobs().get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job")
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str) -> StreamingResponse:
        """Server-sent events carrying job progress until the run finishes."""
        manager = jobs()
        if manager.get(job_id) is None:
            raise HTTPException(status_code=404, detail="No such job")

        async def stream():
            import json

            seen = -1
            while True:
                job = await asyncio.to_thread(manager.wait_for_change, job_id, seen, 1.0)
                if job is None:
                    break
                if job.version != seen:
                    seen = job.version
                    yield f"data: {json.dumps(job.snapshot())}\n\n"
                if job.finished:
                    break

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- audio -------------------------------------------------------------

    @app.get("/api/songs/{song_id}/audio")
    def stream_audio(song_id: str, request: Request) -> Response:
        project = open_project(song_id)
        path = project.audio_path
        if not path.is_file():
            raise HTTPException(status_code=404, detail="No audio for this song")
        return _serve_media(path, request)

    @app.get("/api/songs/{song_id}/stems/{name}")
    def stream_stem(song_id: str, name: str, request: Request) -> Response:
        """Stream one separated part, for the player to mix.

        The name comes out of the URL, so it is matched against the four the
        separator produces rather than joined onto a path, and the document
        decides the file - which is also what keeps the format an
        implementation detail the client never has to guess at.
        """
        project = open_project(song_id)
        if name not in STEM_NAMES:
            raise HTTPException(status_code=404, detail=f"No such stem: {name}")

        document = project.read_optional(StemsDoc)
        relative = document.path_for(name) if document else ""
        if not relative:
            raise HTTPException(status_code=404, detail=f"No {name} stem for this song")
        try:
            path = project.stem_path(relative)
        except KeyError:
            raise HTTPException(status_code=403, detail="Refusing to serve that path")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"No {name} stem for this song")
        return _serve_media(path, request)

    def _serve_media(path: Path, request: Request) -> Response:
        """Serve a file from the library, honouring byte ranges.

        Range support is what makes seeking in a long file work without the
        whole thing being read into memory, and the confinement check is what
        stops the server being talked into serving anything else on the disk.
        """
        if not settings().is_inside_library(path):
            raise HTTPException(status_code=403, detail="Refusing to serve that path")

        media_type = mimetypes.guess_type(str(path))[0] or "audio/wav"
        size = path.stat().st_size

        header = request.headers.get("range")
        match = _RANGE.match(header) if header else None
        if not match:
            return FileResponse(path, media_type=media_type)

        start = int(match.group(1)) if match.group(1) else 0
        end = int(match.group(2)) if match.group(2) else size - 1
        start = max(0, min(start, size - 1))
        end = max(start, min(end, size - 1))

        def chunks() -> Iterator[bytes]:
            remaining = end - start + 1
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    block = handle.read(min(_CHUNK, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block

        return StreamingResponse(
            chunks(),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes",
            },
        )

    # -- export ------------------------------------------------------------

    @app.post("/api/songs/{song_id}/export")
    def export_song(song_id: str) -> dict:
        project = open_project(song_id)
        try:
            sheet = project.read_optional(SheetDoc)
        except OSError as error:
            # Held open by another program, or a disk that has gone away.
            raise HTTPException(
                status_code=409,
                detail=f"The song sheet could not be read ({error.strerror or error}). "
                "Close anything that has it open, then export again.",
            )
        if sheet is None:
            raise HTTPException(
                status_code=409,
                detail="This song has no song sheet yet. Sync it first, then export.",
            )
        if not export.render_text(sheet).strip():
            # Would reach songbook as a song with no words and no chords.
            raise HTTPException(
                status_code=409,
                detail="This song sheet is empty, so there is nothing to export. Sync it again first.",
            )
        if _sheet_is_behind(project):
            # Lyrics or chords re-run or corrected since the sheet was laid
            # out: exporting now would send words the song no longer has.
            raise HTTPException(
                status_code=409,
                detail="The song sheet is older than the latest lyrics or chords. "
                "Sync it first, so the export has them.",
            )

        # The chosen folder, or the default beside the library: created if
        # it is not there yet, so a fresh install exports without being set up.
        folder = settings().sheets_folder
        try:
            destination = export.write_export(folder, sheet, song_id)
        except export.ExportError as error:
            raise HTTPException(status_code=400, detail=str(error))
        except Exception as error:
            # Anything else still answers in words, as JSON: a bare
            # "Internal Server Error" tells the user nothing.
            raise HTTPException(
                status_code=500, detail=f"The export failed unexpectedly: {error}"
            )

        return {
            "status": "ok",
            "filename": destination.name,
            "path": str(destination),
            "folder": str(folder),
        }

    # -- settings ----------------------------------------------------------

    def config_view(current: Settings) -> dict:
        """The settings, plus where exports go when no folder has been chosen."""
        return {**current.to_dict(), "sheets_dir_default": str(DEFAULT_SHEETS_DIR)}

    @app.get("/api/config")
    def get_config() -> dict:
        return config_view(settings())

    @app.put("/api/config")
    def put_config(request: SettingsRequest) -> dict:
        current = settings()
        data = request.model_dump()
        for name in ("cookies_file", "cookies_browser", "stems_enabled", "stems_model"):
            if data.get(name) is None:
                data[name] = current.to_dict()[name]
        # Keep writing to wherever the current settings came from, so a test
        # or an alternate install never writes over the user's config file.
        updated = Settings.from_dict(data, path=current.path)
        # A songbook folder that cannot be used is said so here, in Settings,
        # rather than at the next export. One that does not exist yet is
        # simply made - that is what anyone typing a new folder wants.
        if updated.sheets_dir is not None:
            if updated.sheets_dir.exists() and not updated.sheets_dir.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail=f"The songbook folder {updated.sheets_dir} is a file, not a folder.",
                )
            try:
                updated.sheets_dir.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise HTTPException(
                    status_code=400,
                    detail=f"The songbook folder {updated.sheets_dir} could not be created: "
                    f"{error.strerror or error}.",
                )
        try:
            updated.save()
        except OSError as error:
            # Nothing was changed. Say which of the two it was, in words.
            reason = error.strerror or str(error)
            if not updated.library_dir.is_dir():
                detail = f"The library folder {updated.library_dir} could not be created: {reason}."
            else:
                detail = (
                    f"The settings could not be saved to {updated.path}: {reason}. If "
                    "VidiChord is in a folder you cannot write to, such as Program "
                    "Files, move it somewhere you can."
                )
            raise HTTPException(status_code=400, detail=detail)
        app.state.settings = updated
        return config_view(updated)

    # -- the Angular app ---------------------------------------------------

    _register_frontend(app)
    return app


def _register_frontend(app: FastAPI) -> None:
    """Serve the built Angular app, falling back to index.html for routes."""
    root = frontend_dir()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        # An unmatched API route is a 404, not the single-page app.
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"No such endpoint: /{path}")
        if root is None:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "The frontend has not been built. "
                    "Run 'npm run build' in the frontend directory."
                },
            )

        candidate = (root / path).resolve() if path else root / "index.html"
        # Never serve outside the built app, whatever the path says.
        if root.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(root / "index.html")


app = create_app()
