import threading
from unittest.mock import Mock, call, patch

import pytest

pytest.importorskip("AppKit")

from webex_obs.macos_ui import MacOSUI, _MenuTarget


def test_settings_order_ends_with_delivery_and_exposes_four_action_shortcuts():
    sections = MacOSUI._settings_sections()
    assert [heading for heading, _ in sections] == [
        "OBS recording", "Transcription", "Files and detection", "Keyboard shortcuts", "Webex delivery"
    ]
    assert [key for _, key, _ in dict(sections)["Keyboard shortcuts"]] == [
        "hotkey_audio", "hotkey_video", "hotkey_video_source", "hotkey_stop_transcribe"
    ]


def menu_ui(recording=False, video=False):
    daemon = Mock()
    daemon.recorder.is_recording = recording
    daemon.recorder.is_video_recording = video
    daemon._manual_start_event = threading.Event()
    daemon._manual_stop_event = threading.Event()
    daemon._window_prompt_pending = threading.Event()
    daemon._active_session = None
    daemon._stop_pending = False
    daemon.config.retention_days = 14
    daemon._delete_after_transcription = False
    daemon._window_session_generation = 7
    ui = MacOSUI(daemon)
    ui.recording_items = {choice: Mock() for choice in (
        "start_audio", "start_video", "switch_video", "select_window", "stop_transcribe", "stop_discard",
        "delete_after_transcription",
    )}
    ui.quit_item = Mock()
    return ui


@pytest.mark.parametrize("recording,video,visible", [
    (False, False, {"start_audio", "start_video"}),
    (True, False, {"switch_video", "stop_transcribe", "stop_discard", "delete_after_transcription"}),
    (True, True, {"select_window", "stop_transcribe", "stop_discard", "delete_after_transcription"}),
])
def test_native_menu_hides_unavailable_actions_instead_of_disabling(recording, video, visible):
    ui = menu_ui(recording, video)
    ui._update_recording_menu()
    for choice, item in ui.recording_items.items():
        item.setHidden_.assert_called_once_with(choice not in visible)
        item.setEnabled_.assert_not_called()
    ui.quit_item.setHidden_.assert_called_once_with(recording)


def test_pending_start_hides_start_actions_and_quit():
    ui = menu_ui()
    ui.daemon._manual_start_event.set()
    ui._update_recording_menu()
    for item in ui.recording_items.values():
        item.setHidden_.assert_called_once_with(True)
    ui.quit_item.setHidden_.assert_called_once_with(True)


def test_menu_dispatch_rechecks_state_and_routes_discard_through_confirmation():
    ui = menu_ui(True, True)
    ui.perform_recording_action("start_audio")
    ui.perform_recording_action("switch_video")
    ui.daemon._handle_control_choice.assert_not_called()
    ui.perform_recording_action("stop_discard")
    ui.daemon.request_discard_confirmation.assert_called_once_with()
    ui.perform_recording_action("select_window")
    ui.daemon._handle_control_choice.assert_called_once_with("select_window")


def test_manual_consent_uses_ten_second_modal_and_ok_on_timeout():
    ui = MacOSUI(Mock())
    appkit, foundation = Mock(), Mock()
    appkit.NSApp.runModalForWindow_.return_value = 1
    ui.target = Mock()
    with patch("webex_obs.macos_ui.AppKit", appkit), \
         patch("webex_obs.macos_ui.Foundation", foundation):
        assert ui.show_manual_consent_prompt("Manual meeting") == "ok"
    timer_call = foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.call_args
    assert timer_call.args[0] == 10
    assert timer_call.args[3] is appkit.NSWindow.alloc.return_value.initWithContentRect_styleMask_backing_defer_.return_value
    foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.return_value.invalidate.assert_called_once()
    foundation.NSRunLoop.currentRunLoop.return_value.addTimer_forMode_.assert_called_once_with(
        foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.return_value,
        appkit.NSModalPanelRunLoopMode,
    )
    appkit.NSButton.alloc.return_value.initWithFrame_.return_value.setKeyEquivalent_.assert_any_call("\r")
    assert ui._control_modal_active is False


