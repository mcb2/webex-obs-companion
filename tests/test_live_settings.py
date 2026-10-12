import queue
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest


from webex_obs.config import Config
from webex_obs.obs_controller import CaptureDisplay, WebexWindow


@pytest.fixture(autouse=True)
def speech_components(speech_modules, monkeypatch):
    monkeypatch.setitem(globals(), "WebexOBSDaemon", speech_modules["WebexOBSDaemon"])
    monkeypatch.setitem(globals(), "PostProcessingJob", speech_modules["PostProcessingJob"])


def _daemon():
    daemon = WebexOBSDaemon.__new__(WebexOBSDaemon)
    daemon._recording_request_lock = threading.RLock()
    daemon._video_switch_generation = None
    daemon._window_session_generation = 0
    daemon._manual_stop_event = threading.Event()
    daemon._stop_pending = False
    return daemon


def test_new_session_never_waits_for_title_lookup_before_recording():
    daemon = _daemon()
    daemon.monitor = Mock(current_call_title=None, default_call_title="Webex Session")
    session = daemon._new_recording_session()
    assert session.display_title == "Webex Session"
    daemon.monitor.get_active_call_title.assert_not_called()


@pytest.mark.parametrize("shortcut", ["hotkey_audio", "hotkey_video", "hotkey_video_source", "hotkey_stop_transcribe"])
def test_applies_settings_to_running_components_without_restarting_service(shortcut):
    daemon = _daemon()
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock()
    daemon.monitor = SimpleNamespace(poll_interval=3.0, call_end_grace_seconds=15.0)
    daemon.hotkeys = Mock()
    daemon._settings_lock = threading.Lock()
    updated = daemon.config.model_copy(update={
        "poll_interval": 1.0, "call_end_grace_seconds": 7.0,
        "webex_recipient_email": "updated@example.com", "whisper_model": "new-model",
        shortcut: "<cmd>+<shift>+x",
    })

    with patch("webex_obs.daemon.Transcriber") as transcriber, \
         patch("webex_obs.daemon.WebexClient") as webex, \
         patch("webex_obs.daemon.HotkeyListener") as hotkeys:
        daemon.apply_settings(updated)

    daemon.recorder.configure.assert_called_once_with(updated)
    daemon.hotkeys.stop.assert_not_called()
    hotkeys.return_value.start.assert_called_once()
    assert hotkeys.call_args.kwargs["audio_hotkey"] == updated.hotkey_audio
    assert hotkeys.call_args.kwargs["video_source_hotkey"] == updated.hotkey_video_source
    assert hotkeys.call_args.kwargs["on_audio_start"] == daemon._handle_audio_start_request
    assert hotkeys.call_args.kwargs["on_video_source"] == daemon.request_window_selection
    transcriber.assert_called_once_with(
        model_name="new-model", transcripts_dir=updated.transcripts_dir,
        enable_diarization=False, hf_token=updated.hf_token,
    )
    assert webex.call_args.kwargs["recipient_email"] == "updated@example.com"
    assert daemon.monitor.poll_interval == 1.0
    assert daemon.monitor.call_end_grace_seconds == 7.0
    assert daemon.config is updated


def test_disabled_delivery_keeps_transcript_local():
    daemon = _daemon()
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
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=False, stop_verified=True)
    daemon._manual_start_event = threading.Event()
    daemon._manual_start_mode = "audio"
    daemon._active_session = None
    daemon._request_manual_start("video")
    assert daemon._manual_start_event.is_set()
    assert daemon._manual_start_mode == "video"
    daemon.recorder.start_recording.assert_not_called()


def test_manual_video_start_enters_worker_lifecycle_without_automatic_call_prompt():
    daemon = _daemon()
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock(is_recording=False, stop_verified=True)
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


def test_audio_hotkey_requests_manual_audio_without_window_probe_or_dialog():
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=False, stop_verified=True)
    daemon.monitor = Mock(current_call_title="Cached title", default_call_title="Webex Session")
    daemon._active_session = Mock(display_title="Recording title")
    daemon.ui = Mock()

    with patch.object(daemon, "_request_manual_start") as request:
        daemon._handle_audio_start_request()
    request.assert_called_once_with("audio")

    daemon.monitor.get_active_call_title.assert_not_called()
    daemon.ui.show_control_prompt.assert_not_called()
    daemon.ui.show_recording_menu.assert_not_called()


