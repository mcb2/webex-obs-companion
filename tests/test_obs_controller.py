import sys
import types
import unittest
from unittest.mock import Mock, patch


fake_requests = types.SimpleNamespace(
    GetRecordStatus=lambda: "get-status",
    StartRecord=lambda: "start",
    StopRecord=lambda: "stop",
    SetCurrentProgramScene=lambda **kwargs: ("scene", kwargs),
    GetInputSettings=lambda **kwargs: ("get-input-settings", kwargs),
    SetInputSettings=lambda **kwargs: ("set-input-settings", kwargs),
    GetInputPropertiesListPropertyItems=lambda **kwargs: ("get-window-list", kwargs),
    GetVersion=lambda: "version",
)
fake_obs_module = types.ModuleType("obswebsocket")
fake_obs_module.obsws = Mock
fake_obs_module.requests = fake_requests
sys.modules.setdefault("obswebsocket", fake_obs_module)

from webex_obs.obs_controller import CaptureDisplay, OBSController, WebexWindow


class _Response:
    def __init__(self, **data):
        self.datain = data


class OBSControllerTests(unittest.TestCase):
    def test_startup_closes_new_obs_only_after_confirming_idle(self):
        controller = OBSController()
        with patch.object(controller, "obs_is_running", return_value=False), \
             patch.object(controller, "connect", return_value=True), \
             patch.object(controller, "_recording_active_status", return_value=False), \
             patch.object(controller, "quit_obs") as quit_obs:
            self.assertTrue(controller.initialize())
        quit_obs.assert_called_once()

    def test_startup_keeps_existing_obs_or_unverified_recording(self):
        for running, active in ((True, False), (False, True)):
            controller = OBSController()
            with patch.object(controller, "obs_is_running", return_value=running), \
                 patch.object(controller, "connect", return_value=True), \
                 patch.object(controller, "_recording_active_status", return_value=active), \
                 patch.object(controller, "quit_obs") as quit_obs:
                self.assertTrue(controller.initialize())
            quit_obs.assert_not_called()

    def test_startup_retries_transient_unknown_status_before_quitting(self):
        controller = OBSController()
        with patch.object(controller, "obs_is_running", return_value=False), \
             patch.object(controller, "connect", return_value=True), \
             patch.object(controller, "_recording_active_status", side_effect=[None, None, False]) as status, \
             patch.object(controller, "quit_obs") as quit_obs, \
             patch("webex_obs.obs_controller.time.sleep"):
            self.assertTrue(controller.initialize())
        self.assertEqual(status.call_count, 3)
        quit_obs.assert_called_once()

    def test_startup_never_quits_after_repeated_unavailable_status(self):
        controller = OBSController()
        with patch.object(controller, "obs_is_running", return_value=False), \
             patch.object(controller, "connect", return_value=True), \
             patch.object(controller, "_recording_active_status", return_value=None), \
             patch.object(controller, "quit_obs") as quit_obs, \
             patch("webex_obs.obs_controller.time.sleep"):
            self.assertTrue(controller.initialize())
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
        controller.ws.call.side_effect = [_Response(), _Response(outputActive=True)]
        with patch.object(controller, "check_connection", return_value=True), \
             patch.object(controller, "quit_obs") as quit_obs, \
             patch("webex_obs.obs_controller.time.sleep"):
            controller.stop_recording()
        quit_obs.assert_not_called()
        self.assertTrue(controller.is_recording)

    def test_recording_status_is_verified_and_path_is_remembered(self):
        controller = OBSController(exit_on_stop=False)
        controller.ws = Mock()
        controller.is_connected = True
        controller.ws.call.return_value = _Response(
            outputActive=True,
            outputPath="/tmp/segment.mkv",
        )
        with patch.object(controller, "check_connection", return_value=True):
            self.assertTrue(controller._refresh_recording_status())
        self.assertTrue(controller.is_recording)
        self.assertEqual(controller.recorded_files, ["/tmp/segment.mkv"])

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
        controller.ws.call.side_effect = [RuntimeError("OBS is still initializing"), Mock()]
        with patch.object(controller, "switch_scene", return_value=True), \
             patch.object(controller, "_refresh_recording_status",
                          side_effect=[False, False, False, True]), \
             patch("webex_obs.obs_controller.time.sleep"):
            self.assertTrue(controller.start_recording())
        self.assertEqual(controller.ws.call.call_count, 2)

    def test_status_poll_catches_delayed_success_without_duplicate_start(self):
        controller = OBSController()
        controller.ws = Mock()
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
        name, data = (request[0], request[1]) if isinstance(request, tuple) else (
            request.name, request.dataout
        )
        self.assertIn(name, ("SetInputSettings", "set-input-settings"))
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
            _Response(propertyItems=[
                {"itemName": " ", "itemValue": ""},
                {"itemName": "Main Display", "itemValue": "display-uuid"},
            ]),
            _Response(),
        ]

        self.assertEqual(controller.list_capture_displays(), [
            CaptureDisplay("display-uuid", "Main Display")
        ])
        with patch.object(controller, "list_capture_displays", return_value=[
            CaptureDisplay("display-uuid", "Main Display")
        ]), patch.object(controller, "list_webex_windows") as windows:
            self.assertTrue(controller.select_capture_display("display-uuid"))
            self.assertEqual(controller.poll_webex_window_change(), [])
        windows.assert_not_called()
        request = controller.ws.call.call_args.args[0]
        name, data = (request[0], request[1]) if isinstance(request, tuple) else (
            request.name, request.dataout
        )
        self.assertIn(name, ("SetInputSettings", "set-input-settings"))
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
            controller.recorded_files = ["/tmp/segment.mkv"]
            controller.ws.call.return_value = _Response()
            with patch.object(controller, "check_connection", return_value=True), \
                 patch.object(controller, "_recording_is_confirmed_stopped", return_value=verified), \
                 patch.object(controller, "quit_obs") as quit_obs, \
                 patch("webex_obs.obs_controller.time.sleep"):
                self.assertEqual(controller.stop_recording(), ["/tmp/segment.mkv"])
            self.assertEqual(quit_obs.call_count, int(enabled and verified))


if __name__ == "__main__":
    unittest.main()
