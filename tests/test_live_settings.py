import sys
import queue
import threading
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

fake_mlx_whisper = types.ModuleType("mlx_whisper")
fake_mlx_whisper.transcribe = lambda *args, **kwargs: {"text": "Discussed the roadmap.", "segments": []}
sys.modules.setdefault("mlx_whisper", fake_mlx_whisper)

from webex_obs.config import Config
from webex_obs.daemon import WebexOBSDaemon
from webex_obs.post_processing import PostProcessingJob
from webex_obs.obs_controller import CaptureDisplay, WebexWindow


def test_new_session_never_waits_for_title_lookup_before_recording():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.monitor = Mock(current_call_title=None, default_call_title="Webex Session")
    session = daemon._new_recording_session()
    assert session.display_title == "Webex Session"
    daemon.monitor.get_active_call_title.assert_not_called()


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


def test_manual_video_start_enters_worker_lifecycle_without_automatic_call_prompt():
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


def test_controls_hotkey_opens_status_menu_without_window_probe_or_dialog():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.recorder = Mock(is_recording=True)
    daemon.monitor = Mock(current_call_title="Cached title", default_call_title="Webex Session")
    daemon._active_session = Mock(display_title="Recording title")
    daemon.ui = Mock()

    daemon._handle_dialog_request()

    daemon.monitor.get_active_call_title.assert_not_called()
    daemon.ui.show_control_prompt.assert_not_called()
    daemon.ui.show_recording_menu.assert_called_once_with()


@pytest.mark.parametrize("mode", ["audio", "video"])
@pytest.mark.parametrize("response", ["ok", "cancel"])
@pytest.mark.parametrize("stop_verified", [False, True])
def test_manual_consent_runs_after_start_and_cancel_discards_all_segments(tmp_path, mode, response, stop_verified):
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock(is_recording=False, is_video_recording=mode == "video", is_screen_recording=False)
    daemon.monitor = Mock(is_in_meeting=False, current_call_title=None, default_call_title="Webex Session")
    daemon.monitor.wait_for_state_change.side_effect = [True, KeyboardInterrupt]
    daemon.monitor.is_call_active.return_value = True
    daemon.hotkeys, daemon.ui, daemon._post_processing_worker = Mock(), Mock(), Mock()
    daemon._manual_start_event, daemon._manual_stop_event = threading.Event(), threading.Event()
    daemon._manual_start_event.set()
    daemon._manual_start_mode = mode
    daemon._discard_requested = False
    daemon._active_session = None
    daemon._settings_lock = threading.Lock()
    files = [tmp_path / "first.mkv", tmp_path / "second.mkv"]
    for file in files:
        file.write_text("recorded media")
    session = Mock(display_title="Manual meeting", filename_stem="manual")
    session.rename_recordings.return_value = [str(file) for file in files]
    daemon.transcriber, daemon.webex = Mock(), Mock()

    def start(**kwargs):
        daemon.recorder.is_recording = True
        return True
    def consent(**kwargs):
        assert daemon.recorder.is_recording
        daemon.monitor.get_active_call_title.assert_not_called()
        # OK/timeout is followed by an ordinary stop in this bounded test.
        if response == "ok":
            daemon._manual_stop_event.set()
        return response
    def stop():
        daemon.recorder.is_recording = response == "cancel" and not stop_verified
        return [str(file) for file in files]
    daemon.recorder.start_recording.side_effect = start
    daemon.ui.show_manual_consent_prompt.side_effect = consent
    daemon.recorder.stop_recording.side_effect = stop

    with patch.object(daemon, "_new_recording_session", return_value=session), \
         patch("webex_obs.daemon.MediaCleaner.prune_old_recordings"), \
         patch("webex_obs.daemon.shutil.which", return_value="/usr/bin/ffmpeg"):
        daemon.start()
    daemon.ui.show_manual_consent_prompt.assert_called_once_with(meeting_title="Manual meeting")
    daemon.ui.show_startup_prompt.assert_not_called()
    daemon.recorder.stop_recording.assert_called_once()
    if response == "cancel":
        assert all(file.exists() != stop_verified for file in files)
        daemon._post_processing_worker.submit.assert_not_called()
        daemon.recorder.list_webex_windows.assert_not_called()
        daemon.monitor.suppress_current_call_prompt.assert_called_once()
    else:
        assert all(file.exists() for file in files)
        daemon._post_processing_worker.submit.assert_called_once()


@pytest.mark.parametrize("confirmed", [False, True])
def test_menu_discard_requires_confirmation(confirmed):
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.recorder = Mock(is_recording=True)
    daemon.ui = Mock()
    daemon.ui.confirm_discard.return_value = confirmed
    daemon._active_session = object()
    with patch.object(daemon, "_handle_control_choice") as action:
        daemon.request_discard_confirmation()
    if confirmed:
        action.assert_called_once_with("stop_discard")
    else:
        action.assert_not_called()


