import sys
import types
import unittest
from unittest.mock import Mock, patch


fake_requests = types.SimpleNamespace(
    GetRecordStatus=lambda: "get-status",
    StartRecord=lambda: "start",
    StopRecord=lambda: "stop",
    SetCurrentProgramScene=lambda **kwargs: ("scene", kwargs),
    GetVersion=lambda: "version",
)
fake_obs_module = types.ModuleType("obswebsocket")
fake_obs_module.obsws = Mock
fake_obs_module.requests = fake_requests
sys.modules.setdefault("obswebsocket", fake_obs_module)

from webex_obs.obs_controller import OBSController


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
