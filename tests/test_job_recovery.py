import stat
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from webex_obs.config import Config


@pytest.fixture
def context(tmp_path, speech_modules, monkeypatch):
    # The speech fixture imports consumers without Metal/model downloads.
    from webex_obs.job_store import JobStore
    from webex_obs.webex_client import WebexClient
    from webex_obs.cleaner import MediaCleaner

    monkeypatch.setattr(MediaCleaner, "prune_old_recordings", Mock())
    monkeypatch.setattr("webex_obs.transcriber.shutil.which", lambda _: "/synthetic/ffmpeg")
    media = tmp_path / "synthetic.mkv"
    media.write_bytes(b"synthetic media")
    config = Config(_env_file=None, webex_access_token="synthetic-secret", hf_token="synthetic-hf",
                    webex_recipient_email="person@example.com", enable_diarization=False,
                    transcripts_dir=tmp_path / "transcripts", log_dir=tmp_path / "state")
    store = JobStore(config.log_dir / "jobs")
    job = speech_modules["PostProcessingJob"](
        media_files=(str(media),), meeting_title="Synthetic meeting", output_stem="synthetic",
        transcriber=speech_modules["Transcriber"]("test-model", config.transcripts_dir, False),
        webex=WebexClient(config.webex_token, config.webex_recipient_email),
        delivery_enabled=True, recordings_dir=tmp_path, retention_days=14, journal=store,
    )
    store.create(job)
    app = speech_modules["WebexOBSDaemon"].__new__(speech_modules["WebexOBSDaemon"])
    app._initialize_lifecycle_state()
    app.ui = Mock()
    app.config = config
    app.deliver_transcript = Mock(return_value=True)
    return app, store, job, config


@pytest.mark.parametrize("state", ["queued", "transcribing", "ready", "delivered"])
def test_restart_recovers_safe_work_without_duplicate_delivery(context, state):
    app, store, job, config = context
    record = store.read(job.job_id)
    transcript = Path(record["transcript"])
    if state in {"ready", "delivered"}:
        transcript.parent.mkdir()
        transcript.write_text("Synthetic complete transcript")
    store.update(job.job_id, state=state)
    recovered = store.recover(config)
    assert len(recovered) == 1
    app._process_post_processing_job(recovered[0])
    assert store.read(job.job_id)["state"] == "complete"
    assert app.deliver_transcript.call_count == (state != "delivered")
    assert transcript.exists()
    assert store.recover(config) == []


@pytest.mark.parametrize("state", ["delivering", "uncertain", "failed", "blocked"])
def test_restart_holds_uncertain_and_failed_jobs_for_review(context, state):
    app, store, job, config = context
    store.update(job.job_id, state=state)
    assert store.recover(config) == []
    assert store.read(job.job_id)["state"] == ("uncertain" if state == "delivering" else state)
    assert Path(job.media_files[0]).exists()
    app.deliver_transcript.assert_not_called()


@pytest.mark.parametrize("change", [{"webex_delivery_enabled": False}, {"webex_recipient_email": "changed@example.com"}])
def test_recovery_does_not_send_when_delivery_disabled_or_destination_changed(context, change):
    _, store, job, config = context
    assert store.recover(config.model_copy(update=change)) == []
    assert store.read(job.job_id)["state"] == "blocked"


def test_checkpoints_are_private_and_do_not_store_credentials(context):
    _, store, job, _ = context
    path = store.directory / f"{job.job_id}.json"
    assert "synthetic-secret" not in path.read_text()
    assert "synthetic-hf" not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700


def test_shutdown_during_inference_checkpoints_transcript_without_delivery(context):
    app, store, job, config = context
    target = Path(store.read(job.job_id)["transcript"])

    def transcribe(*args, **kwargs):
        target.parent.mkdir()
        target.write_text("Synthetic complete transcript")
        app._shutdown_event.set()
        return target

    job = replace(job, transcriber=Mock(transcribe_files=transcribe))
    app._process_post_processing_job(job)
    assert store.read(job.job_id)["state"] == "ready"
    app.deliver_transcript.assert_not_called()
    assert len(store.recover(config)) == 1


@pytest.mark.parametrize("outcome", ["uncertain", "rejected"])
def test_failed_network_outcome_is_persisted_and_not_retried(context, outcome):
    app, store, job, config = context
    app.deliver_transcript.return_value = False
    job.webex.last_delivery_outcome = outcome
    app._process_post_processing_job(job)
    assert store.read(job.job_id)["state"] == ("uncertain" if outcome == "uncertain" else "failed")
    assert store.recover(config) == []
    assert Path(job.media_files[0]).exists()


