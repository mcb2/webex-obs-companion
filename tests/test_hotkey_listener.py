import sys
import types
from unittest.mock import Mock, patch

import pytest


class _HotKey:
    @staticmethod
    def parse(value):
        if "+" not in value:
            raise ValueError("expected a modifier and key")
        return value


class _GlobalHotKeys:
    def __init__(self, hotkeys):
        self.hotkeys = hotkeys
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


fake_pynput = types.ModuleType("pynput")
fake_pynput.keyboard = types.SimpleNamespace(
    HotKey=_HotKey,
    GlobalHotKeys=_GlobalHotKeys,
)
sys.modules.setdefault("pynput", fake_pynput)

from webex_obs.hotkey_listener import (  # noqa: E402
    HotkeyListener,
    display_hotkey,
    validate_hotkeys,
)


def test_listener_registers_configured_hotkeys():
    listener = HotkeyListener(
        on_audio_start=Mock(), on_video_switch=Mock(), on_video_source=Mock(), on_stop_transcribe=Mock(),
        audio_hotkey="<ctrl>+<alt>+a",
        video_hotkey="<ctrl>+<alt>+v",
        video_source_hotkey="<ctrl>+<alt>+r",
        stop_transcribe_hotkey="<ctrl>+<alt>+t",
    )

    with patch("webex_obs.hotkey_listener.keyboard.GlobalHotKeys", _GlobalHotKeys):
        listener.start()

    assert set(listener.listener.hotkeys) == {
        "<ctrl>+<alt>+a",
        "<ctrl>+<alt>+v",
        "<ctrl>+<alt>+r",
        "<ctrl>+<alt>+t",
    }
    assert listener.listener.started
    for callback in listener.listener.hotkeys.values():
        callback()
    listener.on_audio_start.assert_called_once_with()
    listener.on_video_switch.assert_called_once_with()
    listener.on_video_source.assert_called_once_with()
    listener.on_stop_transcribe.assert_called_once_with()
    listener.stop()
    assert not listener.listener.started


def test_duplicate_hotkeys_are_rejected_before_registration():
    with pytest.raises(ValueError, match="different key combination"):
        validate_hotkeys("<cmd>+x", "<cmd>+x")


def test_invalid_hotkey_is_rejected():
    with pytest.raises(ValueError, match="Invalid global hotkey"):
        validate_hotkeys("x")


def test_display_hotkey_formats_pynput_syntax():
    assert display_hotkey("<cmd>+<shift>+v") == "Cmd+Shift+V"
