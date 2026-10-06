import sys
import queue
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

fake_mlx_whisper = types.ModuleType("mlx_whisper")
fake_mlx_whisper.transcribe = lambda *args, **kwargs: {"text": "Discussed the roadmap.", "segments": []}
sys.modules.setdefault("mlx_whisper", fake_mlx_whisper)

from webex_obs.config import Config
from webex_obs.daemon import WebexOBSDaemon
from webex_obs.post_processing import PostProcessingJob
from webex_obs.obs_controller import WebexWindow


def test_applies_settings_to_running_components_without_restarting_service():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock()
    daemon.monitor = SimpleNamespace(poll_interval=3.0, call_end_grace_seconds=15.0)
    daemon.hotkeys = Mock()
    daemon._settings_lock = threading.Lock()
    updated = daemon.config.model_copy(update={
        "poll_interval": 1.0, "call_end_grace_seconds": 7.0,
        "webex_recipient_email": "updated@example.com", "whisper_model": "new-model",
        "hotkey_video": "<cmd>+<shift>+x",
    })

    with patch("webex_obs.daemon.Transcriber") as transcriber, \
         patch("webex_obs.daemon.WebexClient") as webex, \
         patch("webex_obs.daemon.HotkeyListener") as hotkeys:
        daemon.apply_settings(updated)

    daemon.recorder.configure.assert_called_once_with(updated)
    daemon.hotkeys.stop.assert_not_called()
    hotkeys.return_value.start.assert_called_once()
    transcriber.assert_called_once_with(
        model_name="new-model", transcripts_dir=updated.transcripts_dir,
        enable_diarization=False, hf_token=updated.hf_token,
    )
    assert webex.call_args.kwargs["recipient_email"] == "updated@example.com"
    assert daemon.monitor.poll_interval == 1.0
    assert daemon.monitor.call_end_grace_seconds == 7.0
    assert daemon.config is updated


def test_disabled_delivery_keeps_transcript_local():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.config = Config(_env_file=None, webex_delivery_enabled=False)
    daemon.webex = Mock()
    daemon.ui = Mock()
    daemon.deliver_transcript(Path("transcript.txt"), "Team call")
    daemon.webex.send_transcript.assert_not_called()
    daemon.ui.show_notification.assert_not_called()

    daemon.config = daemon.config.model_copy(update={"webex_delivery_enabled": True})
    daemon.webex.send_transcript.return_value = True
    daemon.deliver_transcript(Path("transcript.txt"), "Team call")
    daemon.webex.send_transcript.assert_called_once_with(Path("transcript.txt"), meeting_title="Team call")


def test_manual_start_requests_daemon_worker_instead_of_starting_obs_on_menu_thread():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.recorder = Mock(is_recording=False)
    daemon._manual_start_event = threading.Event()
    daemon._manual_start_mode = "audio"
    daemon._request_manual_start("video")
    assert daemon._manual_start_event.is_set()
    assert daemon._manual_start_mode == "video"
    daemon.recorder.start_recording.assert_not_called()


def test_manual_video_start_enters_worker_lifecycle_without_second_prompt():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock(is_recording=False)
    daemon.recorder.initialize.return_value = True
    daemon.recorder.stop_recording.return_value = []
    daemon.monitor = Mock(is_in_meeting=False, current_call_title=None,
                          default_call_title="Webex Session")
    daemon.monitor.wait_for_state_change.side_effect = [True, KeyboardInterrupt]
    daemon.monitor.is_call_active.return_value = False
    daemon.hotkeys = Mock()
    daemon.ui = Mock()
    daemon._post_processing_worker = Mock()
    daemon._manual_start_event = threading.Event()
    daemon._manual_start_event.set()
    daemon._manual_start_mode = "video"
    daemon._manual_stop_event = threading.Event()
    daemon._discard_requested = False
    daemon._active_session = None
    daemon._settings_lock = threading.Lock()
    session = Mock(display_title="Manual recording")
    daemon.recorder.start_recording.return_value = False
    daemon.recorder.retry_start_recording.side_effect = lambda **kwargs: daemon._manual_stop_event.set() or True

    with patch.object(daemon, "_new_recording_session", return_value=session), \
         patch("webex_obs.daemon.MediaCleaner.prune_old_recordings"), \
         patch("webex_obs.daemon.time.sleep"), \
         patch("webex_obs.daemon.shutil.which", return_value="/usr/bin/ffmpeg"):
        daemon.start()

    daemon.recorder.start_recording.assert_called_once_with(mode="video")
    daemon.recorder.retry_start_recording.assert_called_once_with(mode="video")
    daemon.ui.show_startup_prompt.assert_not_called()
    daemon.recorder.stop_recording.assert_called_once()
    assert daemon.monitor.is_in_meeting is False


def test_controls_use_cached_title_without_blocking_call_window_probe():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.recorder = Mock(is_recording=True)
    daemon.monitor = Mock(current_call_title="Cached title", default_call_title="Webex Session")
    daemon._active_session = Mock(display_title="Recording title")
    daemon.ui = Mock()
    daemon.ui.show_control_prompt.return_value = "close"

    daemon._handle_dialog_request()

    daemon.monitor.get_active_call_title.assert_not_called()
    daemon.ui.show_control_prompt.assert_called_once_with(
        is_recording=True, meeting_title="Recording title"
    )