def test_consent_timeout_only_closes_its_own_modal_window():
    target = _MenuTarget.alloc().init()
    appkit, timer = Mock(), Mock()
    with patch("webex_obs.macos_ui.AppKit", appkit):
        target.controlTimeout_(timer)
        appkit.NSApp.stopModalWithCode_.assert_not_called()
        appkit.NSApp.modalWindow.return_value = timer.userInfo.return_value
        target.controlTimeout_(timer)
        appkit.NSApp.stopModalWithCode_.assert_called_once_with(1)


def test_discard_alert_sets_no_as_default_and_requires_yes():
    for response, confirmed in ((1000, False), (1001, True)):
        appkit = Mock(NSAlertSecondButtonReturn=1001)
        alert = appkit.NSAlert.alloc.return_value.init.return_value
        alert.runModal.return_value = response
        with patch("webex_obs.macos_ui.AppKit", appkit):
            assert MacOSUI._confirm_discard() is confirmed
        assert alert.addButtonWithTitle_.call_args_list == [
            call("No, keep recording"), call("Yes, stop and delete")
        ]
        alert.window.return_value.setDefaultButtonCell_.assert_called_once_with(
            alert.addButtonWithTitle_.return_value.cell.return_value
        )


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


def shutdown_ui():
    ui = MacOSUI(Mock())
    ui.daemon._shutdown_event = threading.Event()
    ui.daemon._stopped_event = threading.Event()
    ui.daemon.failure = None
    return ui


def test_quit_waits_for_lifecycle_before_unloading_service():
    ui = shutdown_ui()
    ui.daemon.recorder.is_recording = False
    target = _MenuTarget.alloc().init()
    target.configure(ui)
    with patch("webex_obs.macos_ui.stop_launch_agent") as unload, \
         patch("webex_obs.macos_ui.AppKit") as appkit, \
         patch("webex_obs.macos_ui.threading.Thread") as thread:
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        target.quit_(None)
        ui.daemon.request_shutdown.assert_called_once()
        ui._tick_shutdown()
        unload.assert_not_called()
        appkit.NSApp.stop_.assert_not_called()
        ui.daemon._stopped_event.set()
        ui._tick_shutdown()
        unload.assert_called_once()
        appkit.NSApp.stop_.assert_called_once_with(None)


def test_shutdown_deadline_exits_with_visible_capture_warning():
    ui = shutdown_ui()
    ui._shutdown_started_at = 0
    with patch("webex_obs.macos_ui.time.monotonic", return_value=11), \
         patch("webex_obs.macos_ui.AppKit") as appkit:
        ui._tick_shutdown()
    assert "capture may still be active" in ui.daemon.failure
    appkit.NSApp.stop_.assert_called_once_with(None)
    ui.daemon.recorder.stop_recording.assert_not_called()


def test_application_termination_requests_coordinated_shutdown():
    ui = shutdown_ui()
    target = _MenuTarget.alloc().init()
    target.configure(ui)
    with patch("webex_obs.macos_ui.AppKit") as appkit:
        assert target.applicationShouldTerminate_(None) is appkit.NSTerminateCancel
    ui.daemon.request_shutdown.assert_called_once()


def test_ui_bridge_rejects_new_dialogs_during_shutdown():
    from webex_obs.runtime_state import ApplicationStopping
    ui = shutdown_ui()
    ui.daemon._shutdown_event.set()
    func = Mock()
    # Exercise the background bridge rather than the direct main-thread path.
    with patch("webex_obs.macos_ui.threading.current_thread", return_value=object()):
        with pytest.raises(ApplicationStopping):
            ui._on_main(func)
    func.assert_not_called()
    assert ui.requests.empty()