def test_crash_after_request_acceptance_is_held_instead_of_resent(context):
    app, store, job, config = context

    def send(*args, **kwargs):
        assert store.read(job.job_id)["state"] == "delivering"
        raise SystemExit("synthetic process termination after server acceptance")

    app.deliver_transcript.side_effect = send
    with pytest.raises(SystemExit):
        app._process_post_processing_job(job)
    assert store.recover(config) == []
    assert store.read(job.job_id)["state"] == "uncertain"


def test_shutdown_does_not_drain_queued_jobs_and_recovery_keeps_them(context, speech_modules):
    app, store, first, config = context
    entered, release = threading.Event(), threading.Event()
    second = replace(first, job_id="a" * 32, output_stem="second")

    def process(job):
        entered.set()
        assert release.wait(2)

    worker = speech_modules["PostProcessingWorker"](process)
    worker.submit(first)
    assert entered.wait(2)
    worker.submit(second)
    try:
        assert not worker.shutdown(wait=True, timeout=0.02, drain=False)
        with pytest.raises(RuntimeError, match="shutting down"):
            worker.submit(second)
        assert len(store.recover(config)) == 2
    finally:
        release.set()
        assert worker.shutdown(wait=True)


def test_failed_checkpoint_write_preserves_prior_state(context, monkeypatch):
    _, store, job, _ = context
    monkeypatch.setattr("webex_obs.durable_state.os.replace", Mock(side_effect=OSError("synthetic failure")))
    with pytest.raises(OSError):
        store.update(job.job_id, state="delivering")
    assert store.read(job.job_id)["state"] == "queued"
    assert not list(store.directory.glob(".webex-obs-*"))


def test_changed_media_is_not_processed_or_delivered(context):
    app, store, job, _ = context
    Path(job.media_files[0]).write_bytes(b"synthetic replacement")
    app._process_post_processing_job(job)
    assert store.read(job.job_id)["state"] == "failed"
    app.deliver_transcript.assert_not_called()


def test_recovery_rewrites_interrupted_transcript_at_same_path(context):
    app, store, job, config = context
    record = store.read(job.job_id)
    target = Path(record["transcript"])
    target.parent.mkdir()
    target.write_text("Synthetic committed output before interrupted checkpoint")
    store.update(job.job_id, state="transcribing")
    recovered = store.recover(config)
    app._process_post_processing_job(recovered[0])
    assert store.read(job.job_id)["state"] == "complete"
    assert list(target.parent.glob("*.txt")) == [target]
    assert "Discussed the roadmap" in target.read_text()


def test_jobs_with_same_stem_have_distinct_transcript_targets(context):
    _, store, first, _ = context
    second = replace(first, job_id="b" * 32)
    store.create(second)
    assert store.read(first.job_id)["transcript"] != store.read(second.job_id)["transcript"]


def test_checkpoint_updates_do_not_reorder_recovered_queue(context):
    _, store, first, config = context
    second = replace(first, job_id="c" * 32)
    store.create(second)
    store.update(first.job_id, state="transcribing")
    assert [job.job_id for job in store.recover(config)] == [first.job_id, second.job_id]


def test_partial_transcription_checkpoint_blocks_delivery_and_retention(context, speech_modules, monkeypatch):
    from webex_obs.cleaner import MediaCleaner

    app, store, original, config = context
    second = Path(original.media_files[0]).with_name("synthetic-second.mkv")
    second.write_bytes(b"synthetic second segment")
    job = replace(original, job_id="a" * 32, media_files=(*original.media_files, str(second)))
    store.create(job)
    speech_modules["inference"].side_effect = [
        {"text": "Synthetic first speech", "segments": []}, RuntimeError("synthetic failure"),
    ]
    eligible = Mock()
    monkeypatch.setattr(MediaCleaner, "mark_processed", eligible)
    app._process_post_processing_job(job)
    record = store.read(job.job_id)
    assert record["state"] == "failed"
    assert Path(record["transcript"]).read_text().startswith("# PARTIAL TRANSCRIPT — INCOMPLETE")
    assert all(Path(f).exists() for f in job.media_files)
    app.deliver_transcript.assert_not_called()
    eligible.assert_not_called()
    assert all(recovered.job_id != job.job_id for recovered in store.recover(config))
