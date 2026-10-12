import unittest
from unittest.mock import Mock, patch

import pytest
from obswebsocket import requests


from webex_obs.obs_controller import CaptureDisplay, OBSController, WebexWindow


def _Response(**data):
    response = requests.GetRecordStatus()
    response.input(data, True)
    return response


@pytest.fixture(autouse=True)
def isolated_obs_process(monkeypatch):
    monkeypatch.setattr(OBSController, "obs_is_running", lambda self: True)
    def unexpected_command(*args, **kwargs):
        pytest.fail("OBS tests must explicitly mock every process command")
    monkeypatch.setattr("webex_obs.obs_controller.subprocess.run", unexpected_command)


class OBSControllerTests(unittest.TestCase):
    def test_keep_open_startup_does_not_quit_new_obs(self):
        controller = OBSController(exit_on_stop=False)
        with patch.object(controller, "obs_is_running", return_value=False), \
             patch.object(controller, "connect", return_value=True), \
             patch.object(controller, "quit_obs") as quit_obs:
            self.assertTrue(controller.initialize())
        quit_obs.assert_not_called()

    def test_idle_quit_guard_can_cancel_before_sending_quit(self):
        controller = OBSController()
        with patch.object(controller, "obs_is_running", return_value=True), \
             patch("webex_obs.obs_controller.subprocess.run") as run:
            self.assertFalse(controller.quit_obs(idle_guard=lambda: False))
        run.assert_not_called()

    def test_maintenance_requires_all_outputs_explicitly_idle(self):
        for statuses, expected in (([False, False, False], True),
                                   ([True, False, False], False),
                                   ([False, True, False], False),
                                   ([False, False, True], False),
                                   ([False, None, False], False)):
            controller = OBSController()
            controller.ws = Mock()
            controller.ws.call.side_effect = [_Response(outputActive=s) for s in statuses]
            with patch.object(controller, "check_connection", return_value=True):
                self.assertEqual(controller.outputs_confirmed_idle(), expected)

    def test_maintenance_status_error_is_not_idle(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.ws.call.side_effect = RuntimeError("unavailable")
        with patch.object(controller, "check_connection", return_value=True):
            self.assertFalse(controller.outputs_confirmed_idle())

    def test_startup_never_quits_or_waits_on_status_with_either_stop_preference(self):
        for exit_on_stop in (False, True):
            for running in (False, True):
                controller = OBSController(exit_on_stop=exit_on_stop)
                with patch.object(controller, "obs_is_running", return_value=running), \
                     patch("webex_obs.obs_controller.subprocess.run") as run, \
                     patch("webex_obs.obs_controller.obsws") as websocket, \
                     patch.object(controller, "_recording_active_status") as status, \
                     patch.object(controller, "quit_obs") as quit_obs, \
                     patch("webex_obs.obs_controller.time.sleep") as sleep:
                    self.assertTrue(controller.initialize())
                self.assertEqual(run.call_count, int(not running))
                if not running:
                    run.assert_called_once_with(["open", "-g", "-a", "OBS"], check=True)
                websocket.return_value.connect.assert_called_once()
                status.assert_not_called()
                quit_obs.assert_not_called()
                sleep.assert_not_called()

    def test_startup_reports_connection_failure_without_quitting(self):
        controller = OBSController(exit_on_stop=True)
        with patch.object(controller, "connect", return_value=False), \
             patch.object(controller, "quit_obs") as quit_obs:
            self.assertFalse(controller.initialize())
        quit_obs.assert_not_called()

    def test_quit_retries_when_obs_ignores_first_request(self):
        controller = OBSController()
        running = [True] * 22 + [False]
        with patch.object(controller, "obs_is_running", side_effect=running), \
             patch.object(controller, "_recording_active_status", return_value=False), \
             patch.object(controller, "disconnect") as disconnect, \
             patch("webex_obs.obs_controller.subprocess.run") as run, \
             patch("webex_obs.obs_controller.time.sleep"):
            run.return_value.returncode = 0
            self.assertTrue(controller.quit_obs())
        self.assertEqual(run.call_count, 2)
        disconnect.assert_called_once()

    def test_connect_launches_only_when_obs_is_absent(self):
        for running in (False, True):
            controller = OBSController()
            with patch.object(controller, "obs_is_running", return_value=running), \
                 patch("webex_obs.obs_controller.subprocess.run") as run, \
                 patch("webex_obs.obs_controller.obsws") as websocket:
                self.assertTrue(controller.connect())
            self.assertEqual(run.call_count, int(not running))
            websocket.return_value.connect.assert_called_once()

    def test_stop_status_query_prevents_quit_while_recording_active(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.is_recording = controller._owns_recording = True
        controller.ws.call.return_value = _Response(outputActive=True)
        with patch.object(controller, "check_connection", return_value=True), \
             patch.object(controller, "quit_obs") as quit_obs, \
             patch("webex_obs.obs_controller.time.sleep"):
            controller.stop_recording()
        quit_obs.assert_not_called()
        self.assertTrue(controller.is_recording)

    def test_recovery_resumes_the_current_scene(self):
        controller = OBSController()
        controller.current_scene = "Webex-Video"
        with patch.object(controller, "_refresh_recording_status", return_value=False), \
             patch.object(controller, "start_recording", return_value=True) as start:
            self.assertTrue(controller.ensure_recording())
        start.assert_called_once_with(scene_name="Webex-Video")

    def test_transient_start_failure_retries_without_second_obs_restart(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.ws.call.side_effect = [RuntimeError("OBS is still initializing"), _Response()]
        with patch.object(controller, "switch_scene", return_value=True), \
             patch.object(controller, "_refresh_recording_status",
                          side_effect=[False, False, False, True]), \
             patch("webex_obs.obs_controller.time.sleep"):
            self.assertTrue(controller.start_recording())
        self.assertEqual(controller.ws.call.call_count, 2)

    def test_status_poll_catches_delayed_success_without_duplicate_start(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.ws.call.return_value = _Response()
        with patch.object(controller, "switch_scene", return_value=True), \
             patch.object(controller, "_refresh_recording_status", side_effect=[False, False, True]), \
             patch("webex_obs.obs_controller.time.sleep"):
            self.assertTrue(controller.start_recording())
        self.assertEqual(controller.ws.call.call_count, 1)

    def test_live_window_change_updates_screen_capture_without_stopping_recording(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.is_connected = True
        controller.is_recording = True
        controller.current_scene = "Webex-Video"
        windows = [WebexWindow(101, "Weekly meeting", "Webex", 1200, 800),
                   WebexWindow(202, "Shared content", "CiscoCollabHost", 1440, 900)]
        controller.ws.call.side_effect = [_Response(inputKind="screen_capture"), _Response()]
        with patch.object(controller, "list_webex_windows", return_value=windows), \
             patch.object(controller, "check_connection", return_value=True):
            self.assertTrue(controller.bind_webex_video_window(202))
        request = controller.ws.call.call_args_list[1].args[0]
        name, data = request.name, request.dataout
        self.assertEqual(name, "SetInputSettings")
        self.assertEqual(data, {
            "inputName": "Webex-Meeting-Window",
            "inputSettings": {"type": 1, "window": 202}, "overlay": True,
        })
        self.assertEqual(controller.selected_window_id, 202)
        self.assertTrue(controller.is_recording)
        self.assertEqual(controller.current_scene, "Webex-Video")

    def test_entire_screen_selection_uses_obs_display_and_skips_window_polling(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.is_connected = True
        controller.is_recording = True
        controller.current_scene = "Webex-Video"
        controller.selected_window_id = 101
        controller.ws.call.side_effect = [
            _Response(inputKind="screen_capture"),
            _Response(),
        ]

        with patch("webex_obs.obs_controller.active_displays", return_value=[
            ("display-uuid", "Main Display")
        ]):
            self.assertEqual(controller.list_capture_displays(), [
                CaptureDisplay("display-uuid", "Main Display")
            ])
        # Enumerating screens must never request OBS's crashing property list.
        self.assertEqual(controller.ws.call.call_count, 1)
        with patch.object(controller, "list_capture_displays", return_value=[
            CaptureDisplay("display-uuid", "Main Display")
        ]), patch.object(controller, "list_webex_windows") as windows:
            self.assertTrue(controller.select_capture_display("display-uuid"))
            self.assertEqual(controller.poll_webex_window_change(), [])
        windows.assert_not_called()
        request = controller.ws.call.call_args.args[0]
        name, data = request.name, request.dataout
        self.assertEqual(name, "SetInputSettings")
        self.assertEqual(data["inputSettings"], {"type": 0, "display_uuid": "display-uuid"})
        self.assertTrue(controller.is_screen_recording)
        self.assertIsNone(controller.selected_window_id)

    def test_selecting_window_again_resumes_window_following(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.is_connected = True
        controller.selected_display_uuid = "display-uuid"
        controller.ws.call.side_effect = [_Response(inputKind="screen_capture"), _Response()]
        window = WebexWindow(101, "Meeting", "Webex", 1200, 800)
        with patch.object(controller, "list_webex_windows", return_value=[window]), \
             patch.object(controller, "check_connection", return_value=True):
            self.assertTrue(controller.bind_webex_video_window(101))
        self.assertFalse(controller.is_screen_recording)

    def test_new_video_segment_preserves_entire_screen_selection(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.selected_display_uuid = "display-uuid"
        controller.ws.call.return_value = _Response()
        with patch.object(controller, "switch_scene", return_value=True), \
             patch.object(controller, "bind_webex_video_window") as bind_window, \
             patch.object(controller, "_refresh_recording_status", side_effect=[False, True]):
            self.assertTrue(controller.start_recording("Webex-Video"))
        bind_window.assert_not_called()

    def test_legacy_window_source_does_not_offer_entire_screen(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.is_connected = True
        controller.ws.call.return_value = _Response(inputKind="window_capture")
        self.assertEqual(controller.list_capture_displays(), [])

    def test_new_window_is_reported_once_and_selected_window_replacement_is_detected(self):
        controller = OBSController()
        controller.is_recording = True
        controller.current_scene = "Webex-Video"
        controller.selected_window_id = 101
        controller._seen_window_ids = {101}
        meeting = WebexWindow(101, "Meeting", "Webex", 1200, 800)
        shared = WebexWindow(202, "Shared content", "Webex", 1400, 900)
        with patch.object(controller, "list_webex_windows",
                          side_effect=[[meeting, shared], [meeting, shared],
                                       [meeting, shared], [meeting], [shared],
                                       [shared], [shared]]):
            self.assertEqual(controller.poll_webex_window_change(), [])
            self.assertEqual(controller.poll_webex_window_change(), [shared])
            self.assertEqual(controller.poll_webex_window_change(), [])
            self.assertEqual(controller.poll_webex_window_change(), [])
            self.assertEqual(controller.poll_webex_window_change(), [])
            self.assertEqual(controller.poll_webex_window_change(), [shared])
            self.assertEqual(controller.poll_webex_window_change(), [])

    def test_chat_previews_do_not_displace_selected_meeting_window(self):
        controller = OBSController()
        controller.is_recording = True
        controller.current_scene = "Webex-Video"
        controller.selected_window_id = 101
        controller._seen_window_ids = {101}
        meeting = WebexWindow(101, "Weekly meeting", "Webex", 1200, 800)
        preview = WebexWindow(202, "Chat preview", "Webex", 1200, 800)
        tiny_popup = WebexWindow(303, "Colleague", "Webex", 420, 320)
        similar_sized_chat = WebexWindow(404, "Colleague", "Webex", 1200, 800)
        with patch.object(controller, "list_webex_windows",
                          return_value=[meeting, preview, tiny_popup, similar_sized_chat]):
            for _ in range(4):
                self.assertEqual(controller.poll_webex_window_change(), [])

    def test_unknown_size_window_only_prompts_for_explicit_share(self):
        controller = OBSController()
        controller.is_recording = True
        controller.current_scene = "Webex-Video"
        controller.selected_window_id = 101
        controller._seen_window_ids = {101}
        meeting = WebexWindow(101, "Weekly meeting", "Webex", 0, 0)
        other = WebexWindow(202, "Colleague", "Webex", 0, 0)
        share = WebexWindow(303, "Shared content", "Webex", 0, 0)
        with patch.object(controller, "list_webex_windows",
                          return_value=[meeting, other, share]):
            self.assertEqual(controller.poll_webex_window_change(), [])
            self.assertEqual(controller.poll_webex_window_change(), [share])
            self.assertEqual(controller.poll_webex_window_change(), [])

    def test_obs_window_list_supplies_titles_when_quartz_titles_are_unavailable(self):
        controller = OBSController()
        controller.ws = Mock()
        controller.is_connected = True
        controller.ws.call.return_value = _Response(propertyItems=[
            {"itemName": "[Webex] Shared content", "itemValue": 202},
            {"itemName": "[Safari] Browser", "itemValue": 303},
        ])
        with patch("webex_obs.obs_controller.HAS_QUARTZ", False):
            windows = controller.list_webex_windows()
        self.assertEqual(windows, [WebexWindow(202, "Shared content", "Webex", 0, 0)])

    def test_stop_quits_only_when_verified_and_enabled(self):
        for enabled, verified in ((True, True), (False, True), (True, False)):
            controller = OBSController(exit_on_stop=enabled)
            controller.ws = Mock()
            controller.is_recording = controller._owns_recording = True
            controller.recorded_files = ["/tmp/segment.mkv"]
            controller.ws.call.return_value = _Response()
            with patch.object(controller, "check_connection", return_value=True), \
                 patch.object(controller, "_recording_active_status", return_value=True), \
                 patch.object(controller, "_recording_is_confirmed_stopped", return_value=verified), \
                 patch.object(controller, "quit_obs") as quit_obs, \
                 patch("webex_obs.obs_controller.time.sleep"):
                self.assertEqual(controller.stop_recording(), ["/tmp/segment.mkv"] if verified else [])
            self.assertEqual(quit_obs.call_count, int(enabled and verified))


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("failure", ["disconnected", "status-error", "missing-status", "active"])
def test_unverified_stop_preserves_segments_and_selection_for_retry(failure, tmp_path):
    segment = tmp_path / "segment.mkv"
    segment.write_bytes(b"synthetic recording")
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    controller.recorded_files = [str(segment)]
    controller.selected_window_id = 123
    controller.ws = Mock()
    if failure == "status-error":
        controller.ws.call.side_effect = RuntimeError("synthetic failure")
    elif failure == "active":
        controller.ws.call.return_value = _Response(outputActive=True)
    else:
        controller.ws.call.return_value = _Response()
    with patch.object(controller, "check_connection", return_value=failure != "disconnected"), \
         patch.object(controller, "quit_obs") as quit_obs, \
         patch("webex_obs.obs_controller.time.sleep"):
        assert controller.stop_recording() == []
    assert controller.stop_verified is False
    assert controller.is_recording
    assert controller.recorded_files == [str(segment)]
    assert controller.selected_window_id == 123
    assert segment.exists()
    quit_obs.assert_not_called()

    controller.ws.call.side_effect = None
    controller.ws.call.return_value = _Response(outputActive=False)
    with patch.object(controller, "check_connection", return_value=True):
        assert controller.stop_recording() == [str(segment)]
    assert not controller.is_recording
    assert controller.recorded_files == []
    assert controller.selected_window_id is None


def test_status_then_stop_remembers_output_path_from_stop_response(tmp_path):
    segment = tmp_path / "segment.mkv"
    segment.write_bytes(b"synthetic recording")
    controller = OBSController(exit_on_stop=False)
    controller.ws = Mock()
    controller.is_recording = controller._owns_recording = True
    # GetRecordStatus has no outputPath; StopRecord supplies the finalized path.
    controller.ws.call.side_effect = [
        _Response(outputActive=True, outputPaused=False, outputTimecode="00:00:01.000",
                  outputDuration=1000, outputBytes=128),
        _Response(outputActive=True),
        _Response(outputPath=str(segment)),
        _Response(outputActive=False),
    ]
    with patch.object(controller, "check_connection", return_value=True):
        assert controller._refresh_recording_status()
        assert controller.recorded_files == []
        assert controller.stop_recording() == [str(segment)]
    assert not controller.is_recording


@pytest.mark.parametrize("data", [{}, {"outputActive": None}, {"outputActive": 0}, {"outputActive": "false"}])
def test_unknown_status_does_not_clear_owned_session_or_start_recovery(data, tmp_path):
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    segment = tmp_path / "owned.mkv"
    segment.write_bytes(b"synthetic")
    controller.recorded_files = [str(segment)]
    controller.ws = Mock()
    controller.ws.call.return_value = _Response(**data)
    with patch.object(controller, "check_connection", return_value=True), \
         patch.object(controller, "start_recording") as start:
        assert controller._refresh_recording_status() is None
        assert not controller.ensure_recording()
        assert controller.stop_recording() == []
    start.assert_not_called()
    assert controller.recording_status is None
    assert controller.is_recording
    assert not controller.stop_verified
    assert controller.recorded_files == [str(segment)]
    assert segment.exists()


def test_rejected_stop_and_unknown_post_stop_status_preserve_owned_segments(tmp_path):
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    segment, unrelated = tmp_path / "owned.mkv", tmp_path / "unrelated.mkv"
    segment.write_bytes(b"synthetic")
    unrelated.write_bytes(b"unrelated synthetic")
    controller.recorded_files = [str(segment)]
    controller.ws = Mock()
    rejected = _Response(outputPath=str(unrelated))
    rejected.status = False
    statuses = iter([_Response(outputActive=True), rejected])
    controller.ws.call.side_effect = lambda request: next(statuses, _Response(outputActive=None))
    with patch.object(controller, "check_connection", return_value=True), \
         patch("webex_obs.obs_controller.time.sleep"), \
         patch.object(controller, "quit_obs") as quit_obs:
        assert controller.stop_recording() == []
    assert [call.args[0].name for call in controller.ws.call.call_args_list].count("StopRecord") == 1
    assert controller.recorded_files == [str(segment)]
    assert not controller.stop_verified
    assert controller.is_recording
    assert unrelated.exists()
    quit_obs.assert_not_called()


@pytest.mark.parametrize("operation", ["start", "ensure", "stop", "switch"])
def test_independently_started_recording_is_never_adopted_or_stopped(operation, tmp_path):
    external = tmp_path / "external.mkv"
    external.write_bytes(b"synthetic external recording")
    controller = OBSController(exit_on_stop=False)
    controller.ws = Mock()
    controller.ws.call.return_value = _Response(outputActive=True, outputPath=str(external))
    with patch.object(controller, "check_connection", return_value=True), \
         patch.object(controller, "switch_scene") as scene:
        if operation == "start":
            assert not controller.start_recording()
        elif operation == "ensure":
            assert not controller.ensure_recording()
        elif operation == "stop":
            assert controller.stop_recording() == []
        else:
            controller.switch_to_video_mode()
    assert controller.recording_status is True
    assert not controller.is_recording
    assert not controller.stop_verified
    assert controller.recorded_files == []
    assert external.exists()
    scene.assert_not_called()
    assert all(call.args[0].name == "GetRecordStatus" for call in controller.ws.call.call_args_list)


def test_lost_start_response_does_not_claim_an_active_output():
    controller = OBSController(exit_on_stop=False)
    controller.ws = Mock()
    with patch.object(controller, "_refresh_recording_status", side_effect=[False, True]), \
         patch.object(controller, "switch_scene", return_value=True):
        controller.ws.call.side_effect = ConnectionError("synthetic lost response")
        assert not controller.start_recording()
    assert controller.is_recording  # unresolved request keeps the lifecycle reserved
    assert not controller._owns_recording
    assert not controller.stop_verified
    assert controller.recorded_files == []


def test_connection_loss_cannot_adopt_external_output_during_recovery(tmp_path):
    original, external = tmp_path / "original.mkv", tmp_path / "external.mkv"
    for path in (original, external):
        path.write_bytes(b"synthetic")
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    controller.recorded_files = [str(original)]
    controller.ws = Mock()
    controller.disconnect()
    controller.ws = Mock()
    controller.ws.call.return_value = _Response(outputActive=True, outputPath=str(external))
    with patch.object(controller, "check_connection", return_value=True):
        assert not controller.ensure_recording()
        assert not controller.start_recording()
        assert controller.stop_recording() == []
    assert controller.recorded_files == [str(original)]
    assert all(call.args[0].name == "GetRecordStatus" for call in controller.ws.call.call_args_list)
    # A later positive stopped result finalizes only our already known segment.
    controller.ws.call.return_value = _Response(outputActive=False, outputPath=str(external))
    with patch.object(controller, "check_connection", return_value=True):
        assert controller.stop_recording() == [str(original)]
    assert controller.stop_verified
    assert external.exists()


def test_switch_to_video_keeps_the_owned_recording_output():
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    controller.ws = Mock()
    controller.ws.call.return_value = _Response(outputActive=True)
    with patch.object(controller, "check_connection", return_value=True), \
         patch.object(controller, "switch_scene", return_value=True) as scene, \
         patch.object(controller, "bind_webex_video_window") as bind:
        controller.switch_to_video_mode()
    scene.assert_called_once_with("Webex-Video")
    bind.assert_called_once()
    assert all(call.args[0].name == "GetRecordStatus" for call in controller.ws.call.call_args_list)


@pytest.mark.parametrize("change", ["path", "duration"])
def test_observed_external_restart_does_not_adopt_or_stop_replacement(change, tmp_path):
    original, external = tmp_path / "original.mkv", tmp_path / "external.mkv"
    for path in (original, external):
        path.write_bytes(b"synthetic")
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    controller.ws = Mock()
    first = _Response(outputActive=True, outputPath=str(original), outputDuration=5000)
    replaced = (_Response(outputActive=True, outputPath=str(external), outputDuration=6000)
                if change == "path" else _Response(outputActive=True, outputDuration=1000))
    controller.ws.call.side_effect = [first, replaced, replaced]
    with patch.object(controller, "check_connection", return_value=True):
        assert controller.ensure_recording()
        assert not controller.ensure_recording()
        assert controller.stop_recording() == []
    assert controller.recorded_files == [str(original)]
    assert controller.is_recording
    assert not controller.stop_verified
    assert all(call.args[0].name == "GetRecordStatus" for call in controller.ws.call.call_args_list)


def test_observed_idle_output_revokes_ownership_before_external_start():
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    controller.ws = Mock()
    controller.ws.call.side_effect = [_Response(outputActive=True), _Response(outputActive=False),
                                      _Response(outputActive=True)]
    with patch.object(controller, "check_connection", return_value=True):
        assert controller._refresh_recording_status() is True
        assert controller._refresh_recording_status() is False
        assert controller.stop_recording() == []
    assert not controller._owns_recording
    assert controller.is_recording
    assert not controller.stop_verified


def test_accepted_start_and_verified_stop_return_all_owned_segments(tmp_path):
    recovered, current = tmp_path / "recovered.mkv", tmp_path / "current.mkv"
    for path in (recovered, current):
        path.write_bytes(b"synthetic")
    controller = OBSController(exit_on_stop=False)
    controller.recorded_files = [str(recovered)]
    controller.ws = Mock()
    controller.ws.call.side_effect = [
        _Response(outputActive=False), _Response(),  # idle, accepted StartRecord
        _Response(outputActive=True),               # status need not include path
        _Response(outputActive=True),               # stop preflight
        _Response(outputPath=str(current)),         # finalized StopRecord path
        _Response(outputActive=False),              # verified stop
    ]
    with patch.object(controller, "check_connection", return_value=True), \
         patch.object(controller, "switch_scene", return_value=True):
        assert controller.start_recording()
        assert controller.stop_recording() == [str(recovered), str(current)]
    assert controller.stop_verified
    assert not controller.is_recording
    assert controller.recorded_files == []


def test_connection_loss_after_stop_response_keeps_final_path_for_retry(tmp_path):
    segment = tmp_path / "segment.mkv"
    segment.write_bytes(b"synthetic")
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    controller.selected_window_id = 123
    controller.ws = Mock()

    def call(request):
        if request.name == "StopRecord":
            return _Response(outputPath=str(segment))
        if not controller.recorded_files:
            return _Response(outputActive=True)
        raise ConnectionError("synthetic connection loss while finalizing")

    controller.ws.call.side_effect = call
    with patch.object(controller, "check_connection", return_value=True), \
         patch("webex_obs.obs_controller.time.sleep"):
        assert controller.stop_recording() == []
    assert controller.recorded_files == [str(segment)]
    assert controller.is_recording
    assert not controller.stop_verified
    assert controller.selected_window_id == 123
    assert segment.exists()
    controller.ws.call.side_effect = None
    controller.ws.call.return_value = _Response(outputActive=False)
    with patch.object(controller, "check_connection", return_value=True):
        assert controller.stop_recording() == [str(segment)]
    assert controller.stop_verified


def test_rejected_status_response_cannot_confirm_stop():
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    controller.ws = Mock()
    rejected = _Response(outputActive=False)
    rejected.status = False
    controller.ws.call.return_value = rejected
    with patch.object(controller, "check_connection", return_value=True):
        assert controller.stop_recording() == []
    assert controller.recording_status is None
    assert controller.is_recording
    assert not controller.stop_verified
