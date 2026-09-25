import sys
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
