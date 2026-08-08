"""Background pipeline runs and their progress.

Work happens on worker threads because the pipeline is CPU-bound and blocking:
transcription and chord extraction both take minutes. Each run gets its own
``Job`` with an independent progress record, replacing the single module-level
status dict that previously limited the app to one song at a time.

Progress is versioned so a reader can block until something actually changes
rather than re-sending an unchanged snapshot every second.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .models import StageState, utcnow
from .pipeline import STAGE_LABELS, NeedsUserInput, StageContext, run_stage

#: Terminal job states - no further progress will be reported.
_FINISHED = {"done", "failed", "needs_input"}


@dataclass
class Job:
    """One pipeline run over one song."""

    job_id: str
    song_id: str
    stages: tuple[int, ...]
    state: str = "queued"
    stage: int = 0
    message: str = ""
    percent: float = 0.0
    error: str = ""
    #: Choices the user must pick between when ``state`` is "needs_input".
    options: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    version: int = 0

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "song_id": self.song_id,
            "state": self.state,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, ""),
            "message": self.message,
            "percent": round(self.percent, 2),
            "error": self.error,
            "options": list(self.options),
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @property
    def finished(self) -> bool:
        return self.state in _FINISHED


class JobManager:
    """Owns every job and the threads running them."""

    def __init__(self, max_history: int = 50) -> None:
        self._jobs: dict[str, Job] = {}
        self._condition = threading.Condition()
        self._max_history = max_history

    # -- reading -----------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._condition:
            return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        with self._condition:
            return [job.snapshot() for job in self._jobs.values()]

    def for_song(self, song_id: str) -> Job | None:
        """Most recent job for a song, if any."""
        with self._condition:
            matching = [job for job in self._jobs.values() if job.song_id == song_id]
        return matching[-1] if matching else None

    def wait_for_change(self, job_id: str, since: int, timeout: float = 1.0) -> Job | None:
        """Block until a job's version passes ``since``, or the timeout lapses."""
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.version > since:
                return job
            self._condition.wait(timeout)
            return self._jobs.get(job_id)

    # -- writing -----------------------------------------------------------

    def _update(self, job: Job, **changes) -> None:
        with self._condition:
            for name, value in changes.items():
                setattr(job, name, value)
            job.updated_at = utcnow()
            job.version += 1
            self._condition.notify_all()

    def _prune(self) -> None:
        if len(self._jobs) <= self._max_history:
            return
        # Drop the oldest finished jobs first.
        finished = [job_id for job_id, job in self._jobs.items() if job.finished]
        for job_id in finished[: len(self._jobs) - self._max_history]:
            del self._jobs[job_id]

    # -- running -----------------------------------------------------------

    def submit(
        self,
        song_id: str,
        stages: tuple[int, ...],
        build_context: Callable[[Callable[[str, float | None], None]], StageContext],
    ) -> Job:
        """Queue a pipeline run and start it on a worker thread.

        ``build_context`` receives the progress reporter and returns the
        :class:`StageContext` the stages should run with.
        """
        job = Job(job_id=uuid.uuid4().hex, song_id=song_id, stages=tuple(stages))
        with self._condition:
            self._jobs[job.job_id] = job
            self._prune()

        thread = threading.Thread(
            target=self._run, args=(job, build_context), daemon=True,
            name=f"vidichord-job-{job.job_id[:8]}",
        )
        thread.start()
        return job

    def _run(self, job: Job, build_context) -> None:
        def report(message: str, percent: float | None) -> None:
            changes: dict = {"message": message}
            if percent is not None:
                changes["percent"] = self._overall_percent(job, percent)
            self._update(job, **changes)

        try:
            context = build_context(report)
        except Exception as error:
            traceback.print_exc()
            self._update(job, state="failed", error=str(error))
            return

        self._update(job, state="running")
        for number in job.stages:
            self._update(
                job, stage=number, message=f"Stage {number}: {STAGE_LABELS[number]}"
            )
            try:
                run_stage(number, context)
            except NeedsUserInput as pause:
                self._update(
                    job, state="needs_input", message=str(pause), options=pause.options
                )
                return
            except Exception as error:
                traceback.print_exc()
                self._update(job, state="failed", error=str(error))
                return

        self._update(job, state="done", percent=100.0, message="Complete.")

    @staticmethod
    def _overall_percent(job: Job, stage_percent: float) -> float:
        """Map progress within a stage onto progress through the whole run."""
        if not job.stages:
            return stage_percent
        try:
            position = job.stages.index(job.stage)
        except ValueError:
            position = 0
        span = 100.0 / len(job.stages)
        return min(100.0, position * span + (stage_percent / 100.0) * span)


def stage_states(manifest) -> dict[str, str]:
    """Flatten a manifest's stage records into plain strings."""
    return {
        name: status.state.value if isinstance(status.state, StageState) else str(status.state)
        for name, status in manifest.stages.items()
    }
