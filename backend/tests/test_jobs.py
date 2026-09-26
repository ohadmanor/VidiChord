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