@pytest.mark.parametrize("mode", ["audio", "video"])
@pytest.mark.parametrize("response", ["ok", "cancel"])
@pytest.mark.parametrize("stop_verified", [False, True])
def test_manual_consent_runs_after_start_and_cancel_discards_all_segments(tmp_path, mode, response, stop_verified):
    daemon = _daemon()
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock(is_recording=False, stop_verified=True, is_video_recording=mode == "video", is_screen_recording=False)
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
        daemon.recorder.is_recording = not stop_verified
        daemon.recorder.stop_verified = stop_verified
        return [str(file) for file in files]
    daemon.recorder.start_recording.side_effect = start
    daemon.ui.show_manual_consent_prompt.side_effect = consent
    daemon.recorder.stop_recording.side_effect = stop

    with patch.object(daemon, "_new_recording_session", return_value=session), \
         patch("webex_obs.daemon.MediaCleaner.prune_old_recordings"), \
         patch("webex_obs.daemon.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("webex_obs.daemon.time.sleep", side_effect=KeyboardInterrupt):
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
        if stop_verified:
            daemon._post_processing_worker.submit.assert_called_once()
        else:
            daemon._post_processing_worker.submit.assert_not_called()
            session.rename_recordings.assert_not_called()


@pytest.mark.parametrize("confirmed", [False, True])
def test_menu_discard_requires_confirmation(confirmed):
    daemon = _daemon()
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
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=True)
    daemon.ui = Mock()
    daemon._active_session = object()
    daemon.ui.confirm_discard.side_effect = lambda: setattr(daemon, "_active_session", object()) or True
    with patch.object(daemon, "_handle_control_choice") as action:
        daemon.request_discard_confirmation()
    action.assert_not_called()


def test_confirmed_discard_is_queued_for_recording_worker():
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=True)
    daemon._manual_stop_event = threading.Event()
    daemon._discard_requested = False

    daemon._handle_control_choice("stop_discard")

    assert daemon._discard_requested is True
    assert daemon._manual_stop_event.is_set()
    daemon.recorder.stop_recording.assert_not_called()


def test_idle_video_hotkey_uses_manual_lifecycle_including_consent_notice():
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=False, stop_verified=True)
    with patch.object(daemon, "_request_manual_start") as request:
        daemon._switch_to_video_mode()
    request.assert_called_once_with("video")
    daemon.recorder.switch_to_video_mode.assert_not_called()


def test_audio_hotkey_does_not_restart_an_active_recording():
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=True)
    daemon._manual_start_event = threading.Event()
    daemon._handle_audio_start_request()
    assert not daemon._manual_start_event.is_set()
    daemon.recorder.prepare_recording.assert_not_called()
    daemon.recorder.start_recording.assert_not_called()


def test_video_hotkey_does_not_split_an_existing_video_recording():
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=True, is_video_recording=True)
    daemon._switch_to_video_mode()
    daemon.recorder.switch_to_video_mode.assert_not_called()


def test_video_source_hotkey_does_nothing_during_audio_recording():
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=True, is_video_recording=False)
    daemon.request_window_selection()
    daemon.recorder.list_webex_windows.assert_not_called()
    daemon.recorder.list_capture_displays.assert_not_called()


def test_confirmed_discard_stops_and_deletes_without_post_processing():
    with tempfile.TemporaryDirectory() as folder:
        recording = Path(folder) / "recording.mkv"
        recording.write_bytes(b"test recording")
        daemon = _daemon()
        daemon.config = Config(_env_file=None, enable_diarization=False)
        daemon.recorder = Mock(is_recording=False, stop_verified=True, is_video_recording=False)
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
        daemon = _daemon()
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
    daemon = _daemon()
    daemon.config = Config(_env_file=None, shared_window_behavior="prompt")
    daemon.recorder = Mock(is_recording=True, is_video_recording=False)
    daemon.recorder.switch_to_video_mode.side_effect = lambda: setattr(daemon.recorder, "is_video_recording", True) or True
    daemon.recorder.list_webex_windows.return_value = [
        WebexWindow(101, "Meeting", "Webex", 1200, 800),
        WebexWindow(202, "Shared content", "Webex", 1400, 900),
    ]
    daemon.request_window_selection = Mock()
    daemon.ui = Mock()
    daemon._switch_to_video_mode()
    daemon.recorder.switch_to_video_mode.assert_not_called()
    daemon._apply_video_switch()
    daemon.recorder.switch_to_video_mode.assert_called_once()
    daemon.request_window_selection.assert_called_once_with()


