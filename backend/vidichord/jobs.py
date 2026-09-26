"""Background pipeline runs and their progress.

Work happens on worker threads because the pipeline is CPU-bound and blocking:
transcription and chord extraction both take minutes. Each run gets its own
``Job`` with an independent progress record, replacing the single module-level
status dict that previously limited the app to one song at a time.

Progress is versioned so a reader can block until something actually changes
rather than re-sending an unchanged snapshot every second.

Besides the run as a whole, a job keeps one record per stage it will run -
its state, its own percent, its last message and how long it took - so the
app can draw the whole road: what is done, what is running, what comes next.
A stage that finishes in milliseconds (separation, when it is switched off)
would otherwise never be seen at all, because the event stream only sends the
latest snapshot.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .models import StageState, utcnow
from .pipeline import STAGE_LABELS, NeedsUserInput, StageContext, run_stage

#: Terminal job states - no further progress will be reported.
_FINISHED = {"done", "failed", "needs_input"}


def _new_step(stage: int) -> dict:
    """The record of one scheduled stage that has not started.

    ``state`` is pending, running, done, skipped, failed or needs_input.
    ``started`` and ``ended`` are on the monotonic clock, so the elapsed times
    derived from them survive the wall clock being changed mid-run.
    """
    return {
        "stage": stage, "state": "pending", "percent": None, "message": "",
        "started": None, "ended": None,
    }


def _step_snapshot(record: dict, now: float) -> dict:
    started, ended = record["started"], record["ended"]
    return {
        "stage": record["stage"],
        "label": STAGE_LABELS.get(record["stage"], ""),
        "state": record["state"],
        "percent": None if record["percent"] is None else round(record["percent"], 1),
        "message": record["message"],
        #: Seconds so far, or in total once finished; None until it starts. In
        #: seconds rather than as timestamps, so the app parses no clocks.
        "elapsed": None if started is None
        else round((ended if ended is not None else now) - started, 1),
    }


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
    #: One record per scheduled stage, in running order (see ``_new_step``).
    #: A change replaces the tuple and the record it touches rather than
    #: editing them, because the event stream snapshots jobs outside the lock.
    steps: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        if not self.steps:
            self.steps = tuple(_new_step(number) for number in self.stages)

    def running_step(self) -> dict | None:
        return next((step for step in self.steps if step["state"] == "running"), None)

    def with_step(self, stage: int, **fields) -> tuple[dict, ...]:
        """``steps`` with one stage's record replaced by an updated copy."""
        return tuple(
            {**step, **fields} if step["stage"] == stage else step for step in self.steps
        )

    def snapshot(self) -> dict:
        now = time.monotonic()
        steps = self.steps  # read the reference once: it may be replaced meanwhile
        running = next((step for step in steps if step["state"] == "running"), None)
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
            "stages": list(self.stages),
            "stage_percent": None if running is None or running["percent"] is None
            else round(running["percent"], 1),
            "steps": [_step_snapshot(step, now) for step in steps],
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

        A song already being worked on gets its current job back rather than
        a second one: two runs over one folder would download into the same
        file and overwrite each other's artifacts. A double click does this -
        more easily when adding a song waits on a yt-dlp update.
        """
        job = Job(job_id=uuid.uuid4().hex, song_id=song_id, stages=tuple(stages))
        with self._condition:
            for existing in self._jobs.values():
                if existing.song_id == song_id and not existing.finished:
                    return existing
            self._jobs[job.job_id] = job
            self._prune()

        thread = threading.Thread(
            target=self._run, args=(job, build_context), daemon=True,
            name=f"vidichord-job-{job.job_id[:8]}",
        )
        thread.start()
        return job

    def _set_step(self, job: Job, which: int, step: dict, **changes) -> None:
        """Stage ``which``'s record, and any job fields with it, as one version."""
        # The condition wraps an RLock, so _update may take it again inside.
        with self._condition:
            self._update(job, steps=job.with_step(which, **step), **changes)

    def _run(self, job: Job, build_context) -> None:
        def report(message: str, percent: float | None) -> None:
            # Under the lock: stage 3 reports from its engine threads too.
            with self._condition:
                text = message.strip()
                changes: dict = {"message": text}
                step = job.running_step()
                if step is not None:
                    fields: dict = {"message": text}
                    if percent is not None:
                        # Never backwards within a stage: a retried download,
                        # or Whisper's second pass without the voice filter,
                        # restarts its own count - and a shrinking bar reads
                        # as lost work.
                        percent = max(step["percent"] or 0.0, min(100.0, float(percent)))
                        fields["percent"] = percent
                    changes["steps"] = job.with_step(step["stage"], **fields)
                if percent is not None:
                    changes["percent"] = self._overall_percent(job, percent)
                self._update(job, **changes)

        try:
            context = build_context(report)
        except Exception as error:
            traceback.print_exc()
            # Every step stays pending; the app pins the error on the first.
            self._update(job, state="failed", error=str(error))
            return

        self._update(job, state="running")
        for number in job.stages:
            context.skipped = ""
            self._set_step(
                job, number, {"state": "running", "started": time.monotonic()},
                stage=number, message=f"Stage {number}: {STAGE_LABELS[number]}",
            )
            try:
                run_stage(number, context)
            except NeedsUserInput as pause:
                ended = {"state": "needs_input", "message": str(pause), "ended": time.monotonic()}
                self._set_step(
                    job, number, ended,
                    state="needs_input", message=str(pause), options=pause.options,
                )
                return
            except Exception as error:
                traceback.print_exc()
                self._set_step(
                    job, number,
                    {"state": "failed", "message": str(error), "ended": time.monotonic()},
                    state="failed", error=str(error),
                )
                return
            # A finished step keeps its last message: that is its result line.
            skipped = getattr(context, "skipped", "")
            ended = {
                "state": "skipped" if skipped else "done",
                "percent": 100.0,
                "ended": time.monotonic(),
            }
            if skipped:
                ended["message"] = skipped
            self._set_step(job, number, ended)

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
