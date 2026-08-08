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
from .config import PORT, Settings, frontend_dir
from .jobs import JobManager
from .models import ChordsDoc, LyricsDoc, SheetDoc, SourceDoc
from .pipeline import StageContext
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


class LyricsChoiceRequest(BaseModel):
    #: "ai" to accept the transcript, "manual" to use pasted lyrics.
    choice: str = "ai"
    lyrics: str = ""
    language: str | None = None
    fusion: FusionConfig | None = None
    cleanup: CleanupConfig | None = None


class RerunRequest(BaseModel):
    language: str | None = None
    fusion: FusionConfig | None = None
    cleanup: CleanupConfig | None = None
    retranscribe: bool = False
    force: bool = False
    #: Continue through the later stages after this one.
    cascade: bool = True


class SettingsRequest(BaseModel):
    library_dir: str = ""
    sheets_dir: str = ""


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
            raise HTTPException(status_code=404, detail=f"No such song: {song_id}")

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

        params = pipeline_params(request)
        job = jobs().submit(
            project.song_id, (1, 2, 3, 4), make_context_builder(project, params)
        )
        return {"song_id": project.song_id, "job": job.snapshot()}

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

    @app.get("/api/songs/{song_id}/lyrics")
    def get_lyrics(song_id: str) -> LyricsDoc:
        return _read_artifact(open_project(song_id), LyricsDoc)

    @app.put("/api/songs/{song_id}/lyrics")
    def put_lyrics(song_id: str, document: LyricsDoc) -> dict:
        """Save edited lyrics and rebuild the sheet from them."""
        project = open_project(song_id)
        project.write(document)
        _rebuild_sheet(project)
        return {"status": "ok"}

    @app.get("/api/songs/{song_id}/chords")
    def get_chords(song_id: str) -> ChordsDoc:
        return _read_artifact(open_project(song_id), ChordsDoc)

    @app.put("/api/songs/{song_id}/chords")
    def put_chords(song_id: str, document: ChordsDoc) -> dict:
        """Save edited chords and rebuild the sheet from them."""
        project = open_project(song_id)
        project.write(document)
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

    def _rebuild_sheet(project: SongProject) -> None:
        from .pipeline import stage4_sheet

        context = StageContext(project=project, settings=settings())
        try:
            stage4_sheet.run(context)
        except Exception as error:
            raise HTTPException(status_code=400, detail=f"Could not rebuild sheet: {error}")

    # -- stages ------------------------------------------------------------

    @app.post("/api/songs/{song_id}/stages/{number}/rerun")
    def rerun_stage(song_id: str, number: int, request: RerunRequest) -> dict:
        if number not in (1, 2, 3, 4):
            raise HTTPException(status_code=400, detail="Stage must be 1-4")

        project = open_project(song_id)
        stages = tuple(range(number, 5)) if request.cascade else (number,)
        job = jobs().submit(
            song_id, stages, make_context_builder(project, pipeline_params(request))
        )
        return job.snapshot()

    @app.post("/api/songs/{song_id}/lyrics/choice")
    def lyrics_choice(song_id: str, request: LyricsChoiceRequest) -> dict:
        """Resume a run that paused because no lyrics could be found."""
        if request.choice not in ("ai", "manual"):
            raise HTTPException(status_code=400, detail="choice must be 'ai' or 'manual'")
        if request.choice == "manual" and not request.lyrics.strip():
            raise HTTPException(status_code=400, detail="No lyrics provided")

        project = open_project(song_id)
        params = pipeline_params(request)
        params["choice"] = request.choice
        params["lyrics"] = request.lyrics

        job = jobs().submit(song_id, (2, 3, 4), make_context_builder(project, params))
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
        sheet = _read_artifact(project, SheetDoc)

        target = settings().sheets_dir
        if target is None or not target.is_dir():
            raise HTTPException(
                status_code=400,
                detail="The sheets directory is not configured. Set it in Settings.",
            )

        import json

        filename = export.safe_filename(sheet.title, sheet.artist)
        destination = target / filename
        with destination.open("w", encoding="utf-8") as handle:
            json.dump(export.songbook_payload(sheet), handle, indent=2, ensure_ascii=False)

        return {"status": "ok", "filename": filename, "path": str(destination)}

    # -- settings ----------------------------------------------------------

    @app.get("/api/config")
    def get_config() -> dict:
        return settings().to_dict()

    @app.put("/api/config")
    def put_config(request: SettingsRequest) -> dict:
        updated = Settings.from_dict(request.model_dump())
        updated.save()
        app.state.settings = updated
        return updated.to_dict()

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
