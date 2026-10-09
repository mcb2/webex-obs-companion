from webex_obs.control_dialog import manual_consent_dialog, recording_menu_choices, startup_dialog


def test_menu_only_offers_actions_available_in_each_state():
    def actions(*args, **kwargs):
        return [choice for _, choice in recording_menu_choices(*args, **kwargs)]
    assert actions(False) == ["start_audio", "start_video"]
    assert actions(False, True) == ["start_audio", "start_video"]  # stale video scene
    assert actions(True) == ["switch_video", "stop_transcribe", "stop_discard"]
    assert actions(True, True) == ["select_window", "stop_transcribe", "stop_discard"]
    assert actions(True, True, source_picker_open=True) == ["stop_transcribe", "stop_discard"]
    assert actions(False, transitioning=True) == []
    assert actions(True, transitioning=True) == []


def test_manual_consent_defaults_to_ok_with_ten_second_notice():
    dialog = manual_consent_dialog("Architecture Review")
    assert dialog.choices == (("OK", "ok"), ("Cancel & Discard", "cancel"))
    assert dialog.default_choice == "ok"
    assert "Architecture Review" in dialog.detail
    assert "Two-party / all-party consent" in dialog.detail
    assert "Obtain permission from all participants" in dialog.detail
    assert "10 seconds" in dialog.detail




def test_startup_prompt_keeps_consent_and_audio_timeout_choice():
    dialog = startup_dialog("Architecture Review")
    assert [choice for _, choice in dialog.choices] == ["keep_audio", "switch_video", "cancel"]
    assert "Architecture Review" in dialog.detail
    assert "Obtain permission from all participants" in dialog.detail
    assert "15 seconds" in dialog.detail
