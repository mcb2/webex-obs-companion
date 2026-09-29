import sys
import types
from pathlib import Path
from unittest.mock import Mock

fake_mlx_whisper = types.ModuleType("mlx_whisper")
fake_mlx_whisper.transcribe = lambda *args, **kwargs: {"text": "", "segments": []}
sys.modules.setdefault("mlx_whisper", fake_mlx_whisper)

from webex_obs.post_processing import PostProcessingJob, PostProcessingWorker


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