def test_window_choice_from_previous_session_is_ignored():
    daemon = _daemon()
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
    daemon = _daemon()
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
    daemon = _daemon()
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
    daemon = _daemon()
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock(is_recording=False, stop_verified=True)
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
    daemon = _daemon()
    daemon.ui = Mock()
    daemon.deliver_transcript = Mock(return_value=True)
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


@pytest.mark.parametrize("stop_verified", [False, True])
def test_automatic_cancel_preserves_files_until_stop_is_verified(tmp_path, stop_verified):
    daemon = _daemon()
    daemon.config = Config(_env_file=None, enable_diarization=False)
    daemon.recorder = Mock(is_recording=False, stop_verified=True)
    daemon.monitor = Mock(is_in_meeting=True, current_call_title="Synthetic call", default_call_title="Webex Session")
    daemon.monitor.should_suppress_automatic_prompt.return_value = False
    daemon.monitor.wait_for_state_change.side_effect = [True, KeyboardInterrupt]
    daemon.hotkeys, daemon.ui, daemon._post_processing_worker = Mock(), Mock(), Mock()
    daemon._manual_start_event, daemon._manual_stop_event = threading.Event(), threading.Event()
    daemon._settings_lock = threading.Lock()
    daemon._active_session = None
    daemon.ui.show_startup_prompt.return_value = "cancel"
    files = [tmp_path / "first.mkv", tmp_path / "recovered.mkv"]
    for file in files:
        file.write_bytes(b"synthetic recording")

    def stop():
        daemon.recorder.is_recording = not stop_verified
        daemon.recorder.stop_verified = stop_verified
        return [str(file) for file in files]

    daemon.recorder.stop_recording.side_effect = stop
    with patch("webex_obs.daemon.MediaCleaner.prune_old_recordings"), \
         patch("webex_obs.daemon.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("webex_obs.daemon.time.sleep", side_effect=KeyboardInterrupt):
        daemon.start()
    assert all(file.exists() != stop_verified for file in files)
    daemon.recorder.stop_recording.assert_called_once()
    daemon._post_processing_worker.submit.assert_not_called()
    daemon.monitor.suppress_current_call_prompt.assert_called_once()
    assert (daemon._active_session is None) == stop_verified
    assert daemon._stop_pending == (not stop_verified)
    if not stop_verified:
        assert daemon._discard_requested
        assert daemon.monitor.wait_for_state_change.call_count == 1


@pytest.mark.parametrize("discard", [False, True])
@pytest.mark.parametrize("status", [True, False, None])
def test_unverified_completion_retains_session_and_action_until_explicit_verified_retry(tmp_path, discard, status):
    daemon = _daemon()
    daemon.config = Config(_env_file=None, recordings_dir=tmp_path, enable_diarization=False)
    media = tmp_path / "owned.mkv"
    media.write_bytes(b"synthetic")
    session = Mock(display_title="Synthetic meeting", filename_stem="synthetic")
    session.rename_recordings.return_value = [str(media)]
    daemon._active_session = session
    daemon._discard_requested = discard
    daemon._manual_stop_event = threading.Event()
    daemon._manual_start_event = threading.Event()
    daemon._settings_lock = threading.Lock()
    # A stale inactive flag and even a returned path cannot authorize completion.
    daemon.recorder = Mock(is_recording=False, recording_status=status, stop_verified=False)
    daemon.recorder.stop_recording.return_value = [str(media)]
    daemon.monitor = Mock(current_call_title="Synthetic meeting")
    daemon.ui, daemon.transcriber, daemon.webex, daemon._post_processing_worker = Mock(), Mock(), Mock(), Mock()

    assert not daemon._finish_recording()
    assert media.exists()
    assert daemon._active_session is session
    assert daemon._discard_requested is discard
    assert daemon._stop_pending
    session.rename_recordings.assert_not_called()
    daemon._post_processing_worker.submit.assert_not_called()
    daemon._request_manual_start("audio")
    assert not daemon._manual_start_event.is_set()

    daemon.recorder.stop_verified = True
    assert daemon._finish_recording()
    assert not daemon._stop_pending
    assert daemon._active_session is None
    if discard:
        assert not media.exists()
        session.rename_recordings.assert_not_called()
        daemon._post_processing_worker.submit.assert_not_called()
    else:
        assert media.exists()
        daemon._post_processing_worker.submit.assert_called_once()


@pytest.mark.parametrize("failure", ["no-transcript", "transcription-exception", "delivery-false", "delivery-exception"])
def test_failed_processing_never_becomes_retention_eligible_even_after_other_jobs(tmp_path, failure):
    from webex_obs.cleaner import MediaCleaner

    daemon = _daemon()
    daemon.ui = Mock()
    daemon.deliver_transcript = Mock(return_value=True)
    failed_media = tmp_path / "failed.mkv"
    failed_media.write_bytes(b"synthetic")
    transcriber = Mock()
    transcriber.transcribe_files.return_value = tmp_path / "synthetic.txt"
    if failure == "no-transcript":
        transcriber.transcribe_files.return_value = None
    elif failure == "transcription-exception":
        transcriber.transcribe_files.side_effect = RuntimeError("synthetic processing failure")
    elif failure == "delivery-false":
        daemon.deliver_transcript.return_value = False
    else:
        daemon.deliver_transcript.side_effect = RuntimeError("synthetic delivery failure")
    job = PostProcessingJob(
        media_files=(str(failed_media),), meeting_title="Synthetic failed job", output_stem="synthetic",
        transcriber=transcriber, webex=Mock(), delivery_enabled=True,
        recordings_dir=tmp_path, retention_days=0,
    )
    if failure.endswith("exception"):
        with pytest.raises(RuntimeError):
            daemon._process_post_processing_job(job)
    else:
        daemon._process_post_processing_job(job)
    assert failed_media.exists()
    assert not MediaCleaner._marker(failed_media).exists()

    succeeded = tmp_path / "succeeded.mkv"
    succeeded.write_bytes(b"synthetic")
    successful_job = PostProcessingJob(
        media_files=(str(succeeded),), meeting_title="Synthetic successful job", output_stem="success",
        transcriber=Mock(), webex=Mock(), delivery_enabled=False,
        recordings_dir=tmp_path, retention_days=0,
    )
    daemon.deliver_transcript = Mock(return_value=True)
    daemon._process_post_processing_job(successful_job)
    assert not succeeded.exists()
    assert failed_media.exists()
    MediaCleaner.prune_old_recordings(tmp_path, 0)  # also safe on subsequent startup
    assert failed_media.exists()


def test_pending_stop_resumes_same_session_only_on_explicit_retry(tmp_path):
    daemon = _daemon()
    daemon.config = Config(_env_file=None, recordings_dir=tmp_path, enable_diarization=False)
    daemon.recorder = Mock(is_recording=True, stop_verified=False)
    daemon.recorder.stop_recording.return_value = []
    daemon.monitor = Mock(is_in_meeting=False, current_call_title="Synthetic", default_call_title="Webex Session")
    daemon.monitor.wait_for_state_change.side_effect = KeyboardInterrupt
    daemon.hotkeys, daemon.ui, daemon._post_processing_worker = Mock(), Mock(), Mock()
    daemon._manual_stop_event, daemon._manual_start_event = threading.Event(), threading.Event()
    daemon._settings_lock = threading.Lock()
    daemon._active_session = Mock(display_title="Synthetic")
    session = daemon._active_session
    daemon._discard_requested = True
    daemon._stop_pending = True
    media = tmp_path / "owned.mkv"
    media.write_bytes(b"synthetic")

    def retry(_seconds):
        assert daemon._active_session is session
        daemon.recorder.stop_recording.assert_not_called()
        daemon.recorder.start_recording.assert_not_called()
        daemon.monitor.wait_for_state_change.assert_not_called()
        assert media.exists()
        daemon.recorder.stop_verified = True
        daemon.recorder.stop_recording.return_value = [str(media)]
        daemon._manual_stop_event.set()

    with patch("webex_obs.daemon.MediaCleaner.prune_old_recordings"), \
         patch("webex_obs.daemon.shutil.which", return_value="/synthetic/ffmpeg"), \
         patch("webex_obs.daemon.time.sleep", side_effect=retry):
        daemon.start()
    daemon.recorder.stop_recording.assert_called_once()
    daemon.recorder.start_recording.assert_not_called()
    daemon._post_processing_worker.submit.assert_not_called()
    assert not media.exists()
    assert daemon._active_session is None
    assert not daemon._stop_pending


@pytest.mark.parametrize("cancel", ["stop", "discard", "completed-session", "none"])
def test_queued_video_switches_coalesce_and_cannot_outlive_stop_or_session(cancel):
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=True, is_video_recording=False, is_screen_recording=False)
    daemon.recorder.switch_to_video_mode.return_value = True
    daemon.config = Config(_env_file=None, shared_window_behavior="always_switch")
    daemon.ui = Mock()
    daemon._active_session = Mock()
    daemon._discard_requested = False

    # Force simultaneous callbacks to finish before the lifecycle consumes them.
    barrier = threading.Barrier(3)
    def request():
        barrier.wait(timeout=3)
        daemon._handle_control_choice("switch_video")
    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=3)
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    daemon.recorder.switch_to_video_mode.assert_not_called()
    daemon.ui.show_notification.assert_not_called()
    if cancel == "stop":
        daemon._handle_stop_transcribe_request()
    elif cancel == "discard":
        daemon._handle_control_choice("stop_discard")
    elif cancel == "completed-session":
        daemon._window_session_generation += 1
    daemon._apply_video_switch()
    daemon._apply_video_switch()
    if cancel == "none":
        daemon.recorder.switch_to_video_mode.assert_called_once()
        daemon.ui.show_notification.assert_called_once_with("Webex OBS Companion", "Switched to Video recording mode.")
    else:
        daemon.recorder.switch_to_video_mode.assert_not_called()


