from webex_obs.control_dialog import control_dialog, startup_dialog


def test_idle_choices_and_consent_notice():
    dialog = control_dialog(False, "Architecture Review")
    assert [choice for _, choice in dialog.choices] == ["start_audio", "start_video", "close"]
    assert "Architecture Review" in dialog.detail
    assert "Obtain permission from all participants" in dialog.detail


def test_active_choices_offer_confirmed_discard_but_not_new_recording():
    dialog = control_dialog(True, "Architecture Review")
    assert [choice for _, choice in dialog.choices] == [
        "switch_video", "stop_transcribe", "stop_discard", "close"
    ]
    assert not dialog.disabled_choices


def test_video_recording_disables_switch_to_video():
    dialog = control_dialog(True, "Architecture Review", is_video_recording=True)
    assert dialog.disabled_choices == frozenset({"switch_video"})


def test_startup_prompt_keeps_consent_and_audio_timeout_choice():
    dialog = startup_dialog("Architecture Review")
    assert [choice for _, choice in dialog.choices] == ["keep_audio", "switch_video", "cancel"]
    assert "Architecture Review" in dialog.detail
    assert "Obtain permission from all participants" in dialog.detail
    assert "15 seconds" in dialog.detail
