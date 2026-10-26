"""Lifecycle tests use synthetic files, injected backends, and no system services."""

import json
import threading
from unittest.mock import Mock

import pytest

from webex_obs.config import Config
from webex_obs.runtime_state import RuntimeState, application_health


@pytest.fixture
def daemon(tmp_path, speech_modules, monkeypatch):
    module = __import__("webex_obs.daemon", fromlist=["WebexOBSDaemon"])
    monkeypatch.setattr(module, "HotkeyListener", Mock())
    monkeypatch.setattr(module.MediaCleaner, "prune_old_recordings", Mock())
    config = Config(_env_file=None, log_dir=tmp_path / "state", recordings_dir=tmp_path / "media",
                    transcripts_dir=tmp_path / "text", enable_diarization=False,
                    webex_delivery_enabled=False, poll_interval=0.01)
    backend = Mock(is_recording=False, stop_verified=True)
    backend.initialize.return_value = True
    backend.stop_recording.return_value = []
    app = module.WebexOBSDaemon(config, backend=backend, ui=Mock())
    app.monitor = Mock(is_in_meeting=False, current_call_title=None, default_call_title="Synthetic meeting")
    yield app
    app.request_shutdown()
    if app._lifecycle_thread:
        app._lifecycle_thread.join(3)
    app._post_processing_worker.shutdown(wait=True, drain=False)
    app._runtime.release()


@pytest.mark.parametrize("failure_at", ["hotkeys", "initialize", "monitor", "returned"])
def test_startup_and_lifecycle_failures_are_visible_and_cleaned_up(daemon, failure_at):
    error = RuntimeError("synthetic failure")
    if failure_at == "hotkeys":
        daemon.hotkeys.start.side_effect = error
    elif failure_at == "initialize":
        daemon.recorder.initialize.side_effect = error
    elif failure_at == "monitor":
        daemon.monitor.wait_for_state_change.side_effect = error
    else:
        daemon._run_lifecycle = Mock()
    daemon.start()
    assert daemon.failure
    assert daemon.lifecycle_state == "failed"
    assert daemon._stopped_event.is_set()
    daemon.hotkeys.stop.assert_called_once()
    daemon.recorder.disconnect.assert_called_once()
    assert json.loads((daemon.config.log_dir / "health.json").read_text())["state"] == "failed"


def test_shutdown_wakes_idle_monitor_without_starting_capture(daemon):
    entered = threading.Event()

    def wait(event):
        entered.set()
        assert event.wait(2)
        return True

    daemon.monitor.wait_for_state_change.side_effect = wait
    daemon.start_background()
    assert entered.wait(2)
    assert daemon.shutdown(2)
    assert not daemon.failure
    daemon.recorder.start_recording.assert_not_called()
    daemon.recorder.stop_recording.assert_not_called()


@pytest.mark.parametrize("verified", [True, False])
def test_shutdown_active_capture_finalizes_only_after_verified_stop(daemon, verified):
    from webex_obs.session import RecordingSession

    daemon.config.recordings_dir.mkdir()
    media = daemon.config.recordings_dir / "synthetic.mkv"
    media.write_bytes(b"synthetic media")
    daemon._active_session = RecordingSession.create("Synthetic meeting")
    daemon.recorder.is_recording = True
    daemon._discard_requested = True  # Exit must retain media even during pending discard.

    def stop():
        daemon.recorder.stop_verified = verified
        daemon.recorder.is_recording = not verified
        return [str(media)]

    daemon.recorder.stop_recording.side_effect = stop
    daemon._run_lifecycle = daemon.request_shutdown
    daemon.start()
    daemon.recorder.stop_recording.assert_called_once()
    assert list(daemon.config.recordings_dir.glob("*.mkv"))
    records = list((daemon.config.log_dir / "jobs").glob("*.json"))
    assert len(records) == int(verified)
    if verified:
        assert json.loads(records[0].read_text())["state"] == "queued"
        assert daemon._active_session is None
        assert not daemon.failure
    else:
        assert daemon._active_session is not None
        assert "unresolved" in daemon.failure


