import logging
import os
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
            on_video_switch=self.recorder.switch_to_video_mode,
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
                on_video_switch=self.recorder.switch_to_video_mode,
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
        self.transcriber = transcriber
        self.webex = webex
        self.config = config
        logger.info("Settings applied to running service.")

    def status(self) -> tuple[bool, str]:
        title = self._active_session.display_title if self._active_session else "Ready for calls"
        return self.recorder.is_recording, title

    def deliver_transcript(self, transcript_file: Path, meeting_title: str) -> None:
        if not self.config.webex_delivery_enabled:
            logger.info("Webex delivery disabled; transcript saved locally: %s", transcript_file)
            return
        self.ui.show_notification("Webex OBS Companion", "Delivering transcript to Webex / My Agent...")
        sent = self.webex.send_transcript(transcript_file, meeting_title=meeting_title)
        if sent:
            self.ui.show_notification("Webex OBS Companion", "Summary request delivered to Webex!")
        else:
            self.ui.show_notification("Webex OBS Companion", "Webex delivery failed. Check stdout.log")

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

    def _handle_dialog_request(self):
        """Handle Cmd+Shift+R hotkey to bring up recording controls anytime."""
        if self._active_session:
            self._active_session.use_title_if_missing(
                self.monitor.get_active_call_title()
            )
        meeting_title = (
            self._active_session.display_title
            if self._active_session
            else self.monitor.current_call_title or self.monitor.default_call_title
        )
        choice = self.ui.show_control_prompt(
            is_recording=self.recorder.is_recording,
            meeting_title=meeting_title,
        )

        if choice == "stop_transcribe":
            self._handle_stop_transcribe_request()
        elif choice == "switch_video":
            self.recorder.switch_to_video_mode()
            self.ui.show_notification("Webex OBS Companion", "Switched to Video recording mode.")
        elif choice == "start_audio":
            self._request_manual_start("audio")
        elif choice == "start_video":
            self._request_manual_start("video")
        elif choice == "cancel":
            logger.info("User requested Cancel & Discard via dialog.")
            self._discard_requested = True
            discarded = self.recorder.stop_recording()
            for f in discarded:
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
            self.ui.show_notification("Webex OBS Companion", "Recording discarded.")
            self._active_session = None

    def _request_manual_start(self, mode: str) -> None:
        if self.recorder.is_recording or self._manual_start_event.is_set():
            return
        self._manual_start_mode = mode
        self._manual_start_event.set()
        logger.info("Manual %s recording requested from controls.", mode)

    def start(self):
        logger.info("Starting Webex OBS Companion Daemon...")

        # Pre-flight check for ffmpeg
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg not found in PATH! Please install ffmpeg with: brew install ffmpeg")

        self.hotkeys.start()

        # Startup OBS WebSocket connection test & log
        if self.recorder.connect():
            logger.info(f"Connected to OBS Studio WebSocket ({self.config.obs_address}:{self.config.obs_port}).")
        else:
            logger.info(
                "OBS Studio is not currently running. It will be launched automatically "
                "when a supported call starts."
            )

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
                self._active_session = self._new_recording_session()
                if manual:
                    self.monitor.is_in_meeting = True

                started = self.recorder.start_recording(mode=mode)
                if not started:
                    logger.warning("Could not start OBS recording.%s",
                                   " Will retry while the call remains active..." if not manual else "")
                    while not manual and self.monitor.is_call_active() and not started:
                        time.sleep(5)
                        started = self.recorder.start_recording(mode=mode, relaunch=True)
                    if not started:
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
                    self.recorder.switch_to_video_mode()

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

                if self._discard_requested:
                    logger.info("Meeting finished, recording was discarded by user request.")
                    self._discard_requested = False
                    self._manual_stop_event.clear()
                    self._active_session = None
                    if manual:
                        self.monitor.is_in_meeting = False
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

                self.ui.show_notification("Webex OBS Companion", "Transcribing and diarizing meeting audio...")
                transcript_file = self.transcriber.transcribe_files(
                    recorded_files,
                    meeting_title=session.display_title,
                    output_stem=session.filename_stem,
                )

                if transcript_file:
                    self.deliver_transcript(transcript_file, session.display_title)

                self._manual_stop_event.clear()
                self._active_session = None
                MediaCleaner.prune_old_recordings(self.config.recordings_dir, self.config.retention_days)

        except KeyboardInterrupt:
            logger.info("Shutting down daemon...")
        finally:
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
