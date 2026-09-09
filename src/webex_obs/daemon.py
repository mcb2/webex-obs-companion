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
from .config import Config, Settings, settings
from .hotkey_listener import HotkeyListener, display_hotkey
from .obs_controller import OBSController
from .process_monitor import ProcessMonitor
from .transcriber import Transcriber
from .ui_banner import UIBanner
from .webex_client import WebexClient

logger = logging.getLogger(__name__)


class WebexOBSDaemon:
    def __init__(self, config: Config | None = None):
        self.config = config or settings
        self.obs = OBSController(
            address=self.config.obs_address,
            port=self.config.obs_port,
            password=self.config.obs_password,
            relaunch_per_call=self.config.relaunch_obs_per_call,
        )
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
            on_video_switch=self.obs.switch_to_video_mode,
            on_show_dialog=self._handle_dialog_request,
            on_stop_transcribe=self._handle_stop_transcribe_request,
            video_hotkey=self.config.hotkey_video,
            menu_hotkey=self.config.hotkey_menu,
            stop_transcribe_hotkey=self.config.hotkey_stop_transcribe,
        )
        self._manual_stop_event = threading.Event()
        self._discard_requested = False

    def _handle_stop_transcribe_request(self):
        """Handle Cmd+Shift+S hotkey to immediately stop recording and start transcription."""
        if not self.obs.is_recording:
            UIBanner.show_notification("Webex OBS Companion", "No active meeting recording currently running.")
            return

        logger.info(
            "Manual Stop & Transcribe requested via hotkey (%s).",
            display_hotkey(self.config.hotkey_stop_transcribe),
        )
        UIBanner.show_notification("Webex OBS Companion", "Stopping recording and initiating MLX transcription...")
        self._manual_stop_event.set()

    def _handle_dialog_request(self):
        """Handle Cmd+Shift+R hotkey to bring up recording controls anytime."""
        choice = UIBanner.show_control_prompt(is_recording=self.obs.is_recording)

        if choice == "stop_transcribe":
            self._handle_stop_transcribe_request()
        elif choice == "switch_video":
            self.obs.switch_to_video_mode()
            UIBanner.show_notification("Webex OBS Companion", "Switched to Video recording mode.")
        elif choice == "start_audio":
            logger.info("Manual Start Audio recording triggered from menu.")
            if self.obs.start_recording(scene_name="Webex-Audio"):
                self.monitor.is_in_meeting = True
                UIBanner.show_notification("Webex OBS Companion", "Manual Audio recording started.")
        elif choice == "start_video":
            logger.info("Manual Start Video recording triggered from menu.")
            if self.obs.start_recording(scene_name="Webex-Video"):
                self.monitor.is_in_meeting = True
                UIBanner.show_notification("Webex OBS Companion", "Manual Video recording started.")
        elif choice == "cancel":
            logger.info("User requested Cancel & Discard via dialog.")
            self._discard_requested = True
            discarded = self.obs.stop_recording()
            for f in discarded:
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
            UIBanner.show_notification("Webex OBS Companion", "Recording discarded.")

    def start(self):
        logger.info("Starting Webex OBS Companion Daemon...")

        # Pre-flight check for ffmpeg
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg not found in PATH! Please install ffmpeg with: brew install ffmpeg")

        self.hotkeys.start()

        # Startup OBS WebSocket connection test & log
        if self.obs.connect():
            logger.info(f"Connected to OBS Studio WebSocket ({self.config.obs_address}:{self.config.obs_port}).")
        else:
            logger.info(
                f"OBS Studio is not currently running. It will be launched automatically when a Webex call starts."
            )

        MediaCleaner.prune_old_recordings(self.config.recordings_dir, self.config.retention_days)

        try:
            while True:
                meeting_active = self.monitor.wait_for_state_change()
                if not meeting_active:
                    continue

                self._discard_requested = False
                self._manual_stop_event.clear()

                started = self.obs.start_recording(scene_name="Webex-Audio")
                if not started:
                    logger.warning("Could not start OBS recording. Will retry while the call remains active...")
                    while self.monitor.is_webex_running() and not started:
                        time.sleep(5)
                        started = self.obs.start_recording(scene_name="Webex-Audio", relaunch=True)
                    if not started:
                        self.monitor.is_in_meeting = False
                        continue

                choice = UIBanner.show_startup_prompt()
                if choice == "cancel":
                    logger.info("User cancelled recording. Discarding...")
                    discarded = self.obs.stop_recording()
                    for f in discarded:
                        if os.path.exists(f):
                            try:
                                os.remove(f)
                            except Exception:
                                pass
                    while self.monitor.is_webex_running():
                        time.sleep(self.config.poll_interval)
                    self.monitor.is_in_meeting = False
                    continue
                elif choice == "switch_video":
                    self.obs.switch_to_video_mode()

                UIBanner.show_notification(
                    "Webex OBS Companion",
                    "Recording active (Audio). "
                    f"{display_hotkey(self.config.hotkey_video)} Video, "
                    f"{display_hotkey(self.config.hotkey_stop_transcribe)} Transcribe, "
                    f"{display_hotkey(self.config.hotkey_menu)} Menu."
                )

                # Wait for meeting process termination OR manual stop hotkey
                while self.monitor.is_in_meeting:
                    if self._manual_stop_event.is_set():
                        break
                    time.sleep(self.config.poll_interval)
                    if self.monitor.has_call_ended():
                        self.monitor.is_in_meeting = False
                        break
                    if not self.obs.ensure_recording():
                        logger.error("OBS recovery failed; another recovery attempt will be made on the next poll.")

                if self._discard_requested:
                    logger.info("Meeting finished, recording was discarded by user request.")
                    self._discard_requested = False
                    self._manual_stop_event.clear()
                    continue

                recorded_files = self.obs.stop_recording()
                logger.info(f"Recording session stopped. Captured segments: {recorded_files}")

                if not recorded_files:
                    self._manual_stop_event.clear()
                    continue

                UIBanner.show_notification("Webex OBS Companion", "Transcribing and diarizing meeting audio...")
                transcript_file = self.transcriber.transcribe_files(recorded_files)

                if transcript_file:
                    UIBanner.show_notification("Webex OBS Companion", "Delivering transcript to Webex / My Agent...")
                    sent = self.webex.send_transcript(transcript_file, meeting_title="Webex Session")
                    if sent:
                        UIBanner.show_notification("Webex OBS Companion", "Summary request delivered to Webex!")
                    else:
                        UIBanner.show_notification("Webex OBS Companion", "Webex delivery failed. Check stdout.log")

                self._manual_stop_event.clear()
                MediaCleaner.prune_old_recordings(self.config.recordings_dir, self.config.retention_days)

        except KeyboardInterrupt:
            logger.info("Shutting down daemon...")
        finally:
            self.hotkeys.stop()
            self.obs.disconnect()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    daemon = WebexOBSDaemon()
    daemon.start()


if __name__ == "__main__":
    main()
