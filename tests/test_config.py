import pytest
from pydantic import ValidationError

from webex_obs.config import Settings


def test_hotkeys_have_backward_compatible_defaults():
    settings = Settings(_env_file=None)

    assert settings.webex_delivery_enabled is True
    assert settings.call_end_grace_seconds == 15.0
    assert settings.hotkey_video == "<cmd>+<shift>+v"
    assert settings.hotkey_menu == "<cmd>+<shift>+r"
    assert settings.hotkey_stop_transcribe == "<cmd>+<shift>+s"


def test_webex_delivery_can_be_disabled_in_env():
    assert Settings(_env_file=None, WEBEX_DELIVERY_ENABLED="false").webex_delivery_enabled is False


def test_new_obs_option_accepts_legacy_value_and_prefers_new_value():
    assert Settings(_env_file=None, RELAUNCH_OBS_PER_CALL="false").exit_obs_on_recording_stop is False
    assert Settings(_env_file=None, RELAUNCH_OBS_PER_CALL="false",
                    EXIT_OBS_ON_RECORDING_STOP="true").exit_obs_on_recording_stop is True


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