def test_shutdown_is_bounded_and_never_stops_backend_concurrently(daemon):
    entered, release = threading.Event(), threading.Event()

    def initialize():
        entered.set()
        assert release.wait(3)
        return True

    daemon.recorder.initialize.side_effect = initialize
    daemon.start_background()
    assert entered.wait(2)
    try:
        assert not daemon.shutdown(0.02)
        daemon.recorder.stop_recording.assert_not_called()
        daemon.recorder.disconnect.assert_not_called()
        daemon._request_manual_start("audio")
        daemon.recorder.prepare_recording.assert_not_called()
    finally:
        release.set()
    assert daemon._stopped_event.wait(2)


def test_cleanup_failure_does_not_skip_other_cleanup(daemon):
    daemon._run_lifecycle = daemon.request_shutdown
    daemon.hotkeys.stop.side_effect = RuntimeError("synthetic failure")
    daemon.start()
    assert daemon.failure
    daemon.recorder.disconnect.assert_called_once()


def test_fatal_processing_worker_failure_stops_lifecycle(daemon):
    entered = threading.Event()
    daemon.monitor.wait_for_state_change.side_effect = lambda event: entered.set() or event.wait(2)
    daemon.start_background()
    assert entered.wait(2)
    daemon._worker_failed()
    assert daemon._stopped_event.wait(2)
    assert daemon.lifecycle_state == "failed"


def test_runtime_lock_prevents_overlapping_job_consumers(tmp_path):
    first, second = RuntimeState(tmp_path), RuntimeState(tmp_path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="Another companion"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_health_requires_matching_live_pid_and_fresh_running_heartbeat(tmp_path, monkeypatch):
    state = RuntimeState(tmp_path)
    state.report("running", 3)
    record = json.loads((tmp_path / "health.json").read_text())
    monkeypatch.setattr("webex_obs.runtime_state.os.kill", Mock())
    assert application_health(tmp_path, None) == "not running"
    assert "unhealthy" in application_health(tmp_path, record["pid"] + 1)
    assert application_health(tmp_path, record["pid"]).startswith("healthy")
    monkeypatch.setattr("webex_obs.runtime_state.time.time", lambda: record["expires"] + 1)
    assert "stale" in application_health(tmp_path, record["pid"])
    state.report("failed", 3)
    assert "failed" in application_health(tmp_path, record["pid"])


def test_sigterm_uses_shutdown_path_without_operating_service(speech_modules, monkeypatch):
    import signal
    import webex_obs.daemon as module

    handlers = {}
    app = Mock(failure=None)
    app._stopped_event = threading.Event()
    app._shutdown_event = threading.Event()

    def shutdown_request():
        app._shutdown_event.set()
        app._stopped_event.set()

    app.request_shutdown.side_effect = shutdown_request
    app.start_background.side_effect = lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)
    monkeypatch.setattr(module, "WebexOBSDaemon", lambda **kwargs: app)
    monkeypatch.setattr(module.signal, "signal", lambda number, handler: handlers.update({number: handler}))
    monkeypatch.setattr(module.sys, "platform", "synthetic-headless")
    monkeypatch.setattr(module.logging, "basicConfig", Mock())
    module.main()
    app.request_shutdown.assert_called_once()
    assert signal.SIGINT in handlers


def test_main_exits_nonzero_after_shutdown_deadline(speech_modules, monkeypatch):
    import webex_obs.daemon as module

    app = Mock(failure=None)
    app._stopped_event.wait.return_value = False
    app._shutdown_event.is_set.return_value = True
    app.shutdown.return_value = False
    monkeypatch.setattr(module, "WebexOBSDaemon", lambda **kwargs: app)
    monkeypatch.setattr(module.signal, "signal", Mock())
    monkeypatch.setattr(module.sys, "platform", "synthetic-headless")
    monkeypatch.setattr(module.logging, "basicConfig", Mock())
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1
    assert "timed out" in app.failure
    app.shutdown.assert_called_once()


