"""Recorder contract used by the meeting lifecycle.

Future native backends should return completed media paths from stop_recording,
including all known segments after a recovery. Return no completed paths until
stop_verified is True. recording_status is True (active), False (stopped), or
None (unknown); is_recording also stays True for unresolved owned sessions.
"""

import threading
import time
from typing import Callable, Literal, Protocol

from .config import Config
from .obs_controller import CaptureDisplay, OBSController, WebexWindow
from .obs_maintenance import OBSMaintenance


RecordingMode = Literal["audio", "video"]


class RecordingBackend(Protocol):
    @property
    def is_recording(self) -> bool: ...

    @property
    def recording_status(self) -> bool | None: ...
    @property
    def stop_verified(self) -> bool: ...

    def connect(self) -> bool: ...
    def initialize(self) -> bool: ...
    def prepare_recording(self) -> bool: ...
    def maintain_idle(self, start_pending: Callable[[], bool]) -> bool: ...
    def mark_call_evidence(self) -> None: ...
    def disconnect(self) -> None: ...
    def start_recording(self, mode: RecordingMode = "audio") -> bool: ...
    def retry_start_recording(self, mode: RecordingMode = "audio") -> bool: ...
    def ensure_recording(self) -> bool: ...
    def switch_to_video_mode(self) -> None: ...
    def list_webex_windows(self) -> list[WebexWindow]: ...
    def select_webex_window(self, window_id: int) -> bool: ...
    def list_capture_displays(self) -> list[CaptureDisplay]: ...
    def select_capture_display(self, display_uuid: str) -> bool: ...
    def poll_webex_window_change(self) -> list[WebexWindow]: ...
    @property
    def is_video_recording(self) -> bool: ...
    @property
    def is_screen_recording(self) -> bool: ...
    @property
    def selected_display_uuid(self) -> str | None: ...
    def stop_recording(self) -> list[str]: ...
    def configure(self, config: Config) -> None: ...


class OBSRecordingBackend:
    """Translate generic recording modes into the existing OBS scene names."""

    def __init__(self, controller: OBSController):
        self.controller = controller
        self._pending_connection: tuple[str, int, str] | None = None
        self._lifecycle_lock = threading.RLock()
        self._maintenance = OBSMaintenance()
        self.idle_restart_minutes = 60

    def configure(self, config: Config) -> None:
        """Keep the live OBS socket until the current recording has stopped."""
        with self._lifecycle_lock:
            self._configure(config)

    def _configure(self, config: Config) -> None:
        self.controller.exit_on_stop = config.exit_obs_on_recording_stop
        self.idle_restart_minutes = config.obs_idle_restart_minutes
        connection = (config.obs_address, config.obs_port, config.obs_password)
        # Settings runs on the UI thread. Verification and reconnection belong
        # to the lifecycle worker, even when the last cached state was idle.
        self._pending_connection = connection

    def _apply_connection(self) -> None:
        if self._pending_connection is None:
            return
        address, port, password = self._pending_connection
        self._pending_connection = None
        if (self.controller.address, self.controller.port, self.controller.password) != (address, port, password):
            self.controller.disconnect()
            self.controller.address, self.controller.port, self.controller.password = address, port, password

    @property
    def is_recording(self) -> bool:
        return self.controller.is_recording

    @property
    def recording_status(self) -> bool | None:
        return self.controller.recording_status

    @property
    def stop_verified(self) -> bool:
        return self.controller.stop_verified

    @property
    def is_video_recording(self) -> bool:
        return self.controller.is_recording and self.controller.current_scene == "Webex-Video"

    @property
    def is_screen_recording(self) -> bool:
        return self.is_video_recording and self.controller.is_screen_recording

    @property
    def selected_display_uuid(self) -> str | None:
        return self.controller.selected_display_uuid

    def connect(self) -> bool:
        return self.controller.connect()

    def initialize(self) -> bool:
        with self._lifecycle_lock:
            result = self.controller.initialize()
            self._maintenance.last_refresh = time.monotonic()
            return result

    def prepare_recording(self) -> bool:
        """Warm OBS before title/window validation; never start a recording here."""
        self.mark_call_evidence()
        with self._lifecycle_lock:
            was_running = self.controller.obs_is_running()
            result = self.controller.check_connection()
            if (result and self._pending_connection is not None
                    and not self.controller.is_recording
                    and self.controller._recording_active_status() is False):
                self._apply_connection()
                result = self.controller.check_connection()
            if result and not was_running:
                self._maintenance.last_refresh = time.monotonic()
            return result

    def mark_call_evidence(self) -> None:
        self._maintenance.mark_busy()

    def maintain_idle(self, start_pending: Callable[[], bool]) -> bool:
        with self._lifecycle_lock:
            return self._maintenance.maintain(
                self.controller, self.idle_restart_minutes, start_pending,
            )

    def disconnect(self) -> None:
        with self._lifecycle_lock:
            self.controller.disconnect()

    def start_recording(self, mode: RecordingMode = "audio") -> bool:
        if mode not in ("audio", "video"):
            raise ValueError(f"Unknown recording mode: {mode}")
        with self._lifecycle_lock:
            self.mark_call_evidence()
            if (self._pending_connection is not None and not self.controller.is_recording
                    and self.controller._recording_active_status() is False):
                self._apply_connection()
            return self.controller.start_recording(
                scene_name="Webex-Video" if mode == "video" else "Webex-Audio",
            )

    def retry_start_recording(self, mode: RecordingMode = "audio") -> bool:
        """Retry an initial start while reusing a running OBS instance."""
        return self.start_recording(mode)

    def ensure_recording(self) -> bool:
        with self._lifecycle_lock:
            return self.controller.ensure_recording()

    def switch_to_video_mode(self) -> None:
        with self._lifecycle_lock:
            self.controller.switch_to_video_mode()

    def list_webex_windows(self) -> list[WebexWindow]:
        return self.controller.list_webex_windows()

    def select_webex_window(self, window_id: int) -> bool:
        return self.controller.bind_webex_video_window(window_id)

    def list_capture_displays(self) -> list[CaptureDisplay]:
        return self.controller.list_capture_displays()

    def select_capture_display(self, display_uuid: str) -> bool:
        return self.controller.select_capture_display(display_uuid)

    def poll_webex_window_change(self) -> list[WebexWindow]:
        return self.controller.poll_webex_window_change()

    def stop_recording(self) -> list[str]:
        with self._lifecycle_lock:
            files = self.controller.stop_recording()
            self.mark_call_evidence()
            if self.controller.stop_verified is True:
                self._apply_connection()
            return files


def create_recording_backend(config: Config) -> RecordingBackend:
    """The sole backend selection point; OBS is the only supported implementation."""
    backend = OBSRecordingBackend(OBSController(
        address=config.obs_address,
        port=config.obs_port,
        password=config.obs_password,
        exit_on_stop=config.exit_obs_on_recording_stop,
    ))
    backend.idle_restart_minutes = config.obs_idle_restart_minutes
    return backend