@pytest.mark.parametrize("failure", [None, "ordinary", "rollback"])
def test_settings_worker_keeps_main_thread_free_and_reports_completion_on_main(failure):
    from webex_obs.settings_store import SettingsRollbackError
    ui = MacOSUI(Mock())
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    main = threading.current_thread()
    def save(values, path):
        assert threading.current_thread() is not main
        entered.set()
        try:
            assert release.wait(3)
            if failure == "ordinary":
                raise ValueError("synthetic secret must not reach UI")
            if failure == "rollback":
                raise SettingsRollbackError("synthetic rollback error")
        finally:
            finished.set()
    ui.daemon.save_settings.side_effect = save
    ui._alert = Mock(side_effect=lambda *a: threading.current_thread() is main)
    try:
        ui._save_settings_async({"obs_ws_port": "4456"})
        assert entered.wait(3)
        assert ui._settings_saving
        assert ui.requests.empty()
        ui._save_settings_async({"obs_ws_port": "4457"})
        assert ui.daemon.save_settings.call_count == 1
        ui._alert.assert_not_called()
    finally:
        release.set()
    assert finished.wait(3)
    complete, result, done = ui.requests.get(timeout=3)
    complete()
    assert not ui._settings_saving
    if failure:
        ui._alert.assert_called_once()
        message = ui._alert.call_args.args[2]
        assert "synthetic secret" not in message
        assert ("restart" in message) is (failure == "rollback")
    else:
        ui._alert.assert_not_called()


@pytest.fixture
def settings_ui():
    from webex_obs.config import Config
    ui = MacOSUI(Mock())
    ui.daemon.config = Config(_env_file=None, retention_days=0,
                              call_end_grace_seconds=0, obs_idle_restart_minutes=0)
    ui.target = Mock()
    appkit, foundation = Mock(), Mock()
    appkit.NSWindowStyleMaskTitled = 1
    appkit.NSWindowStyleMaskClosable = 2
    appkit.NSWindowStyleMaskMiniaturizable = 4
    def field():
        control = Mock()
        saved = {}
        control.setStringValue_.side_effect = lambda value: saved.update(text=value)
        control.stringValue.side_effect = lambda: saved["text"]
        control.setState_.side_effect = lambda value: saved.update(state=value)
        control.state.side_effect = lambda: saved["state"]
        control.selectItemAtIndex_.side_effect = lambda value: saved.update(index=value)
        control.indexOfSelectedItem.side_effect = lambda: saved["index"]
        return control
    for name in ("NSTextField", "NSSecureTextField", "NSButton"):
        getattr(appkit, name).alloc.return_value.initWithFrame_.side_effect = lambda *a: field()
    appkit.NSPopUpButton.alloc.return_value.initWithFrame_pullsDown_.side_effect = lambda *a: field()
    with patch("webex_obs.macos_ui.AppKit", appkit), \
         patch("webex_obs.macos_ui.Foundation", foundation):
        yield ui, appkit


def test_accepting_unchanged_settings_with_zero_values_does_not_save(settings_ui):
    ui, appkit = settings_ui
    with patch.object(ui, "_save_settings_async") as save:
        ui.show_settings()
        appkit.NSApp.runModalForWindow_.assert_not_called()
        assert ui._settings_window is not None
        ui.save_settings()
    save.assert_not_called()
    assert ui._settings_window is None


def test_settings_is_normal_nonmodal_window_and_reopening_preserves_edits(settings_ui):
    ui, appkit = settings_ui
    ui.show_settings()
    window = ui._settings_window
    inputs = ui._settings_inputs
    inputs["obs_ws_port"][0].setStringValue_("4456")
    window.isMiniaturized.return_value = True
    ui.show_settings()
    assert ui._settings_window is window
    assert ui._settings_inputs is inputs
    assert inputs["obs_ws_port"][0].stringValue() == "4456"
    appkit.NSWindow.alloc.assert_called_once()
    window.makeKeyAndOrderFront_.assert_has_calls([call(None), call(None)])
    window.deminiaturize_.assert_called_once_with(None)
    window.setLevel_.assert_called_once_with(appkit.NSNormalWindowLevel)
    appkit.NSApp.runModalForWindow_.assert_not_called()


