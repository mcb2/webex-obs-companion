from webex_obs.control_dialog import control_dialog


def test_idle_choices_and_consent_notice():
    dialog = control_dialog(False, "Architecture Review")
    assert [choice for _, choice in dialog.choices] == ["start_audio", "start_video", "close"]
    assert "Architecture Review" in dialog.detail
    assert "Obtain permission from all participants" in dialog.detail


def test_active_choices_do_not_offer_discard_or_new_recording():
    dialog = control_dialog(True, "Architecture Review")
    assert [choice for _, choice in dialog.choices] == ["switch_video", "stop_transcribe", "close"]
