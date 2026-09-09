import pytest
from pydantic import ValidationError

from webex_obs.config import Settings


def test_hotkeys_have_backward_compatible_defaults():
    settings = Settings(_env_file=None)

    assert settings.call_end_grace_seconds == 15.0
    assert settings.hotkey_video == "<cmd>+<shift>+v"
    assert settings.hotkey_menu == "<cmd>+<shift>+r"
    assert settings.hotkey_stop_transcribe == "<cmd>+<shift>+s"


def test_hotkeys_are_normalized():
    settings = Settings(
        _env_file=None,
        HOTKEY_VIDEO="  <CMD>+<SHIFT>+X  ",
        HOTKEY_MENU="<cmd>+<shift>+m",
        HOTKEY_STOP_TRANSCRIBE="<cmd>+<shift>+t",
    )

    assert settings.hotkey_video == "<cmd>+<shift>+x"


def test_duplicate_hotkeys_are_rejected():
    with pytest.raises(ValidationError, match="must be different"):
        Settings(
            _env_file=None,
            HOTKEY_VIDEO="<cmd>+<shift>+x",
            HOTKEY_MENU="<cmd>+<shift>+x",
        )
