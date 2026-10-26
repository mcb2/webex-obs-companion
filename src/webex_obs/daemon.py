import logging
import os
import queue
import shutil
import signal
import sys
import threading
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
from .job_store import JobStore
from .runtime_state import ApplicationStopping, RuntimeState
from .post_processing import PostProcessingJob, PostProcessingWorker
from .recording_backend import RecordingBackend, create_recording_backend
from .process_monitor import ProcessMonitor
from .session import RecordingSession
from .transcriber import Transcriber
from .ui_banner import UIBanner
from .webex_client import WebexClient

logger = logging.getLogger(__name__)


class WebexOBSDaemon:
    def __init__(self, config: Config | None = None, backend: RecordingBackend | None = None, ui=None, *, detection_diagnostics: bool = False):
        self.config = config or settings
        self.recorder = backend or create_recording_backend(self.config)
        # UI is injected so the lifecycle can run without AppKit (including tests).
        self.ui = ui or UIBanner
        self.monitor = ProcessMonitor(
            poll_interval=self.config.poll_interval,
            call_end_grace_seconds=self.config.call_end_grace_seconds,
        )
        self.diagnostics = None
        if detection_diagnostics:
            from .detection_diagnostics import DetectionDiagnostics

            self.diagnostics = DetectionDiagnostics(self.config.log_dir)
            self.monitor.diagnostics = self.diagnostics
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
            on_audio_start=self._handle_audio_start_request,
            on_video_switch=self._switch_to_video_mode,
            on_video_source=self.request_window_selection,
            on_stop_transcribe=self._handle_stop_transcribe_request,
            audio_hotkey=self.config.hotkey_audio,
            video_hotkey=self.config.hotkey_video,
            video_source_hotkey=self.config.hotkey_video_source,
            stop_transcribe_hotkey=self.config.hotkey_stop_transcribe,
        )
        self._manual_stop_event = threading.Event()
        self._manual_start_event = threading.Event()
        self._manual_start_mode = "audio"
        self._discard_requested = False
        self._delete_after_transcription = False
        self._stop_pending = False
        self._active_session: RecordingSession | None = None
        self._settings_lock = threading.Lock()
        # Protect request flags only; never hold this across OBS or AppKit work.
        self._recording_request_lock = threading.RLock()
        self._video_switch_generation: int | None = None
        self._window_selection_queue: queue.Queue[tuple[int, int | str]] = queue.Queue()
        self._window_prompt_pending = threading.Event()
        self._window_session_generation = 0
        self._initialize_lifecycle_state()
        self._job_store = JobStore(self.config.log_dir / "jobs")
        self._runtime = RuntimeState(self.config.log_dir)
        self._post_processing_worker = PostProcessingWorker(
            self._process_post_processing_job, on_fatal=self._worker_failed
        )

    def _initialize_lifecycle_state(self) -> None:
        self._settings_update_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._stopped_event = threading.Event()
        self._lifecycle_thread: threading.Thread | None = None
        self.lifecycle_state = "starting"
        self.failure: str | None = None
        self._job_store = None
        self._runtime = None

    def request_shutdown(self) -> None:
        """Nonblocking; the lifecycle worker alone finalizes capture."""
        with self._recording_request_lock:
            self._shutdown_event.set()
            self._manual_start_event.set()  # Wake idle detection, never start capture.
            self._manual_stop_event.set()
            self._video_switch_generation = None

    def _worker_failed(self) -> None:
        self.failure = "Post-processing worker failed; restart required."
        self.request_shutdown()

    def _pulse(self) -> None:
        if getattr(self, "diagnostics", None):
            self.diagnostics.record(
                "lifecycle", state=self.lifecycle_state,
                recording=self.recorder.is_recording is True,
                stop_pending=self._stop_pending,
                manual_start_pending=self._manual_start_event.is_set(),
                manual_stop_pending=self._manual_stop_event.is_set(),
                session_generation=self._window_session_generation,
            )
        if self._runtime is not None:
            self._runtime.report(self.lifecycle_state, self.config.poll_interval)

    def start_background(self) -> None:
        if self._lifecycle_thread is not None:
            return
        self._lifecycle_thread = threading.Thread(target=self.start, name="meeting-lifecycle", daemon=True)
        self._lifecycle_thread.start()

    def shutdown(self, timeout: float = 10.0) -> bool:
        """Wait at most timeout seconds; a stuck backend is never stopped concurrently."""
        self.request_shutdown()
        return self._stopped_event.wait(timeout)

    def start(self) -> None:
        """Supervise startup, lifecycle, and cleanup including partial startup failures."""
        acquired = False
        try:
            if self._runtime is not None:
                self._runtime.acquire()
                acquired = True
            if getattr(self, "diagnostics", None):
                self.diagnostics.start()
            self._pulse()
            self._run_lifecycle()
            if not self._shutdown_event.is_set():
                raise RuntimeError("Lifecycle worker returned unexpectedly")
        except (KeyboardInterrupt, ApplicationStopping):
            self.request_shutdown()
        except BaseException as exc:
            self.failure = f"Lifecycle failed ({type(exc).__name__}); check application logs."
            logger.exception("Lifecycle startup/worker failed; shutting down.")
            self.request_shutdown()
        finally:
            self.lifecycle_state = "stopping"
            if acquired or self._runtime is None:
                try:
                    self._pulse()
                except Exception:
                    self.failure = "Could not save application health; check logs."
                    logger.exception(self.failure)
                try:
                    # A previously ambiguous stop needs explicit user resolution.
                    if self.recorder.is_recording or self._active_session is not None:
                        if self._stop_pending:
                            self.failure = "Capture stop unresolved; check OBS. Media retained."
                        else:
                            self._discard_requested = False
                            if not self._finish_recording():
                                self.failure = "Capture stop unresolved; check OBS. Media retained."
                except BaseException:
                    self.failure = "Capture finalization failed; check OBS. Media retained."
                    logger.exception(self.failure)
                for cleanup in (
                    lambda: self._post_processing_worker.shutdown(wait=True, timeout=2.0, drain=False),
                    self._disconnect_settings_components,
                ):
                    try:
                        cleanup()
                    except BaseException:
                        self.failure = "Shutdown cleanup failed; check OBS and application logs."
                        logger.exception(self.failure)
            self.lifecycle_state = "failed" if self.failure else "stopped"
            try:
                if acquired:
                    self._pulse()
            except Exception:
                self.failure = "Could not save final application health; check logs."
                self.lifecycle_state = "failed"
                logger.exception(self.failure)
            finally:
                if getattr(self, "diagnostics", None):
                    self.diagnostics.close()
                self._stopped_event.set()
            # Retain ownership until process exit, including any inference/network
            # worker still running after the bounded wait.

    def _disconnect_settings_components(self) -> None:
        # Settings now runs off-thread. Do not let an in-flight save register
        # new listeners after lifecycle cleanup has stopped the previous ones.
        with self._settings_update_lock:
            try:
                self.hotkeys.stop()
            finally:
                self.recorder.disconnect()

    def save_settings(self, values: dict[str, str | bool], path: Path | None = None) -> Config:
        """Serialize persistence and runtime application on the Settings worker."""
        from .settings_store import save_settings

        with self._settings_update_lock:
            if self._shutdown_event.is_set():
                raise ApplicationStopping("Application is shutting down")
            return save_settings(values, path, current=self.config, apply=self._apply_settings)

    def apply_settings(self, config: Config) -> None:
        """Install validated settings; restore changed components on failure."""
        with self._settings_update_lock:
            self._apply_settings(config)

    def _apply_settings(self, config: Config) -> None:
        if self._shutdown_event.is_set():
            raise ApplicationStopping("Application is shutting down")
        old = self.config
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
        old_hotkeys = self.hotkeys
        replacement = None
        if any(getattr(old, key) != getattr(config, key) for key in (
            "hotkey_audio", "hotkey_video", "hotkey_video_source", "hotkey_stop_transcribe"
        )):
            replacement = HotkeyListener(
                on_audio_start=self._handle_audio_start_request,
                on_video_switch=self._switch_to_video_mode,
                on_video_source=self.request_window_selection,
                on_stop_transcribe=self._handle_stop_transcribe_request,
                audio_hotkey=config.hotkey_audio,
                video_hotkey=config.hotkey_video,
                video_source_hotkey=config.hotkey_video_source,
                stop_transcribe_hotkey=config.hotkey_stop_transcribe,
            )
        recorder_attempted = False
        hotkeys_attempted = False
        old_poll = self.monitor.poll_interval
        old_grace = self.monitor.call_end_grace_seconds
        try:
            if self._shutdown_event.is_set():
                raise ApplicationStopping("Application is shutting down")
            if replacement is not None:
                hotkeys_attempted = True
                old_hotkeys.stop()
                replacement.start()
            recorder_attempted = True
            self.recorder.configure(config)
            self.monitor.poll_interval = config.poll_interval
            self.monitor.call_end_grace_seconds = config.call_end_grace_seconds
        except Exception:
            # Include the component that raised: an integration may mutate then fail.
            rollback = [
                lambda: setattr(self.monitor, "poll_interval", old_poll),
                lambda: setattr(self.monitor, "call_end_grace_seconds", old_grace),
            ]
            if recorder_attempted:
                rollback.append(lambda: self.recorder.configure(old))
            if hotkeys_attempted:
                rollback.extend((replacement.stop, old_hotkeys.start))
            failed = False
            for restore in rollback:
                try:
                    restore()
                except Exception as exc:
                    failed = True
                    logger.error("Runtime settings rollback failed (%s).", type(exc).__name__)
            if failed:
                from .settings_store import SettingsRollbackError
                raise SettingsRollbackError("Runtime rollback failed; restart after finishing recording.") from None
            raise
        with self._settings_lock:
            self.transcriber = transcriber
            self.webex = webex
            self.config = config
            if replacement is not None:
                self.hotkeys = replacement
        logger.info("Settings applied to running service.")

    def status(self) -> tuple[bool, str]:
        title = self.failure or (self._active_session.display_title if self._active_session else "Ready for calls")
        return self.recorder.is_recording, title

    def deliver_transcript(
        self,
        transcript_file: Path,
        meeting_title: str,
        *,
        delivery_enabled: bool | None = None,
        webex: WebexClient | None = None,
    ) -> bool:
        if delivery_enabled is None:
            delivery_enabled = self.config.webex_delivery_enabled
        if not delivery_enabled:
            logger.info("Webex delivery disabled; transcript saved locally: %s", transcript_file)
            return True
        client = webex if webex is not None else self.webex
        self.ui.show_notification("Webex OBS Companion", "Delivering transcript to Webex...")
        sent = client.send_transcript(transcript_file, meeting_title=meeting_title)
        if sent:
            self.ui.show_notification("Webex OBS Companion", "Transcript delivered to Webex.")
        elif client.last_delivery_outcome == "uncertain":
            self.ui.show_notification(
                "Webex OBS Companion",
                "Webex delivery is uncertain. Check Webex before retrying; transcript kept locally.",
            )
        else:
            self.ui.show_notification(
                "Webex OBS Companion", "Webex delivery failed. Transcript kept locally; check application logs."
            )
        return bool(sent)

    def _process_post_processing_job(self, job: PostProcessingJob) -> None:
        """Transcribe, deliver, and clean up one finalized meeting."""
        files = list(job.media_files)
        MediaCleaner.protect_recordings(files)
        identities = MediaCleaner.recording_identities(files)
        self.ui.show_notification(
            "Webex OBS Companion",
            f"Transcribing and diarizing {job.meeting_title}...",
        )
        record = job.journal.read(job.job_id) if job.journal else None
        if record and record["state"] not in {"queued", "ready", "delivered"}:
            return
        if record and record["state"] != "delivered" and not (
            record["state"] == "ready" and job.retention_days == 0
        ):
            if identities != record["identities"]:
                job.journal.update(job.job_id, state="failed")
                logger.error("Job media changed or is missing; manual recovery required.")
                return
        if record and record["state"] == "queued":
            job.journal.update(job.job_id, state="transcribing")
        if record and record["state"] in {"ready", "delivered"}:
            transcript_file = Path(record["transcript"])
            if not transcript_file.is_file():
                logger.error("Checkpoint transcript missing; manual recovery required.")
                job.journal.update(job.job_id, state="failed")
                return
        else:
            options = {"output_path": Path(record["transcript"])} if record else {}
            transcript_file = job.transcriber.transcribe_files(
                files, meeting_title=job.meeting_title, output_stem=job.output_stem, **options,
            )
        if not transcript_file:
            if job.journal:
                job.journal.update(job.job_id, state="failed")
            logger.warning("Processing incomplete; media remains protected for recovery.")
            return
        if record and record["state"] == "queued":
            job.journal.update(job.job_id, state="ready")
        if job.retention_days == 0:
            # The ready checkpoint must precede deletion so restart can deliver
            # the saved transcript without requiring the intentionally removed media.
            MediaCleaner.mark_processed(files, job.recordings_dir,
                                       identities=record["identities"] if record else identities)
            MediaCleaner.prune_old_recordings(job.recordings_dir, 0, media_files=files)
        if self._shutdown_event.is_set():
            return
        if not record or record["state"] != "delivered":
            if job.journal:
                # Write before the request. A crash after acceptance but before the
                # next checkpoint must never cause an automatic duplicate send.
                job.journal.update(job.job_id, state="delivering" if job.delivery_enabled else "ready")
            if not self.deliver_transcript(
                transcript_file, job.meeting_title,
                delivery_enabled=job.delivery_enabled, webex=job.webex,
            ):
                if job.journal:
                    state = "uncertain" if job.webex.last_delivery_outcome == "uncertain" else "failed"
                    job.journal.update(job.job_id, state=state)
                logger.warning("Delivery incomplete; transcript retained for recovery.")
                return
            if job.journal:
                job.journal.update(job.job_id, state="delivered")
        if job.retention_days != 0:
            MediaCleaner.mark_processed(files, job.recordings_dir,
                                       identities=record["identities"] if record else identities)
        if job.journal:
            job.journal.update(job.job_id, state="complete")
        if job.retention_days != 0:
            MediaCleaner.prune_old_recordings(job.recordings_dir, job.retention_days)

    def _finish_recording(self) -> bool:
        """Finalize only on positive stop evidence; retain the pending action otherwise."""
        with self._recording_request_lock:
            self._stop_pending = True
            self._video_switch_generation = None
        recorded_files = self.recorder.stop_recording()
        self._manual_stop_event.clear()
        if self.recorder.stop_verified is not True:
            self._stop_pending = True
            logger.error("OBS stop could not be verified; retaining session, files and pending action.")
            self.ui.show_notification(
                "Webex OBS Companion",
                "Could not verify recording stopped. Files kept. Check OBS, then retry Stop from the menu.",
            )
            return False
        if self._shutdown_event.is_set():
            self._discard_requested = False
        if self._discard_requested:
            failed = []
            for filename in recorded_files:
                try:
                    os.remove(filename)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.exception("Could not delete discarded recording: %s", filename)
                    failed.append(filename)
            self.ui.show_notification(
                "Webex OBS Companion",
                "Recording stopped, but some files could not be deleted." if failed else "Recording discarded.",
            )
        elif recorded_files:
            session = self._active_session or self._new_recording_session()
            session.use_title_if_missing(self.monitor.current_call_title)
            recorded_files = session.rename_recordings(recorded_files)
            MediaCleaner.protect_recordings(recorded_files)
            recording_directory = self.recorder.recording_directory
            with self._settings_lock:
                job = PostProcessingJob(
                    media_files=tuple(recorded_files),
                    meeting_title=session.display_title,
                    output_stem=session.filename_stem,
                    transcriber=self.transcriber,
                    webex=self.webex,
                    delivery_enabled=self.config.webex_delivery_enabled,
                    recordings_dir=(recording_directory if isinstance(recording_directory, Path)
                                    else self.config.recordings_dir),
                    retention_days=(0 if getattr(self, "_delete_after_transcription", False)
                                    else self.config.retention_days),
                    journal=self._job_store,
                )
            if self._shutdown_event.is_set() and job.journal is not None:
                job.journal.create(job)
            else:
                self._post_processing_worker.submit(job)
            logger.info("Queued recording session for background post-processing: '%s'.", session.display_title)
        with self._recording_request_lock:
            self._discard_requested = False
            self._delete_after_transcription = False
            self._active_session = None
            self._stop_pending = False
            self.monitor.release_platform()
            self._window_session_generation += 1
        return True

    def _new_recording_session(self) -> RecordingSession:
        # Metadata must not delay OBS startup, particularly for manual requests.
        title = self.monitor.current_call_title
        title = title or self.monitor.default_call_title
        session = RecordingSession.create(title)
        logger.info("Recording session title: '%s'.", session.display_title)
        return session

    def toggle_delete_after_transcription(self, generation: int) -> None:
        """Change only the clicked session; reject delayed actions after stop/start."""
        with self._recording_request_lock:
            if (generation != self._window_session_generation
                    or self._active_session is None or self.recorder.is_recording is not True
                    or self._stop_pending or self._manual_stop_event.is_set()
                    or self._shutdown_event.is_set() or self.config.retention_days == 0):
                return
            self._delete_after_transcription = not self._delete_after_transcription

    def _handle_stop_transcribe_request(self):
        """Handle Cmd+Shift+S hotkey to immediately stop recording and start transcription."""
        with self._recording_request_lock:
            recording = self.recorder.is_recording
            if recording:
                self._manual_stop_event.set()
                self._video_switch_generation = None
        if not recording:
            self.ui.show_notification("Webex OBS Companion", "No active meeting recording currently running.")
            return

        logger.info(
            "Manual Stop & Transcribe requested via hotkey (%s).",
            display_hotkey(self.config.hotkey_stop_transcribe),
        )
        self.ui.show_notification("Webex OBS Companion", "Stopping recording and initiating MLX transcription...")

    def _handle_audio_start_request(self):
        self._request_manual_start("audio")

    def request_discard_confirmation(self):
        session = self._active_session
        if not self.recorder.is_recording:
            return
        if self.ui.confirm_discard():
            with self._recording_request_lock:
                if self.recorder.is_recording and self._active_session is session:
                    self._handle_control_choice("stop_discard")

    def _switch_to_video_mode(self) -> None:
        """Menu/hotkey callbacks enqueue; only the lifecycle worker touches OBS."""
        with self._recording_request_lock:
            if self._stop_pending or self._manual_stop_event.is_set():
                return
            if not self.recorder.is_recording:
                self._request_manual_start("video")
                return
            if self.recorder.is_video_recording is True:
                return
            self._video_switch_generation = self._window_session_generation

    def _apply_video_switch(self) -> None:
        with self._recording_request_lock:
            generation = self._video_switch_generation
            self._video_switch_generation = None
            if (generation is None or generation != self._window_session_generation
                    or self._stop_pending or self._manual_stop_event.is_set()):
                return
        if self.recorder.is_video_recording is True or not self.recorder.is_recording:
            return
        if self.recorder.switch_to_video_mode() is not True:
            self.ui.show_notification("Webex OBS Companion", "Could not switch to Video recording mode.")
            return
        self.ui.show_notification("Webex OBS Companion", "Switched to Video recording mode.")
        if (self.recorder.is_video_recording is True
                and self.recorder.is_screen_recording is not True
                and self.config.shared_window_behavior == "prompt"
                and len(self.recorder.list_webex_windows()) > 1):
            self.request_window_selection()

    def _handle_control_choice(self, choice: str) -> None:
        if choice == "stop_transcribe":
            self._handle_stop_transcribe_request()
        elif (choice == "switch_video" and self.recorder.is_recording
              and self.recorder.is_video_recording is not True):
            self._switch_to_video_mode()
        elif choice == "select_window":
            self.request_window_selection()
        elif choice == "start_audio":
            self._request_manual_start("audio")
        elif choice == "start_video":
            self._request_manual_start("video")
        elif choice == "stop_discard":
            with self._recording_request_lock:
                if not self.recorder.is_recording:
                    return
                logger.info("User confirmed Stop & Discard via recording controls.")
                self._discard_requested = True
                self._manual_stop_event.set()
                self._video_switch_generation = None

    def _request_manual_start(self, mode: str) -> None:
        with self._recording_request_lock:
            if (self._shutdown_event.is_set() or self.recorder.is_recording or self._active_session is not None
                    or self._manual_start_event.is_set() or self._stop_pending):
                return
            self._manual_start_mode = mode
            self._manual_start_event.set()
        logger.info("Manual %s recording requested from controls.", mode)
        # The lifecycle may still be validating an automatic candidate. Warm OBS
        # immediately without doing WebSocket work on the UI/hotkey thread.
        threading.Thread(target=self.recorder.prepare_recording,
                         name="obs-prepare", daemon=True).start()

    def request_window_selection(self, suggested_window_id: int | None = None) -> None:
        """Show the picker without holding up the recording lifecycle worker."""
        with self._recording_request_lock:
            if (self.recorder.is_video_recording is not True or self._window_prompt_pending.is_set()
                    or self._stop_pending or self._manual_stop_event.is_set()):
                return
            self._window_prompt_pending.set()
            generation = self._window_session_generation

        def choose() -> None:
            try:
                windows = self.recorder.list_webex_windows()
                displays = self.recorder.list_capture_displays()
                if (generation != self._window_session_generation
                        or self._stop_pending or self._manual_stop_event.is_set()):
                    return
                if not windows and not displays:
                    self.ui.show_notification("Webex OBS Companion", "No recording windows or screens are available.")
                    return
                selection = self.ui.choose_webex_window(
                    windows, suggested_window_id, displays, self.recorder.selected_display_uuid
                )
                if selection is not None:
                    self._window_selection_queue.put((generation, selection))
            except Exception:
                logger.exception("Could not show the Webex window picker")
            finally:
                self._window_prompt_pending.clear()

        threading.Thread(target=choose, name="webex-window-picker", daemon=True).start()

    def _update_video_window(self) -> None:
        if (self.recorder.is_video_recording is not True
                or self._stop_pending or self._manual_stop_event.is_set()):
            return
        try:
            while True:
                generation, selection = self._window_selection_queue.get_nowait()
                if generation == self._window_session_generation:
                    if isinstance(selection, str):
                        changed = self.recorder.select_capture_display(selection)
                    else:
                        changed = self.recorder.select_webex_window(selection)
                    if changed:
                        self.ui.show_notification(
                            "Webex OBS Companion",
                            "Recording the entire screen." if isinstance(selection, str)
                            else "Video recording window changed.",
                        )
                    else:
                        self.ui.show_notification("Webex OBS Companion", "Could not select that recording source.")
        except queue.Empty:
            pass
        if self.recorder.is_screen_recording is True:
            return
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

    def _run_lifecycle(self):
        logger.info("Starting Webex OBS Companion Daemon...")

        # Pre-flight check for ffmpeg
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg not found in PATH! Please install ffmpeg with: brew install ffmpeg")

        if self._shutdown_event.is_set():
            return
        self.hotkeys.start()
        self._post_processing_worker.start()

        self.monitor.on_poll = self._pulse
        self.monitor.on_strong_evidence = self.recorder.prepare_recording
        self.monitor.on_possible_evidence = self.recorder.mark_call_evidence
        self.monitor.on_idle = lambda: self.recorder.maintain_idle(
            lambda: self._shutdown_event.is_set() or self._manual_start_event.is_set() or self.monitor.is_in_meeting
        )
        # Startup always leaves OBS warm; exit-after-recording applies only to stop.
        if self.recorder.initialize():
            logger.info("Verified OBS Studio WebSocket (%s:%s).", self.config.obs_address, self.config.obs_port)
        else:
            logger.warning("Could not verify OBS Studio at startup; recording start will retry.")

        if self._job_store is not None:
            for job in self._job_store.recover(self.config):
                self._post_processing_worker.submit(job)
        MediaCleaner.prune_old_recordings(self.config.recordings_dir, self.config.retention_days)
        self.lifecycle_state = "running"
        self._pulse()

        try:
            while not self._shutdown_event.is_set():
                self._pulse()
                if getattr(self, "_stop_pending", False):
                    # Explicit retries only: no repeated stop commands, new
                    # session, or automatic recovery while stop is unresolved.
                    if self._manual_stop_event.is_set():
                        self._finish_recording()
                    else:
                        self._shutdown_event.wait(self.config.poll_interval)
                    continue
                meeting_active = self.monitor.wait_for_state_change(self._manual_start_event)
                if self._shutdown_event.is_set():
                    break
                if not meeting_active:
                    continue
                with self._recording_request_lock:
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
                    self._delete_after_transcription = False
                    self._manual_stop_event.clear()
                    if not manual:
                        self.monitor.bind_current_platform()
                    self._window_session_generation += 1
                    self._active_session = self._new_recording_session()
                    if manual:
                        self.monitor.is_in_meeting = True

                if self._shutdown_event.is_set():
                    break
                started = self.recorder.start_recording(mode=mode)
                if not started:
                    logger.warning("OBS did not start recording; retrying with the running OBS instance.")
                    for _ in range(2):
                        if self.recorder.is_recording or self._shutdown_event.is_set():
                            break
                        self._shutdown_event.wait(1.0)
                        if self._shutdown_event.is_set():
                            break
                        started = self.recorder.retry_start_recording(mode=mode)
                        if started:
                            break
                    while (not self._shutdown_event.is_set() and not manual and not self.recorder.is_recording
                           and self.monitor.is_call_active() and not started):
                        self._shutdown_event.wait(5)
                        if self._shutdown_event.is_set():
                            break
                        started = self.recorder.start_recording(mode=mode)
                    if not started:
                        if self.recorder.is_recording:
                            self._stop_pending = True
                            self.monitor.is_in_meeting = False
                            self.ui.show_notification(
                                "Webex OBS Companion",
                                "Recording start could not be verified. Check OBS, then choose Stop to resolve it.",
                            )
                            continue
                        if manual:
                            self.ui.show_notification(
                                "Webex OBS Companion", "Recording could not start. Check OBS and the service log."
                            )
                        self.monitor.is_in_meeting = False
                        self._active_session = None
                        self.monitor.release_platform()
                        continue

                if self._shutdown_event.is_set():
                    break
                session = self._active_session
                if manual:
                    # Recording is already active. Do not delay this notice with
                    # accessibility/title enrichment or the video-source picker.
                    choice = self.ui.show_manual_consent_prompt(
                        meeting_title=session.display_title if session else self.monitor.default_call_title
                    )
                    if choice == "cancel":
                        self._discard_requested = True
                        self._manual_stop_event.set()
                    elif not self._manual_stop_event.is_set() and session:
                        session.use_title_if_missing(self.monitor.get_active_call_title())
                else:
                    if session:
                        session.use_title_if_missing(self.monitor.get_active_call_title())
                    choice = self.ui.show_startup_prompt(
                        meeting_title=session.display_title if session else self.monitor.default_call_title
                    )
                if self._shutdown_event.is_set():
                    break
                if choice == "cancel" and not manual:
                    logger.info(
                        "User cancelled recording. Discarding and suppressing further automatic prompts for this call..."
                    )
                    self.monitor.suppress_current_call_prompt(
                        session.display_title
                        if session and session.display_title != self.monitor.default_call_title
                        else None
                    )
                    self._discard_requested = True
                    self.monitor.is_in_meeting = False
                    self._finish_recording()
                    continue
                elif choice == "switch_video":
                    self._switch_to_video_mode()

                self._apply_video_switch()

                if (not self._manual_stop_event.is_set()
                        and mode == "video" and self.recorder.is_video_recording is True
                        and self.recorder.is_screen_recording is not True
                        and self.config.shared_window_behavior == "prompt"
                        and len(self.recorder.list_webex_windows()) > 1):
                    self.request_window_selection()

                if not self._manual_stop_event.is_set():
                    self.ui.show_notification(
                        "Webex OBS Companion",
                        f"Recording active ({'Video' if self.recorder.is_video_recording is True else 'Audio'}). "
                        f"{display_hotkey(self.config.hotkey_video)} Video, "
                        f"{display_hotkey(self.config.hotkey_video_source)} Source, "
                        f"{display_hotkey(self.config.hotkey_stop_transcribe)} Transcribe, "
                        "Menu-bar icon for all controls."
                    )

                # Wait for meeting process termination OR manual stop hotkey
                while self.monitor.is_in_meeting and not self._shutdown_event.is_set():
                    self._pulse()
                    if self._manual_stop_event.is_set():
                        break
                    self._shutdown_event.wait(self.config.poll_interval)
                    if self._manual_stop_event.is_set():
                        break
                    if not manual and self.monitor.has_call_ended():
                        self.monitor.is_in_meeting = False
                        break
                    self._apply_video_switch()
                    if self._manual_stop_event.is_set():
                        break
                    if not self.recorder.ensure_recording():
                        logger.error("OBS recovery failed; another recovery attempt will be made on the next poll.")
                    self._update_video_window()

                if self._shutdown_event.is_set():
                    break
                if self._discard_requested:
                    if self.monitor.is_call_active():
                        self.monitor.suppress_current_call_prompt()
                elif (not manual and self._manual_stop_event.is_set()) or (manual and self.monitor.is_call_active()):
                    self.monitor.suppress_current_call_prompt()
                self.monitor.is_in_meeting = False
                self._finish_recording()

        except KeyboardInterrupt:
            self.request_shutdown()


def main(*, detection_diagnostics: bool = False):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    daemon = WebexOBSDaemon(detection_diagnostics=detection_diagnostics)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: daemon.request_shutdown())
    if sys.platform == "darwin":
        from .macos_ui import MacOSUI

        ui = MacOSUI(daemon)
        daemon.ui = ui
        try:
            ui.run()
        finally:
            timeout = 0.0 if ui._shutdown_started_at is not None else 10.0
            if not daemon.shutdown(timeout=timeout):
                daemon.failure = "Shutdown timed out; check OBS."
    else:
        daemon.start_background()
        while not daemon._stopped_event.wait(0.2):
            if daemon._shutdown_event.is_set():
                if not daemon.shutdown():
                    daemon.failure = "Shutdown timed out; capture may still be active. Check OBS."
                    logger.error(daemon.failure)
                break
    if daemon.failure:
        raise SystemExit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--detection-diagnostics", action="store_true")
    main(detection_diagnostics=parser.parse_args().detection_diagnostics)
