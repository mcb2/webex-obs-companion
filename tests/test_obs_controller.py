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
    def test_start_aborts_when_required_relaunch_fails(self):
        controller = OBSController(relaunch_per_call=True)
        with patch.object(controller, "relaunch_obs", return_value=False):
            self.assertFalse(controller.start_recording())

    def test_recording_status_is_verified_and_path_is_remembered(self):
        controller = OBSController(relaunch_per_call=False)
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
        controller = OBSController(relaunch_per_call=True)
        controller.current_scene = "Webex-Video"
        with patch.object(controller, "_refresh_recording_status", return_value=False), \
             patch.object(controller, "start_recording", return_value=True) as start:
            self.assertTrue(controller.ensure_recording())
        start.assert_called_once_with(scene_name="Webex-Video", relaunch=True)


if __name__ == "__main__":
    unittest.main()
