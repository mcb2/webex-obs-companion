import unittest
from unittest.mock import patch

from webex_obs.config import Config
from webex_obs.recording_backend import create_recording_backend


class BackendTests(unittest.TestCase):
    def test_startup_resets_idle_restart_age_instead_of_immediately_restarting(self):
        from unittest.mock import Mock
        from webex_obs.recording_backend import OBSRecordingBackend
        controller = Mock(exit_on_stop=False, address="localhost", is_recording=False)
        controller.initialize.return_value = True
        backend = OBSRecordingBackend(controller)
        backend._maintenance.last_refresh = 0
        backend._maintenance.last_busy = 0
        with patch("webex_obs.recording_backend.time.monotonic", return_value=5000):
            self.assertTrue(backend.initialize())
            self.assertFalse(backend.maintain_idle(lambda: False))
        self.assertEqual(backend._maintenance.last_refresh, 5000)
        controller.quit_obs.assert_not_called()

    def test_idle_interval_reaches_backend_and_can_be_disabled_live(self):
        from unittest.mock import Mock
        from webex_obs.recording_backend import OBSRecordingBackend
        config = Config(_env_file=None, obs_idle_restart_minutes=90)
        with patch("webex_obs.recording_backend.OBSController"):
            backend = create_recording_backend(config)
        self.assertEqual(backend.idle_restart_minutes, 90)
        backend.controller = Mock(is_recording=False)
        backend.configure(config.model_copy(update={"obs_idle_restart_minutes": 0}))
        self.assertEqual(backend.idle_restart_minutes, 0)

    def test_prepare_does_not_record_and_resets_age_only_for_new_obs(self):
        from unittest.mock import Mock
        from webex_obs.recording_backend import OBSRecordingBackend
        for running in (False, True):
            controller = Mock()
            controller.obs_is_running.return_value = running
            controller.check_connection.return_value = True
            backend = OBSRecordingBackend(controller)
            backend._maintenance.last_refresh = 0
            with patch("webex_obs.recording_backend.time.monotonic", return_value=100):
                self.assertTrue(backend.prepare_recording())
            self.assertEqual(backend._maintenance.last_refresh, 0 if running else 100)
            controller.start_recording.assert_not_called()

    def test_factory_passes_existing_obs_options(self):
        config = Config(_env_file=None, obs_ws_host="example.test", obs_ws_port=4466,
                        obs_ws_password="secret", exit_obs_on_recording_stop=False)
        with patch("webex_obs.recording_backend.OBSController") as controller:
            backend = create_recording_backend(config)
            self.assertIs(backend.controller, controller.return_value)
        controller.assert_called_once_with(address="example.test", port=4466,
                                           password="secret", exit_on_stop=False)

    def test_modes_translate_to_existing_obs_scenes(self):
        from unittest.mock import Mock
        from webex_obs.recording_backend import OBSRecordingBackend
        controller = Mock()
        backend = OBSRecordingBackend(controller)
        backend.start_recording("audio")
        backend.start_recording("video")
        backend.retry_start_recording("video")
        self.assertEqual(controller.start_recording.call_args_list[0].kwargs,
                         {"scene_name": "Webex-Audio"})
        self.assertEqual(controller.start_recording.call_args_list[1].kwargs,
                         {"scene_name": "Webex-Video"})
        self.assertEqual(controller.start_recording.call_args_list[2].kwargs,
                         {"scene_name": "Webex-Video"})

    def test_connection_change_waits_until_active_recording_stops(self):
        from unittest.mock import Mock
        from webex_obs.recording_backend import OBSRecordingBackend
        controller = Mock(address="old", port=4455, password="old", is_recording=True)
        controller.stop_recording.return_value = ["recording.mkv"]
        backend = OBSRecordingBackend(controller)
        config = Config(_env_file=None, obs_ws_host="new", obs_ws_port=4466,
                        obs_ws_password="new", exit_obs_on_recording_stop=False)
        backend.configure(config)
        controller.disconnect.assert_not_called()
        self.assertEqual(controller.address, "old")
        self.assertEqual(backend.stop_recording(), ["recording.mkv"])
        controller.disconnect.assert_called_once()
        self.assertEqual((controller.address, controller.port, controller.password),
                         ("new", 4466, "new"))


if __name__ == "__main__":
    unittest.main()
