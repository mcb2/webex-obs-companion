"""Recorder contract used by the meeting lifecycle.

Future native backends should return completed media paths from stop_recording,
including all segments after a recovery or an audio/video mode change.
"""

from typing import Literal, Protocol

from .config import Config
from .obs_controller import OBSController


RecordingMode = Literal["audio", "video"]


class RecordingBackend(Protocol):
    @property
    def is_recording(self) -> bool: ...

    def connect(self) -> bool: ...
    def disconnect(self) -> None: ...
    def start_recording(self, mode: RecordingMode = "audio", relaunch: bool = False) -> bool: ...
    def ensure_recording(self) -> bool: ...
    def switch_to_video_mode(self) -> None: ...
    def stop_recording(self) -> list[str]: ...
    def configure(self, config: Config) -> None: ...


class OBSRecordingBackend:
    """Translate generic recording modes into the existing OBS scene names."""

    def __init__(self, controller: OBSController):
        self.controller = controller
        self._pending_connection: tuple[str, int, str] | None = None

    def configure(self, config: Config) -> None:
        """Keep the live OBS socket until the current recording has stopped."""
        self.controller.relaunch_per_call = config.relaunch_obs_per_call
        connection = (config.obs_address, config.obs_port, config.obs_password)
        if self.controller.is_recording:
            self._pending_connection = connection
        else:
            self._pending_connection = connection
            self._apply_connection()

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

    def connect(self) -> bool:
        return self.controller.connect()

    def disconnect(self) -> None:
        self.controller.disconnect()

    def start_recording(self, mode: RecordingMode = "audio", relaunch: bool = False) -> bool:
        if mode not in ("audio", "video"):
            raise ValueError(f"Unknown recording mode: {mode}")
        return self.controller.start_recording(
            scene_name="Webex-Video" if mode == "video" else "Webex-Audio",
            relaunch=relaunch,
        )

    def ensure_recording(self) -> bool:
        return self.controller.ensure_recording()

    def switch_to_video_mode(self) -> None:
        self.controller.switch_to_video_mode()

    def stop_recording(self) -> list[str]:
        files = self.controller.stop_recording()
        self._apply_connection()
        return files


def create_recording_backend(config: Config) -> RecordingBackend:
    """The sole backend selection point; OBS is the only supported implementation."""
    return OBSRecordingBackend(OBSController(
        address=config.obs_address,
        port=config.obs_port,
        password=config.obs_password,
        relaunch_per_call=config.relaunch_obs_per_call,
    ))