@pytest.mark.parametrize("manual", [True, False])
def test_shutdown_during_consent_dialog_preserves_recording_even_if_cancel_returns(daemon, manual):
    daemon.config.recordings_dir.mkdir()
    media = daemon.config.recordings_dir / "synthetic.mkv"
    media.write_bytes(b"synthetic recording")
    daemon.monitor.wait_for_state_change.return_value = True
    daemon.monitor.should_suppress_automatic_prompt.return_value = False
    daemon.monitor.get_active_call_title.return_value = "Synthetic meeting"
    if manual:
        daemon._manual_start_event.set()

    def start(**kwargs):
        daemon.recorder.is_recording = True
        return True

    def dialog(**kwargs):
        daemon.request_shutdown()
        return "cancel"

    def stop():
        daemon.recorder.is_recording = False
        return [str(media)]

    daemon.recorder.start_recording.side_effect = start
    daemon.recorder.stop_recording.side_effect = stop
    daemon.ui.show_startup_prompt.side_effect = dialog
    daemon.ui.show_manual_consent_prompt.side_effect = dialog
    daemon.start()
    daemon.recorder.stop_recording.assert_called_once()
    assert len(list(daemon.config.recordings_dir.glob("*.mkv"))) == 1
    assert len(list((daemon.config.log_dir / "jobs").glob("*.json"))) == 1
    assert not daemon.failure


def test_shutdown_interrupts_long_recording_poll_and_finalizes_capture(daemon):
    daemon.config = daemon.config.model_copy(update={"poll_interval": 60.0})
    daemon._manual_start_event.set()
    daemon.ui.show_manual_consent_prompt.return_value = "ok"
    daemon.monitor.get_active_call_title.return_value = "Synthetic meeting"
    daemon.monitor.is_in_meeting = False
    polling = threading.Event()
    original_wait = daemon._shutdown_event.wait

    def wait(timeout):
        if timeout == 60.0:
            polling.set()
        return original_wait(timeout)

    def start(**kwargs):
        daemon.recorder.is_recording = True
        return True

    def stop():
        daemon.recorder.is_recording = False
        return []

    daemon._shutdown_event.wait = wait
    daemon.recorder.start_recording.side_effect = start
    daemon.recorder.stop_recording.side_effect = stop
    daemon.start_background()
    assert polling.wait(2)
    assert daemon.shutdown(2)
    daemon.recorder.stop_recording.assert_called_once()
    assert not daemon.failure


@pytest.mark.parametrize("verified", [False, True])
def test_automatic_platform_binding_lasts_until_verified_stop(daemon, verified):
    from webex_obs.process_monitor import ProcessMonitor, ZOOM

    monitor = ProcessMonitor()
    monitor.current_platform = ZOOM
    monitor.current_call_title = "Synthetic Zoom call"
    daemon.monitor = monitor
    daemon._post_processing_worker = Mock()
    daemon._new_recording_session = Mock(return_value=Mock(display_title="Synthetic Zoom call"))
    daemon.recorder.is_video_recording = False
    daemon.recorder.is_screen_recording = False
    daemon.recorder.stop_verified = verified
    monitor.wait_for_state_change = Mock(return_value=True)

    def start(**kwargs):
        assert monitor.bound_platform is ZOOM
        daemon.recorder.is_recording = True
        monitor.is_in_meeting = True
        return True

    def consent(**kwargs):
        # Normal consent stays before waiting for automatic call end.
        assert monitor.bound_platform is ZOOM
        daemon._manual_stop_event.set()
        return "ok"

    def stop():
        assert monitor.bound_platform is ZOOM
        daemon.recorder.is_recording = not verified
        daemon.request_shutdown()
        return []

    daemon.recorder.start_recording.side_effect = start
    daemon.recorder.stop_recording.side_effect = stop
    daemon.ui.show_startup_prompt.side_effect = consent
    daemon.start()
    daemon.ui.show_startup_prompt.assert_called_once()
    daemon.ui.show_manual_consent_prompt.assert_not_called()
    assert monitor.bound_platform is (None if verified else ZOOM)


