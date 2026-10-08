from unittest.mock import Mock, call

import pytest

pytest.importorskip("AppKit")

from webex_obs.macos_ui import MacOSUI


def test_status_icon_tracks_audio_video_and_stop_without_repeated_updates():
    ui = MacOSUI(Mock())
    ui.item = Mock()
    ui._status_images = {state: object() for state in ("idle", "audio", "video")}
    button = ui.item.button.return_value

    ui._update_status_icon(False, False)
    ui._update_status_icon(True, False)
    ui._update_status_icon(True, True)
    ui._update_status_icon(True, True)
    # The previous video scene must not leave a badge when recording stops.
    ui._update_status_icon(False, True)

    assert button.setImage_.call_args_list == [
        call(ui._status_images[state]) for state in ("idle", "audio", "video", "idle")
    ]
    assert button.setAccessibilityLabel_.call_args_list == [
        call("Meeting recorder: not recording"),
        call("Meeting recorder: recording audio"),
        call("Meeting recorder: recording video"),
        call("Meeting recorder: not recording"),
    ]
