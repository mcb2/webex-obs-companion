from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def speech_components(speech_modules, monkeypatch):
    monkeypatch.setitem(globals(), "PostProcessingJob", speech_modules["PostProcessingJob"])
    monkeypatch.setitem(globals(), "PostProcessingWorker", speech_modules["PostProcessingWorker"])


def make_job(title: str) -> PostProcessingJob:
    return PostProcessingJob(
        media_files=(f"{title}.mkv",),
        meeting_title=title,
        output_stem=title,
        transcriber=Mock(),
        webex=Mock(),
        delivery_enabled=True,
        recordings_dir=Path("/tmp/recordings"),
        retention_days=14,
    )


def test_worker_processes_jobs_in_fifo_order_and_survives_job_failure():
    processed = []

    def process(job):
        processed.append(job.meeting_title)
        if job.meeting_title == "second":
            raise RuntimeError("test failure")

    worker = PostProcessingWorker(process)
    worker.submit(make_job("first"))
    worker.submit(make_job("second"))
    worker.submit(make_job("third"))
    worker.shutdown(wait=True)

    assert processed == ["first", "second", "third"]


def test_unexpected_worker_exit_reports_fatal_failure():
    import threading
    failed = threading.Event()

    def fatal(job):
        raise SystemExit("synthetic worker termination")

    worker = PostProcessingWorker(fatal, on_fatal=failed.set)
    worker.submit(make_job("synthetic"))
    assert failed.wait(2)
    assert worker.shutdown(wait=True)
