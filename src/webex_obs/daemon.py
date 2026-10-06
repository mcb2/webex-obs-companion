import logging
import os
import queue
import shutil
import sys
import threading
import time
from pathlib import Path

# Ensure Homebrew and standard binary paths are in PATH
extra_paths = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")]
current_path = os.environ.get("PATH", "")
for p in extra_paths:
    if p not in current_path and os.path.exists(p):
        current_path = f"{p}:{current_path}"
os.environ["PATH"] = current_path

from .cleaner import MediaCleaner
from .config import Config, settings
from .hotkey_listener import HotkeyListener, display_hotkey
from .post_processing import PostProcessingJob, PostProcessingWorker
from .recording_backend import RecordingBackend, create_recording_backend
from .process_monitor import ProcessMonitor
from .session import RecordingSession
from .transcriber import Transcriber
from .ui_banner import UIBanner
from .webex_client import WebexClient

logger = logging.getLogger(__name__)


class WebexOBSDaemon:
    def __init__(self, config: Config | None = None, backend: RecordingBackend | None = None, ui=None):
        self.config = config or settings
        self.recorder = backend or create_recording_backend(self.config)
        # UI is injected so the lifecycle can run without AppKit (including tests).
        self.ui = ui or UIBanner
        self.monitor = ProcessMonitor(
            poll_interval=self.config.poll_interval,
            call_end_grace_seconds=self.config.call_end_grace_seconds,
        )
        self.transcriber = Transcriber(
            model_name=self.config.whisper_model,
            transcripts_dir=self.config.transcripts_dir,
            enable_diarization=self.config.enable_diarization,
            hf_token=self.config.hf_token,
        )
        self.webex = WebexClient(
            token=self.config.webex_token,
            recipient_email=self.config.webex_recipient_email,
            room_id=self.config.room_id,
            my_agent_email=self.config.my_agent_email,
        )
        self.hotkeys = HotkeyListener(
            on_video_switch=self._switch_to_video_mode,
            on_show_dialog=self._handle_dialog_request,
            on_stop_transcribe=self._handle_stop_transcribe_request,
            video_hotkey=self.config.hotkey_video,
            menu_hotkey=self.config.hotkey_menu,
            stop_transcribe_hotkey=self.config.hotkey_stop_transcribe,
        )
        self._manual_stop_event = threading.Event()
        self._manual_start_event = threading.Event()
        self._manual_start_mode = "audio"
        self._discard_requested = False
        self._active_session: RecordingSession | None = None
        self._settings_lock = threading.Lock()
        self._window_selection_queue: queue.Queue[tuple[int, int]] = queue.Queue()
        self._window_prompt_pending = threading.Event()
        self._window_session_generation = 0
        self._post_processing_worker = PostProcessingWorker(
            self._process_post_processing_job
        )

    def apply_settings(self, config: Config) -> None:
        """Install validated settings without interrupting a recording."""
        old = self.config
        # Construct workers before stopping the old hotkey listener.
        transcriber = Transcriber(
            model_name=config.whisper_model,
            transcripts_dir=config.transcripts_dir,
            enable_diarization=config.enable_diarization,
            hf_token=config.hf_token,
        )
        webex = WebexClient(
            token=config.webex_access_token,
            recipient_email=config.webex_recipient_email,
            room_id=config.webex_room_id,
            my_agent_email=config.my_agent_email,
        )
        if any(getattr(old, key) != getattr(config, key) for key in (
            "hotkey_video", "hotkey_menu", "hotkey_stop_transcribe"
        )):
            replacement = HotkeyListener(
                on_video_switch=self._switch_to_video_mode,
                on_show_dialog=self._handle_dialog_request,
                on_stop_transcribe=self._handle_stop_transcribe_request,
                video_hotkey=config.hotkey_video,
                menu_hotkey=config.hotkey_menu,
                stop_transcribe_hotkey=config.hotkey_stop_transcribe,
            )
            self.hotkeys.stop()
            try:
                replacement.start()
            except Exception:
                self.hotkeys.start()
                raise
            self.hotkeys = replacement
        self.recorder.configure(config)
        self.monitor.poll_interval = config.poll_interval
        self.monitor.call_end_grace_seconds = config.call_end_grace_seconds
        with self._settings_lock:
            self.transcriber = transcriber
            self.webex = webex
            self.config = config
        logger.info("Settings applied to running service.")

    def status(self) -> tuple[bool, str]:
        title = self._active_session.display_title if self._active_session else "Ready for calls"
        return self.recorder.is_recording, title

    def deliver_transcript(
        self,
        transcript_file: Path,
        meeting_title: str,
        *,
        delivery_enabled: bool | None = None,
        webex: WebexClient | None = None,
    ) -> None:
        if delivery_enabled is None:
            delivery_enabled = self.config.webex_delivery_enabled
        if not delivery_enabled:
            logger.info("Webex delivery disabled; transcript saved locally: %s", transcript_file)
            return
        client = webex if webex is not None else self.webex
        self.ui.show_notification("Webex OBS Companion", "Delivering transcript to Webex / My Agent...")
        sent = client.send_transcript(transcript_file, meeting_title=meeting_title)
        if sent:
            self.ui.show_notification("Webex OBS Companion", "Summary request delivered to Webex!")
        else:
            self.ui.show_notification("Webex OBS Companion", "Webex delivery failed. Check stdout.log")

    def _process_post_processing_job(self, job: PostProcessingJob) -> None:
        """Transcribe, deliver, and clean up one finalized meeting."""
        try:
            self.ui.show_notification(
                "Webex OBS Companion",
                f"Transcribing and diarizing {job.meeting_title}...",
            )
            transcript_file = job.transcriber.transcribe_files(
                list(job.media_files),
                meeting_title=job.meeting_title,
                output_stem=job.output_stem,
            )
            if transcript_file:
                self.deliver_transcript(
                    transcript_file,
                    job.meeting_title,
                    delivery_enabled=job.delivery_enabled,
                    webex=job.webex,
                )
        finally:
            MediaCleaner.prune_old_recordings(
                job.recordings_dir,
                job.retention_days,
            )

    def _new_recording_session(self) -> RecordingSession:
        title = self.monitor.current_call_title or self.monitor.get_active_call_title()
        title = title or self.monitor.default_call_title
        session = RecordingSession.create(title)
        logger.info("Recording session title: '%s'.", session.display_title)
        return session

    def _handle_stop_transcribe_request(self):
        """Handle Cmd+Shift+S hotkey to immediately stop recording and start transcription."""
        if not self.recorder.is_recording:
            self.ui.show_notification("Webex OBS Companion", "No active meeting recording currently running.")
            return

        logger.info(
            "Manual Stop & Transcribe requested via hotkey (%s).",
            display_hotkey(self.config.hotkey_stop_transcribe),
        )
        self.ui.show_notification("Webex OBS Companion", "Stopping recording and initiating MLX transcription...")
        self._manual_stop_event.set()

    def control_prompt_state(self) -> tuple[bool, str]:
        """Read cached state; accessibility window probes can stall UI presentation."""
        meeting_title = (
            self._active_session.display_title
            if self._active_session
            else self.monitor.current_call_title or self.monitor.default_call_title
        )
        return self.recorder.is_recording, meeting_title

    def _handle_dialog_request(self):
        """Handle the global controls hotkey from its background listener."""
        is_recording, meeting_title = self.control_prompt_state()
        choice = self.ui.show_control_prompt(
            is_recording=is_recording,
            meeting_title=meeting_title,
            is_video_recording=self.recorder.is_video_recording is True,
        )
        self._handle_control_choice(choice)

    def _switch_to_video_mode(self) -> None:
        self.recorder.switch_to_video_mode()
        if (self.recorder.is_video_recording is True
                and self.config.shared_window_behavior == "prompt"
                and len(self.recorder.list_webex_windows()) > 1):
            self.request_window_selection()

    def _handle_control_choice(self, choice: str) -> None:
        if choice == "stop_transcribe":
            self._handle_stop_transcribe_request()
        elif choice == "switch_video" and self.recorder.is_video_recording is not True:
            self._switch_to_video_mode()
            self.ui.show_notification("Webex OBS Companion", "Switched to Video recording mode.")
        elif choice == "select_window":
            self.request_window_selection()
        elif choice == "start_audio":
            self._request_manual_start("audio")
        elif choice == "start_video":
            self._request_manual_start("video")
        elif choice == "stop_discard" and self.recorder.is_recording:
            logger.info("User confirmed Stop & Discard via recording controls.")
            self._discard_requested = True
            self._manual_stop_event.set()

    def _request_manual_start(self, mode: str) -> None:
        if self.recorder.is_recording or self._manual_start_event.is_set():
            return
        self._manual_start_mode = mode
        self._manual_start_event.set()
        logger.info("Manual %s recording requested from controls.", mode)

    def request_window_selection(self, suggested_window_id: int | None = None) -> None:
        """Show the picker without holding up the recording lifecycle worker."""
        if self.recorder.is_video_recording is not True or self._window_prompt_pending.is_set():
            return
        windows = self.recorder.list_webex_windows()
        if not windows:
            self.ui.show_notification("Webex OBS Companion", "No Webex recording windows are available.")
            return
        self._window_prompt_pending.set()
        generation = self._window_session_generation

        def choose() -> None:
            try:
                window_id = self.ui.choose_webex_window(windows, suggested_window_id)
                if window_id is not None:
                    self._window_selection_queue.put((generation, window_id))
            except Exception:
                logger.exception("Could not show the Webex window picker")
            finally:
                self._window_prompt_pending.clear()

        threading.Thread(target=choose, name="webex-window-picker", daemon=True).start()

    def _update_video_window(self) -> None:
        if self.recorder.is_video_recording is not True:
            return
        try:
            while True:
                generation, window_id = self._window_selection_queue.get_nowait()
                if generation == self._window_session_generation:
                    if self.recorder.select_webex_window(window_id):
                        self.ui.show_notification("Webex OBS Companion", "Video recording window changed.")
                    else:
                        self.ui.show_notification("Webex OBS Companion", "Could not select that Webex window.")
        except queue.Empty:
            pass
        candidates = self.recorder.poll_webex_window_change()
        if not candidates:
            return
        suggested_id = max(candidates, key=lambda w: w.area).window_id
        if self.config.shared_window_behavior == "always_switch":
            if self.recorder.select_webex_window(suggested_id):
                self.ui.show_notification("Webex OBS Companion", "Following a new Webex window.")
            else:
                self.ui.show_notification("Webex OBS Companion", "Could not follow the new Webex window.")
        elif not self._window_prompt_pending.is_set():
            self.request_window_selection(suggested_id)

    def start(self):
        logger.info("Starting Webex OBS Companion Daemon...")

        # Pre-flight check for ffmpeg
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg not found in PATH! Please install ffmpeg with: brew install ffmpeg")

        self.hotkeys.start()
        self._post_processing_worker.start()

        # Launch and verify OBS once at service start; a newly launched instance
        # is closed by initialize after its idle status is confirmed.
        if self.recorder.initialize():
            logger.info("Verified OBS Studio WebSocket (%s:%s).", self.config.obs_address, self.config.obs_port)
        else:
            logger.warning("Could not verify OBS Studio at startup; recording start will retry.")

        MediaCleaner.prune_old_recordings(self.config.recordings_dir, self.config.retention_days)

        try:
            while True:
                meeting_active = self.monitor.wait_for_state_change(self._manual_start_event)
                if not meeting_active:
                    continue
                manual = self._manual_start_event.is_set()
                mode = self._manual_start_mode if manual else "audio"
                self._manual_start_event.clear()

                if not manual and self.monitor.should_suppress_automatic_prompt():
                    logger.info(
                        "Skipping automatic recording prompt for a call already declined by the user."
                    )
                    self.monitor.is_in_meeting = False
                    continue

                self._discard_requested = False
                self._manual_stop_event.clear()
                self._window_session_generation = getattr(self, "_window_session_generation", 0) + 1
                self._active_session = self._new_recording_session()
                if manual:
                    self.monitor.is_in_meeting = True

                started = self.recorder.start_recording(mode=mode)
                if not started:
                    logger.warning("OBS did not start recording; retrying with the running OBS instance.")
                    for _ in range(2):
                        time.sleep(1.0)
                        started = self.recorder.retry_start_recording(mode=mode)
                        if started:
                            break
                    while not manual and self.monitor.is_call_active() and not started:
                        time.sleep(5)
                        started = self.recorder.start_recording(mode=mode)
                    if not started:
                        if manual:
                            self.ui.show_notification(
                                "Webex OBS Companion", "Recording could not start. Check OBS and the service log."
                            )
                        self.monitor.is_in_meeting = False
                        self._active_session = None
                        continue

                session = self._active_session
                if session:
                    session.use_title_if_missing(
                        self.monitor.get_active_call_title()
                    )
                choice = "keep_audio" if manual else self.ui.show_startup_prompt(
                    meeting_title=(
                        session.display_title if session else self.monitor.default_call_title
                    )
                )
                if choice == "cancel":
                    logger.info(
                        "User cancelled recording. Discarding and suppressing further automatic prompts for this call..."
                    )
                    self.monitor.suppress_current_call_prompt(
                        session.display_title
                        if session and session.display_title != self.monitor.default_call_title
                        else None
                    )
                    discarded = self.recorder.stop_recording()
                    for f in discarded:
                        if os.path.exists(f):
                            try:
                                os.remove(f)
                            except Exception:
                                pass
                    self.monitor.is_in_meeting = False
                    self._active_session = None
                    continue
                elif choice == "switch_video":
                    self._switch_to_video_mode()

                if (mode == "video" and self.recorder.is_video_recording is True
                        and self.config.shared_window_behavior == "prompt"
                        and len(self.recorder.list_webex_windows()) > 1):
                    self.request_window_selection()

                self.ui.show_notification(
                    "Webex OBS Companion",
                    f"Recording active ({'Video' if mode == 'video' or choice == 'switch_video' else 'Audio'}). "
                    f"{display_hotkey(self.config.hotkey_video)} Video, "
                    f"{display_hotkey(self.config.hotkey_stop_transcribe)} Transcribe, "
                    f"{display_hotkey(self.config.hotkey_menu)} Menu."
                )

                # Wait for meeting process termination OR manual stop hotkey
                while self.monitor.is_in_meeting:
                    if self._manual_stop_event.is_set():
                        break
                    time.sleep(self.config.poll_interval)
                    if self._manual_stop_event.is_set():
                        break
                    if not manual and self.monitor.has_call_ended():
                        self.monitor.is_in_meeting = False
                        break
                    if not self.recorder.ensure_recording():
                        logger.error("OBS recovery failed; another recovery attempt will be made on the next poll.")
                    self._update_video_window()

                if self._discard_requested:
                    discarded = self.recorder.stop_recording()
                    if self.recorder.is_recording:
                        logger.error("OBS stop could not be verified; keeping recording files.")
                        self.ui.show_notification(
                            "Webex OBS Companion",
                            "Could not verify recording stopped; it may still be active. Files were kept.",
                        )
                    else:
                        failed = []
                        for filename in discarded:
                            try:
                                os.remove(filename)
                            except FileNotFoundError:
                                pass
                            except OSError:
                                logger.exception("Could not delete discarded recording: %s", filename)
                                failed.append(filename)
                        if failed:
                            self.ui.show_notification(
                                "Webex OBS Companion",
                                "Recording stopped, but some files could not be deleted.",
                            )
                        else:
                            logger.info("Recording stopped and discarded by user request.")
                            self.ui.show_notification("Webex OBS Companion", "Recording discarded.")
                    if not manual and self.monitor.is_call_active():
                        self.monitor.suppress_current_call_prompt()
                    self.monitor.is_in_meeting = False
                    self._discard_requested = False
                    self._manual_stop_event.clear()
                    self._active_session = None
                    continue

                if (not manual and self._manual_stop_event.is_set()) or (manual and self.monitor.is_call_active()):
                    self.monitor.suppress_current_call_prompt()
                if manual or self._manual_stop_event.is_set():
                    self.monitor.is_in_meeting = False
                recorded_files = self.recorder.stop_recording()
                logger.info(f"Recording session stopped. Captured segments: {recorded_files}")

                if not recorded_files:
                    self._manual_stop_event.clear()
                    self._active_session = None
                    continue

                session = self._active_session or self._new_recording_session()
                session.use_title_if_missing(self.monitor.current_call_title)
                recorded_files = session.rename_recordings(recorded_files)

                with self._settings_lock:
                    job = PostProcessingJob(
                        media_files=tuple(recorded_files),
                        meeting_title=session.display_title,
                        output_stem=session.filename_stem,
                        transcriber=self.transcriber,
                        webex=self.webex,
                        delivery_enabled=self.config.webex_delivery_enabled,
                        recordings_dir=self.config.recordings_dir,
                        retention_days=self.config.retention_days,
                    )
                self._post_processing_worker.submit(job)
                logger.info(
                    "Queued recording session for background post-processing: '%s'.",
                    session.display_title,
                )

                self._manual_stop_event.clear()
                self._active_session = None

        except KeyboardInterrupt:
            logger.info("Shutting down daemon...")
        finally:
            self._post_processing_worker.shutdown(wait=False)
            self.hotkeys.stop()
            self.recorder.disconnect()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    daemon = WebexOBSDaemon()
    if sys.platform == "darwin":
        from .macos_ui import MacOSUI

        ui = MacOSUI(daemon)
        daemon.ui = ui
        ui.run()
    else:
        daemon.start()


if __name__ == "__main__":
    main()
