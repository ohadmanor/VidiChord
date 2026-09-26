"""The job manager: one run per song at a time.

Two runs over one song's folder would download into the same file and
overwrite each other's artifacts. A double click on "Extract & Align" is
enough to ask for that - more so when adding a song waits on a yt-dlp update.
"""

import threading

import pytest

from vidichord import jobs
from vidichord.jobs import JobManager


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """These jobs end by failing on purpose; their tracebacks are noise."""
    monkeypatch.setattr(jobs.traceback, "print_exc", lambda: None)


def _held_run(release: threading.Event):
    """A build_context that keeps its job 'queued' until ``release`` is set."""

    def build(report):
        release.wait(5)
        raise RuntimeError("released")

    return build


def test_a_song_being_worked_on_gets_its_current_job_back():
    manager, release = JobManager(), threading.Event()
    try:
        first = manager.submit("song", (1,), _held_run(release))
        second = manager.submit("song", (1,), _held_run(release))

        assert second is first
        assert len(manager.list()) == 1
    finally:
        release.set()


def test_another_song_is_not_held_up():
    manager, release = JobManager(), threading.Event()
    try:
        first = manager.submit("one song", (1,), _held_run(release))
        second = manager.submit("another song", (1,), _held_run(release))

        assert second is not first
    finally:
        release.set()


def test_a_finished_song_can_be_run_again():
    manager = JobManager()

    def fail(report):
        raise RuntimeError("done at once")

    first = manager.submit("song", (1,), fail)
    for _ in range(50):
        if manager.get(first.job_id).finished:
            break
        threading.Event().wait(0.05)

    second = manager.submit("song", (1,), fail)

    assert second is not first


# -- the steps of a run -------------------------------------------------------
#
# Every stage a run will work through has its own record, so the app can show
# the whole road - done, running, next - with each step's own progress and
# result. A stage that takes milliseconds must still leave its mark: the event
# stream only ever sends the latest snapshot.

from vidichord.jobs import Job
from vidichord.pipeline import NeedsUserInput, StageContext


def _context(report):
    return StageContext(project=None, settings=None, _report=report)


def _finish(manager, job, timeout=5.0):
    deadline = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if manager.get(job.job_id).finished:
            break
        deadline.wait(0.02)
    return manager.get(job.job_id).snapshot()


def _states(snapshot):
    return [step["state"] for step in snapshot["steps"]]


def test_a_new_job_lists_every_stage_it_will_run():
    snapshot = Job(job_id="j", song_id="s", stages=(1, 5, 2, 3)).snapshot()

    assert snapshot["stages"] == [1, 5, 2, 3]
    assert [step["stage"] for step in snapshot["steps"]] == [1, 5, 2, 3]
    labels = [step["label"] for step in snapshot["steps"]]
    assert labels == ["Audio", "Stems", "Lyrics", "Chords"]
    assert _states(snapshot) == ["pending"] * 4
    assert all(step["elapsed"] is None for step in snapshot["steps"])
    assert snapshot["stage_percent"] is None


def test_each_step_keeps_its_outcome_and_its_result_line(monkeypatch):
    def run_stage(number, context):
        if number == 5:
            context.skip("Demucs is not installed.")
            return
        context.report("working...", 50.0)
        context.report(f"result of {number}.", 100.0)

    monkeypatch.setattr(jobs, "run_stage", run_stage)
    manager = JobManager()

    snapshot = _finish(manager, manager.submit("song", (1, 5, 2), _context))

    # The skip is per stage: the stage after it is done, not skipped too.
    assert _states(snapshot) == ["done", "skipped", "done"]
    assert [step["message"] for step in snapshot["steps"]] == [
        "result of 1.", "Demucs is not installed.", "result of 2.",
    ]
    assert all(step["percent"] == 100.0 for step in snapshot["steps"])
    assert all(step["elapsed"] is not None for step in snapshot["steps"])


def test_a_running_step_shows_its_own_percent_and_never_goes_back(monkeypatch):
    release, reported = threading.Event(), threading.Event()

    def run_stage(number, context):
        context.report("first pass", 50.0)
        context.report("second pass, starting over", 20.0)
        reported.set()
        release.wait(5)

    monkeypatch.setattr(jobs, "run_stage", run_stage)
    manager = JobManager()
    job = manager.submit("song", (1, 2), _context)
    try:
        assert reported.wait(5)
        snapshot = manager.get(job.job_id).snapshot()

        assert snapshot["stage_percent"] == 50.0
        assert _states(snapshot) == ["running", "pending"]
        assert snapshot["steps"][0]["message"] == "second pass, starting over"
        # The run as a whole: halfway through the first of two stages.
        assert snapshot["percent"] == 25.0
    finally:
        release.set()


def test_a_failed_step_carries_the_error(monkeypatch):
    def run_stage(number, context):
        if number == 2:
            raise RuntimeError("no network")

    monkeypatch.setattr(jobs, "run_stage", run_stage)
    manager = JobManager()

    snapshot = _finish(manager, manager.submit("song", (1, 2, 3), _context))

    assert _states(snapshot) == ["done", "failed", "pending"]
    assert snapshot["steps"][1]["message"] == "no network"
    assert snapshot["error"] == "no network"


def test_a_step_waiting_for_an_answer_says_what_it_asks(monkeypatch):
    def run_stage(number, context):
        if number == 2:
            raise NeedsUserInput("No lyrics found.", ["ai", "manual"])

    monkeypatch.setattr(jobs, "run_stage", run_stage)
    manager = JobManager()

    snapshot = _finish(manager, manager.submit("song", (1, 2, 3), _context))

    assert snapshot["state"] == "needs_input"
    assert _states(snapshot) == ["done", "needs_input", "pending"]
    assert snapshot["steps"][1]["message"] == "No lyrics found."


def test_a_run_that_cannot_start_leaves_every_step_pending():
    def broken(report):
        raise RuntimeError("no such song")

    manager = JobManager()

    snapshot = _finish(manager, manager.submit("song", (1, 2), broken))

    assert snapshot["state"] == "failed"
    assert _states(snapshot) == ["pending", "pending"]