def test_new_webex_window_follows_configured_behavior():
    candidate = WebexWindow(202, "Shared content", "Webex", 1440, 900)
    for behavior in ("always_switch", "prompt"):
        daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
        daemon.config = Config(_env_file=None, shared_window_behavior=behavior)
        daemon.recorder = Mock(is_video_recording=True)
        daemon.recorder.poll_webex_window_change.return_value = [candidate]
        daemon.ui = Mock()
        daemon._window_selection_queue = queue.Queue()
        daemon._window_prompt_pending = threading.Event()
        daemon._window_session_generation = 1
        daemon.request_window_selection = Mock()
        daemon._update_video_window()
        if behavior == "always_switch":
            daemon.recorder.select_webex_window.assert_called_once_with(202)
            daemon.request_window_selection.assert_not_called()
        else:
            daemon.recorder.select_webex_window.assert_not_called()
            daemon.request_window_selection.assert_called_once_with(202)


def test_video_switch_offers_existing_windows_in_prompt_mode():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.config = Config(_env_file=None, shared_window_behavior="prompt")
    daemon.recorder = Mock(is_video_recording=True)
    daemon.recorder.list_webex_windows.return_value = [
        WebexWindow(101, "Meeting", "Webex", 1200, 800),
        WebexWindow(202, "Shared content", "Webex", 1400, 900),
    ]
    daemon.request_window_selection = Mock()
    daemon._switch_to_video_mode()
    daemon.recorder.switch_to_video_mode.assert_called_once()
    daemon.request_window_selection.assert_called_once_with()


def test_window_choice_from_previous_session_is_ignored():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.config = Config(_env_file=None, shared_window_behavior="prompt")
    daemon.recorder = Mock(is_video_recording=True)
    daemon.recorder.poll_webex_window_change.return_value = []
    daemon.ui = Mock()
    daemon._window_selection_queue = queue.Queue()
    daemon._window_selection_queue.put((1, 101))
    daemon._window_session_generation = 2
    daemon._window_prompt_pending = threading.Event()
    daemon._update_video_window()
    daemon.recorder.select_webex_window.assert_not_called()


def test_completed_recording_is_queued_before_lifecycle_rearms():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock(is_recording=False)
    daemon.recorder.initialize.return_value = True
    daemon.recorder.stop_recording.return_value = ["recording.mkv"]
    daemon.monitor = Mock(
        is_in_meeting=False,
        current_call_title=None,
        default_call_title="Webex Session",
    )
    daemon.monitor.wait_for_state_change.side_effect = [True, KeyboardInterrupt]
    daemon.monitor.is_call_active.return_value = False
    daemon.hotkeys = Mock()
    daemon.ui = Mock()
    daemon.transcriber = Mock()
    daemon.webex = Mock()
    daemon._post_processing_worker = Mock()
    daemon._manual_start_event = threading.Event()
    daemon._manual_start_event.set()
    daemon._manual_start_mode = "audio"
    daemon._manual_stop_event = threading.Event()
    daemon._discard_requested = False
    daemon._active_session = None
    daemon._settings_lock = threading.Lock()
    session = Mock(
        display_title="Architecture Review",
        filename_stem="2026-09-28_10-00-00 - Architecture Review",
    )
    session.rename_recordings.return_value = ["renamed.mkv"]
    daemon.recorder.start_recording.side_effect = (
        lambda **kwargs: daemon._manual_stop_event.set() or True
    )

    with patch.object(daemon, "_new_recording_session", return_value=session), \
         patch("webex_obs.daemon.MediaCleaner.prune_old_recordings"), \
         patch("webex_obs.daemon.shutil.which", return_value="/usr/bin/ffmpeg"):
        daemon.start()

    daemon.transcriber.transcribe_files.assert_not_called()
    daemon._post_processing_worker.submit.assert_called_once()
    job = daemon._post_processing_worker.submit.call_args.args[0]
    assert isinstance(job, PostProcessingJob)
    assert job.media_files == ("renamed.mkv",)
    assert job.meeting_title == "Architecture Review"
    assert daemon.monitor.wait_for_state_change.call_count == 2


def test_post_processing_job_uses_snapshotted_workers_and_settings():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.ui = Mock()
    daemon.deliver_transcript = Mock()
    transcriber = Mock()
    transcript = Path("transcript.txt")
    transcriber.transcribe_files.return_value = transcript
    webex = Mock()
    job = PostProcessingJob(
        media_files=("recording.mkv",),
        meeting_title="Architecture Review",
        output_stem="session-stem",
        transcriber=transcriber,
        webex=webex,
        delivery_enabled=False,
        recordings_dir=Path("/tmp/recordings"),
        retention_days=30,
    )

    with patch("webex_obs.daemon.MediaCleaner.prune_old_recordings") as prune:
        daemon._process_post_processing_job(job)

    transcriber.transcribe_files.assert_called_once_with(
        ["recording.mkv"],
        meeting_title="Architecture Review",
        output_stem="session-stem",
    )
    daemon.deliver_transcript.assert_called_once_with(
        transcript,
        "Architecture Review",
        delivery_enabled=False,
        webex=webex,
    )
    prune.assert_called_once_with(Path("/tmp/recordings"), 30)