@pytest.mark.parametrize("cancel_first", [True, False])
def test_quick_webex_transition_starts_new_recording_after_verified_stop(daemon, monkeypatch, cancel_first):
    from webex_obs.process_monitor import CallEvidence, ProcessMonitor, WEBEX

    monitor = ProcessMonitor(poll_interval=0, call_end_grace_seconds=15)
    daemon.monitor = monitor
    active = CallEvidence(True, "Synthetic meeting")
    # With cancellation, also observe the original call still active: this
    # must stay suppressed until a complete idle gap and confirmed return.
    rows = iter([active, active] + ([active, active] if cancel_first else []) + [
        CallEvidence(False, fully_idle=True), active, active, active, active,
    ])
    monkeypatch.setattr(monitor, "_call_evidence", lambda p:
                        next(rows) if p is WEBEX else CallEvidence(False))
    monkeypatch.setattr(monitor, "get_active_call_title", lambda: "Synthetic meeting")
    monkeypatch.setattr("webex_obs.process_monitor.time.monotonic", lambda: 100.0)
    events = []

    def start(**kwargs):
        events.append("start")
        daemon.recorder.is_recording = True
        return True

    def stop():
        events.append("stop")
        daemon.recorder.is_recording = False
        return []

    def consent(**kwargs):
        if daemon.ui.show_startup_prompt.call_count == 1:
            return "cancel" if cancel_first else "ok"
        daemon.request_shutdown()
        return "ok"

    daemon.recorder.start_recording.side_effect = start
    daemon.recorder.stop_recording.side_effect = stop
    daemon.ui.show_startup_prompt.side_effect = consent
    daemon.start()
    assert not daemon.failure
    assert daemon.ui.show_startup_prompt.call_count == 2
    assert events == ["start", "stop", "start", "stop"]


@pytest.mark.parametrize("verified", [True, False])
def test_session_delete_override_is_queued_only_after_verified_stop_and_resets(daemon, verified):
    from webex_obs.session import RecordingSession

    daemon._active_session = RecordingSession.create("Synthetic meeting")
    daemon.recorder.is_recording = True
    daemon.toggle_delete_after_transcription(daemon._window_session_generation)
    assert daemon._delete_after_transcription
    assert daemon.config.retention_days == 14
    daemon.config.recordings_dir.mkdir()
    media = daemon.config.recordings_dir / "synthetic.mkv"
    media.write_bytes(b"synthetic recording")
    daemon.recorder.stop_verified = verified
    daemon.recorder.stop_recording.return_value = [str(media)]
    daemon._post_processing_worker.submit = Mock()
    assert daemon._finish_recording() is verified
    if not verified:
        assert daemon._delete_after_transcription
        assert media.exists()
        daemon._post_processing_worker.submit.assert_not_called()
        daemon.recorder.stop_verified = True
        assert daemon._finish_recording()
    job = daemon._post_processing_worker.submit.call_args.args[0]
    assert job.retention_days == 0
    assert not daemon._delete_after_transcription
    assert daemon.config.retention_days == 14
    daemon._active_session = RecordingSession.create("Synthetic next meeting")
    next_media = daemon.config.recordings_dir / "synthetic-next.mkv"
    next_media.write_bytes(b"synthetic next recording")
    daemon.recorder.stop_recording.return_value = [str(next_media)]
    assert daemon._finish_recording()
    assert daemon._post_processing_worker.submit.call_args.args[0].retention_days == 14


@pytest.mark.parametrize("blocked", ["stale", "idle", "stopping", "zero", "shutdown"])
def test_session_delete_override_rejects_unavailable_or_stale_requests(daemon, blocked):
    daemon._active_session = Mock()
    daemon.recorder.is_recording = True
    generation = daemon._window_session_generation
    if blocked == "stale":
        generation -= 1
    elif blocked == "idle":
        daemon._active_session = None
    elif blocked == "stopping":
        daemon._stop_pending = True
    elif blocked == "zero":
        daemon.config.retention_days = 0
    else:
        daemon.request_shutdown()
    daemon.toggle_delete_after_transcription(generation)
    assert not daemon._delete_after_transcription


def test_session_delete_override_can_be_toggled_off(daemon):
    daemon._active_session = Mock()
    daemon.recorder.is_recording = True
    daemon.toggle_delete_after_transcription(daemon._window_session_generation)
    daemon.toggle_delete_after_transcription(daemon._window_session_generation)
    assert not daemon._delete_after_transcription


def test_diagnostic_writer_is_closed_on_lifecycle_shutdown(daemon):
    from webex_obs.detection_diagnostics import DetectionDiagnostics

    daemon.diagnostics = DetectionDiagnostics(daemon.config.log_dir)
    daemon._run_lifecycle = daemon.request_shutdown
    daemon.start()
    assert daemon._stopped_event.is_set()
    assert not daemon.diagnostics._worker.is_alive()
    assert daemon.diagnostics.path.exists()
    records = [json.loads(line) for line in daemon.diagnostics.path.read_text().splitlines()]
    assert records[0]["kind"] == "enabled"
    assert records[-1]["state"] == "stopped"
    daemon.recorder.start_recording.assert_not_called()