def test_rejected_video_switch_never_announces_video_success():
    daemon = _daemon()
    daemon.config = Config(_env_file=None)
    daemon.recorder = Mock(is_recording=True, is_video_recording=False)
    daemon.recorder.switch_to_video_mode.return_value = False
    daemon.ui = Mock()
    daemon._handle_control_choice("switch_video")
    daemon._apply_video_switch()
    daemon.ui.show_notification.assert_called_once_with("Webex OBS Companion", "Could not switch to Video recording mode.")
    daemon.recorder.list_webex_windows.assert_not_called()


def test_picker_enumeration_runs_off_main_thread_and_ignores_completion_during_probe():
    daemon = _daemon()
    daemon.recorder = Mock(is_recording=True, is_video_recording=True)
    daemon._window_prompt_pending = threading.Event()
    daemon._window_selection_queue = queue.Queue()
    daemon.ui = Mock()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def list_windows():
        try:
            assert threading.current_thread() is not threading.main_thread()
            entered.set()
            assert release.wait(3)
            return [WebexWindow(101, "Synthetic meeting", "Webex", 1200, 800)]
        except BaseException as exc:
            errors.append(exc)
            raise

    daemon.recorder.list_webex_windows.side_effect = list_windows
    daemon.recorder.list_capture_displays.side_effect = lambda: finished.set() or []
    daemon.request_window_selection()
    try:
        assert entered.wait(3)
        daemon._window_session_generation += 1
    finally:
        release.set()
    assert finished.wait(3)
    # Join the picker so its final stale-session check has completed.
    for thread in threading.enumerate():
        if thread.name == "webex-window-picker":
            thread.join(3)
            assert not thread.is_alive()
    assert not errors
    daemon.ui.choose_webex_window.assert_not_called()
    assert daemon._window_selection_queue.empty()
    assert not daemon._window_prompt_pending.is_set()
