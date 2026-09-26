import unittest
from unittest.mock import patch

from webex_obs.config import Config
from webex_obs.recording_backend import create_recording_backend


class BackendTests(unittest.TestCase):
    def test_factory_passes_existing_obs_options(self):
        config = Config(_env_file=None, obs_ws_host="example.test", obs_ws_port=4466,
                        obs_ws_password="secret", relaunch_obs_per_call=False)
        with patch("webex_obs.recording_backend.OBSController") as controller:
            backend = create_recording_backend(config)
            self.assertIs(backend.controller, controller.return_value)
        controller.assert_called_once_with(address="example.test", port=4466,
                                           password="secret", relaunch_per_call=False)

    def test_modes_translate_to_existing_obs_scenes(self):
        from unittest.mock import Mock
        from webex_obs.recording_backend import OBSRecordingBackend
        controller = Mock()
        backend = OBSRecordingBackend(controller)
        backend.start_recording("audio")
        backend.start_recording("video", relaunch=True)
        backend.retry_start_recording("video")
        self.assertEqual(controller.start_recording.call_args_list[0].kwargs,
                         {"scene_name": "Webex-Audio", "relaunch": False})
        self.assertEqual(controller.start_recording.call_args_list[1].kwargs,
                         {"scene_name": "Webex-Video", "relaunch": True})
        self.assertEqual(controller.start_recording.call_args_list[2].kwargs,
                         {"scene_name": "Webex-Video", "skip_relaunch": True})

    def test_connection_change_waits_until_active_recording_stops(self):
        from unittest.mock import Mock
        from webex_obs.recording_backend import OBSRecordingBackend
        controller = Mock(address="old", port=4455, password="old", is_recording=True)
        controller.stop_recording.return_value = ["recording.mkv"]
        backend = OBSRecordingBackend(controller)
        config = Config(_env_file=None, obs_ws_host="new", obs_ws_port=4466,
                        obs_ws_password="new", relaunch_obs_per_call=False)
        backend.configure(config)
        controller.disconnect.assert_not_called()
        self.assertEqual(controller.address, "old")
        self.assertEqual(backend.stop_recording(), ["recording.mkv"])
        controller.disconnect.assert_called_once()
        self.assertEqual((controller.address, controller.port, controller.password),
                         ("new", 4466, "new"))


if __name__ == "__main__":
    unittest.main()
