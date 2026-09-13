from __future__ import annotations

import ctypes
import struct
import sys
from dataclasses import dataclass
from ctypes import POINTER, Structure, byref, c_int32, c_uint32, c_void_p, sizeof
from typing import Any

CORE_AUDIO_PATH = (
    "/System/Library/Frameworks/CoreAudio.framework/Versions/A/CoreAudio"
)


def _fourcc(value: str) -> int:
    return struct.unpack(">I", value.encode("ascii"))[0]


K_AUDIO_OBJECT_SYSTEM_OBJECT = 1
K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL = _fourcc("glob")
K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN = 0
K_AUDIO_HARDWARE_PROPERTY_PROCESS_OBJECT_LIST = _fourcc("prs#")
K_AUDIO_HARDWARE_PROPERTY_TRANSLATE_PID = _fourcc("id2p")
K_AUDIO_PROCESS_PROPERTY_RUNNING_INPUT = _fourcc("piri")
K_AUDIO_PROCESS_PROPERTY_RUNNING_OUTPUT = _fourcc("piro")


class AudioObjectPropertyAddress(Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


@dataclass(frozen=True)
class AudioActivity:
    available: bool
    input_pids: tuple[int, ...] = ()
    output_pids: tuple[int, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.input_pids or self.output_pids)


class CoreAudioProcessMonitor:
    """Read transport-independent audio I/O state for specific macOS PIDs."""

    def __init__(self, core_audio: Any | None = None):
        self._core_audio = core_audio or self._load_core_audio()
        self._available = self._configure_library()

    @staticmethod
    def _load_core_audio():
        if sys.platform != "darwin":
            return None
        try:
            return ctypes.CDLL(CORE_AUDIO_PATH)
        except OSError:
            return None

    @staticmethod
    def _address(selector: int) -> AudioObjectPropertyAddress:
        return AudioObjectPropertyAddress(
            selector,
            K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
            K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
        )

    def _configure_library(self) -> bool:
        if self._core_audio is None:
            return False
        try:
            self._core_audio.AudioObjectHasProperty.argtypes = [
                c_uint32,
                POINTER(AudioObjectPropertyAddress),
            ]
            self._core_audio.AudioObjectHasProperty.restype = ctypes.c_bool
            self._core_audio.AudioObjectGetPropertyData.argtypes = [
                c_uint32,
                POINTER(AudioObjectPropertyAddress),
                c_uint32,
                c_void_p,
                POINTER(c_uint32),
                c_void_p,
            ]
            self._core_audio.AudioObjectGetPropertyData.restype = c_int32
            address = self._address(K_AUDIO_HARDWARE_PROPERTY_PROCESS_OBJECT_LIST)
            return bool(
                self._core_audio.AudioObjectHasProperty(
                    K_AUDIO_OBJECT_SYSTEM_OBJECT, byref(address)
                )
            )
        except (AttributeError, TypeError):
            return False

    @property
    def available(self) -> bool:
        return self._available

    def _translate_pid(self, pid: int) -> int | None:
        address = self._address(K_AUDIO_HARDWARE_PROPERTY_TRANSLATE_PID)
        qualifier = c_int32(pid)
        output = c_uint32(0)
        size = c_uint32(sizeof(output))
        status = self._core_audio.AudioObjectGetPropertyData(
            K_AUDIO_OBJECT_SYSTEM_OBJECT,
            byref(address),
            sizeof(qualifier),
            byref(qualifier),
            byref(size),
            byref(output),
        )
        return int(output.value) if status == 0 and output.value else None

    def _read_boolean(self, object_id: int, selector: int) -> bool:
        address = self._address(selector)
        output = c_uint32(0)
        size = c_uint32(sizeof(output))
        status = self._core_audio.AudioObjectGetPropertyData(
            object_id,
            byref(address),
            0,
            None,
            byref(size),
            byref(output),
        )
        return status == 0 and bool(output.value)

    def activity_for_pids(self, pids: list[int] | tuple[int, ...]) -> AudioActivity:
        if not self.available:
            return AudioActivity(available=False)

        input_pids = []
        output_pids = []
        for pid in sorted(set(pids)):
            try:
                object_id = self._translate_pid(pid)
                if object_id is None:
                    continue
                if self._read_boolean(
                    object_id, K_AUDIO_PROCESS_PROPERTY_RUNNING_INPUT
                ):
                    input_pids.append(pid)
                if self._read_boolean(
                    object_id, K_AUDIO_PROCESS_PROPERTY_RUNNING_OUTPUT
                ):
                    output_pids.append(pid)
            except (OSError, TypeError, ValueError):
                continue

        return AudioActivity(
            available=True,
            input_pids=tuple(input_pids),
            output_pids=tuple(output_pids),
        )
