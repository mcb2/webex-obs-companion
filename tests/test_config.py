import pytest
from pydantic import ValidationError

from webex_obs.config import Settings


def test_four_action_hotkeys_have_defaults():
    settings = Settings(_env_file=None)

    assert settings.webex_delivery_enabled is True
    assert settings.call_end_grace_seconds == 15.0
    assert settings.hotkey_audio == "<cmd>+<shift>+a"
    assert settings.hotkey_video == "<cmd>+<shift>+v"
    assert settings.hotkey_video_source == "<cmd>+<shift>+r"
    assert settings.hotkey_stop_transcribe == "<cmd>+<shift>+s"


def test_webex_delivery_can_be_disabled_in_env():
    assert Settings(_env_file=None, WEBEX_DELIVERY_ENABLED="false").webex_delivery_enabled is False


def test_new_obs_option_accepts_legacy_value_and_prefers_new_value():
    assert Settings(_env_file=None, RELAUNCH_OBS_PER_CALL="false").exit_obs_on_recording_stop is False
    assert Settings(_env_file=None, RELAUNCH_OBS_PER_CALL="false",
                    EXIT_OBS_ON_RECORDING_STOP="true").exit_obs_on_recording_stop is True


def test_shared_window_behavior_is_validated():
    assert Settings(_env_file=None).shared_window_behavior == "prompt"
    assert Settings(_env_file=None, SHARED_WINDOW_BEHAVIOR="always_switch").shared_window_behavior == "always_switch"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SHARED_WINDOW_BEHAVIOR="unexpected")


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


def test_source_shortcut_reads_legacy_menu_value_but_prefers_new_key():
    assert Settings(_env_file=None, HOTKEY_MENU="<ctrl>+r").hotkey_video_source == "<ctrl>+r"
    assert Settings(_env_file=None, HOTKEY_MENU="<ctrl>+r", HOTKEY_VIDEO_SOURCE="<ctrl>+w").hotkey_video_source == "<ctrl>+w"


@pytest.mark.parametrize("first,second", [
    ("HOTKEY_AUDIO", "HOTKEY_VIDEO"),
    ("HOTKEY_AUDIO", "HOTKEY_VIDEO_SOURCE"),
    ("HOTKEY_AUDIO", "HOTKEY_STOP_TRANSCRIBE"),
    ("HOTKEY_VIDEO", "HOTKEY_VIDEO_SOURCE"),
    ("HOTKEY_VIDEO", "HOTKEY_STOP_TRANSCRIBE"),
    ("HOTKEY_VIDEO_SOURCE", "HOTKEY_STOP_TRANSCRIBE"),
])
def test_all_four_shortcuts_must_be_distinct(first, second):
    with pytest.raises(ValidationError, match="must be different"):
        Settings(_env_file=None, **{first: "<ctrl>+x", second: "<ctrl>+x"})
