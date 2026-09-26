import sys
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


def test_applies_settings_to_running_components_without_restarting_service():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock()
    daemon.monitor = SimpleNamespace(poll_interval=3.0, call_end_grace_seconds=15.0)
    daemon.hotkeys = Mock()
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
    daemon.recorder.connect.return_value = True
    daemon.recorder.stop_recording.return_value = []
    daemon.monitor = Mock(is_in_meeting=False, current_call_title=None,
                          default_call_title="Webex Session")
    daemon.monitor.wait_for_state_change.side_effect = [True, KeyboardInterrupt]
    daemon.monitor.is_call_active.return_value = False
    daemon.hotkeys = Mock()
    daemon.ui = Mock()
    daemon._manual_start_event = threading.Event()
    daemon._manual_start_event.set()
    daemon._manual_start_mode = "video"
    daemon._manual_stop_event = threading.Event()
    daemon._discard_requested = False
    daemon._active_session = None
    session = Mock(display_title="Manual recording")
    daemon.recorder.start_recording.side_effect = lambda **kwargs: daemon._manual_stop_event.set() or True

    with patch.object(daemon, "_new_recording_session", return_value=session), \
         patch("webex_obs.daemon.MediaCleaner.prune_old_recordings"), \
         patch("webex_obs.daemon.shutil.which", return_value="/usr/bin/ffmpeg"):
        daemon.start()

    daemon.recorder.start_recording.assert_called_once_with(mode="video")
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
