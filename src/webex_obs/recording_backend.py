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


class OBSRecordingBackend:
    """Translate generic recording modes into the existing OBS scene names."""

    def __init__(self, controller: OBSController):
        self.controller = controller

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
        return self.controller.stop_recording()


def create_recording_backend(config: Config) -> RecordingBackend:
    """The sole backend selection point; OBS is the only supported implementation."""
    return OBSRecordingBackend(OBSController(
        address=config.obs_address,
        port=config.obs_port,
        password=config.obs_password,
        relaunch_per_call=config.relaunch_obs_per_call,
    ))