def test_stale_discard_confirmation_cannot_discard_a_later_session():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.recorder = Mock(is_recording=True)
    daemon.ui = Mock()
    daemon._active_session = object()
    daemon.ui.confirm_discard.side_effect = lambda: setattr(daemon, "_active_session", object()) or True
    with patch.object(daemon, "_handle_control_choice") as action:
        daemon.request_discard_confirmation()
    action.assert_not_called()


def test_confirmed_discard_is_queued_for_recording_worker():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.recorder = Mock(is_recording=True)
    daemon._manual_stop_event = threading.Event()
    daemon._discard_requested = False

    daemon._handle_control_choice("stop_discard")

    assert daemon._discard_requested is True
    assert daemon._manual_stop_event.is_set()
    daemon.recorder.stop_recording.assert_not_called()


def test_idle_video_hotkey_uses_manual_lifecycle_including_consent_notice():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.recorder = Mock(is_recording=False)
    with patch.object(daemon, "_request_manual_start") as request:
        daemon._switch_to_video_mode()
    request.assert_called_once_with("video")
    daemon.recorder.switch_to_video_mode.assert_not_called()


def test_confirmed_discard_stops_and_deletes_without_post_processing():
    with tempfile.TemporaryDirectory() as folder:
        recording = Path(folder) / "recording.mkv"
        recording.write_bytes(b"test recording")
        daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
        daemon.config = Config(_env_file=None, enable_diarization=False)
        daemon.recorder = Mock(is_recording=False, is_video_recording=False)
        daemon.recorder.initialize.return_value = True
        daemon.recorder.start_recording.side_effect = lambda **kwargs: True

        def stop_recording():
            daemon.recorder.is_recording = False
            return [str(recording)]

        daemon.recorder.stop_recording.side_effect = stop_recording
        daemon.monitor = Mock(is_in_meeting=False, current_call_title=None,
                              default_call_title="Webex Session")
        daemon.monitor.wait_for_state_change.side_effect = [True, KeyboardInterrupt]
        daemon.hotkeys = Mock()
        daemon.ui = Mock()
        daemon._post_processing_worker = Mock()
        daemon._manual_start_event = threading.Event()
        daemon._manual_start_event.set()
        daemon._manual_start_mode = "audio"
        daemon._manual_stop_event = threading.Event()
        daemon._discard_requested = False
        daemon._active_session = None
        daemon._settings_lock = threading.Lock()

        def request_discard(_seconds):
            daemon.recorder.is_recording = True
            daemon._handle_control_choice("stop_discard")

        with patch.object(daemon, "_new_recording_session", return_value=Mock()), \
             patch("webex_obs.daemon.MediaCleaner.prune_old_recordings"), \
             patch("webex_obs.daemon.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("webex_obs.daemon.time.sleep", side_effect=request_discard):
            daemon.start()

        assert not recording.exists()
        daemon.recorder.stop_recording.assert_called_once()
        daemon._post_processing_worker.submit.assert_not_called()


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


def test_manual_picker_offers_entire_screen_without_webex_windows():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.recorder = Mock(is_video_recording=True, selected_display_uuid=None)
    daemon.recorder.list_webex_windows.return_value = []
    displays = [CaptureDisplay("display-uuid", "Main Display")]
    daemon.recorder.list_capture_displays.return_value = displays
    daemon.ui = Mock()
    daemon.ui.choose_webex_window.return_value = "display-uuid"
    daemon._window_selection_queue = queue.Queue()
    daemon._window_prompt_pending = threading.Event()
    daemon._window_session_generation = 1

    with patch("webex_obs.daemon.threading.Thread") as thread:
        thread.side_effect = lambda **kwargs: SimpleNamespace(start=kwargs["target"])
        daemon.request_window_selection()

    daemon.ui.choose_webex_window.assert_called_once_with([], None, displays, None)
    assert daemon._window_selection_queue.get_nowait() == (1, "display-uuid")


def test_entire_screen_choice_disables_automatic_window_following():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.recorder = Mock(is_video_recording=True, is_screen_recording=False)
    daemon.recorder.select_capture_display.side_effect = lambda _uuid: setattr(
        daemon.recorder, "is_screen_recording", True
    ) or True
    daemon.ui = Mock()
    daemon._window_selection_queue = queue.Queue()
    daemon._window_selection_queue.put((1, "display-uuid"))
    daemon._window_session_generation = 1

    daemon._update_video_window()

    daemon.recorder.select_capture_display.assert_called_once_with("display-uuid")
    daemon.recorder.poll_webex_window_change.assert_not_called()


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