def test_ok_saves_only_edits_once_and_does_not_stop_another_modal(settings_ui):
    ui, appkit = settings_ui
    target = _MenuTarget.alloc().init()
    target.configure(ui)
    ui.show_settings()
    ui._settings_inputs["obs_ws_port"][0].setStringValue_("4456")
    ui._settings_inputs["webex_delivery_enabled"][0].setState_(False)
    ui._settings_inputs["shared_window_behavior"][0].selectItemAtIndex_(1)
    with patch.object(ui, "_save_settings_async") as save:
        target.saveSettings_(None)
        target.saveSettings_(None)
    save.assert_called_once_with({"obs_ws_port": "4456", "webex_delivery_enabled": False,
                                  "shared_window_behavior": "always_switch"})
    assert ui._settings_window is None
    assert ui._settings_inputs == {}
    appkit.NSApp.stopModalWithCode_.assert_not_called()


@pytest.mark.parametrize("native_close", [False, True])
def test_cancel_and_native_close_discard_edits_and_allow_reopening(settings_ui, native_close):
    ui, appkit = settings_ui
    target = _MenuTarget.alloc().init()
    target.configure(ui)
    ui.show_settings()
    window = ui._settings_window
    ui._settings_inputs["obs_ws_port"][0].setStringValue_("4456")
    with patch.object(ui, "_save_settings_async") as save:
        if native_close:
            notification = Mock()
            notification.object.return_value = window
            target.windowWillClose_(notification)
        else:
            target.cancelSettings_(None)
            window.close.assert_called_once()
        save.assert_not_called()
    assert ui._settings_window is None
    assert ui._settings_panes == []
    assert ui._sidebar_source is None
    ui.show_settings()
    assert ui._settings_inputs["obs_ws_port"][0].stringValue() == "4455"
    appkit.NSApp.stopModalWithCode_.assert_not_called()


@pytest.mark.parametrize("recording,retention,selected,visible", [
    (False, 14, False, False), (True, 0, False, False),
    (True, 0, True, False), (True, 14, False, True), (True, 14, True, True),
])
def test_session_retention_menu_visibility_checkmark_and_dispatch(recording, retention, selected, visible):
    ui = menu_ui(recording)
    ui.daemon.config.retention_days = retention
    ui.daemon._delete_after_transcription = selected
    ui._update_recording_menu()
    item = ui.recording_items["delete_after_transcription"]
    item.setHidden_.assert_called_once_with(not visible)
    item.setState_.assert_called_once_with(selected)
    ui.perform_recording_action("delete_after_transcription", 7)
    assert ui.daemon.toggle_delete_after_transcription.call_count == int(visible)
    if visible:
        ui.daemon.toggle_delete_after_transcription.assert_called_once_with(7)


def test_retention_setup_note_is_present_in_gui(settings_ui):
    ui, appkit = settings_ui
    ui.show_settings()
    labels = [call.args[0] for call in appkit.NSTextField.labelWithString_.call_args_list]
    assert "0 = Do not retain recordings." in labels
    entries = dict(ui._settings_sections())["Files and detection"]
    assert entries[:3] == [
        ("Recordings folder", "recordings_dir", "text"),
        ("Recording retention (days)", "retention_days", "text"),
        ("Transcripts folder", "transcripts_dir", "text"),
    ]


def test_menu_action_captures_session_generation_before_background_dispatch():
    ui = menu_ui(True)
    target = _MenuTarget.alloc().init()
    target.configure(ui)
    sender = Mock()
    sender.representedObject.return_value = "delete_after_transcription"
    with patch("webex_obs.macos_ui.threading.Thread") as thread:
        target.recordingAction_(sender)
    assert thread.call_args.kwargs["args"] == ("delete_after_transcription", 7)
