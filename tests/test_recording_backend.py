import unittest
from unittest.mock import patch

import pytest

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
        def stop():
            controller.is_recording = False
            controller.stop_verified = True
            return ["recording.mkv"]
        controller.stop_recording.side_effect = stop
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


def test_unverified_stop_keeps_pending_connection_until_later_verified_stop():
    from unittest.mock import Mock
    from webex_obs.recording_backend import OBSRecordingBackend

    controller = Mock(address="old", port=4455, password="old", is_recording=True)
    controller.stop_recording.return_value = ["synthetic.mkv"]
    backend = OBSRecordingBackend(controller)
    backend.configure(Config(_env_file=None, obs_ws_host="new", obs_ws_password="new"))

    assert backend.stop_recording() == ["synthetic.mkv"]
    controller.disconnect.assert_not_called()
    assert controller.address == "old"

    controller.is_recording = False
    controller.stop_verified = True
    assert backend.stop_recording() == ["synthetic.mkv"]
    controller.disconnect.assert_called_once()
    assert controller.address == "new"


def test_unknown_status_and_stale_inactive_flag_cannot_replace_connection():
    from unittest.mock import Mock
    from webex_obs.recording_backend import OBSRecordingBackend

    controller = Mock(address="old", port=4455, password="synthetic", is_recording=False, stop_verified=False)
    controller._recording_active_status.return_value = None
    controller.stop_recording.return_value = []
    backend = OBSRecordingBackend(controller)
    backend.configure(Config(_env_file=None, obs_ws_host="new"))
    backend.stop_recording()
    assert backend._pending_connection is not None
    assert controller.address == "old"
    controller.disconnect.assert_not_called()


def test_stop_status_is_exposed_separately_from_pending_session():
    from unittest.mock import Mock
    from webex_obs.recording_backend import OBSRecordingBackend

    controller = Mock(is_recording=True, recording_status=None, stop_verified=False)
    backend = OBSRecordingBackend(controller)
    assert backend.is_recording
    assert backend.recording_status is None
    assert backend.stop_verified is False


def test_configure_defers_network_work_until_verified_idle_preparation():
    from unittest.mock import Mock
    from webex_obs.recording_backend import OBSRecordingBackend

    controller = Mock(address="old", port=4455, password="synthetic", is_recording=False)
    controller.obs_is_running.return_value = True
    controller.check_connection.return_value = True
    controller._recording_active_status.return_value = False
    backend = OBSRecordingBackend(controller)
    backend.configure(Config(_env_file=None, obs_ws_host="new"))
    controller._recording_active_status.assert_not_called()
    controller.disconnect.assert_not_called()
    assert backend.prepare_recording()
    controller.disconnect.assert_called_once()
    assert controller.address == "new"
    assert backend._pending_connection is None


def test_external_recording_blocks_pending_connection_during_preparation():
    from unittest.mock import Mock
    from webex_obs.recording_backend import OBSRecordingBackend

    controller = Mock(address="old", port=4455, password="synthetic", is_recording=False)
    controller.obs_is_running.return_value = True
    controller.check_connection.return_value = True
    controller._recording_active_status.return_value = True
    backend = OBSRecordingBackend(controller)
    backend.configure(Config(_env_file=None, obs_ws_host="new"))
    assert backend.prepare_recording()
    controller.disconnect.assert_not_called()
    assert controller.address == "old"
    assert backend._pending_connection is not None


@pytest.mark.parametrize("failure", ["connection-loss", "rejected-stop", "unknown-status"])
def test_real_backend_retains_old_connection_and_segments_after_failed_stop(tmp_path, failure):
    from unittest.mock import Mock
    from obswebsocket import requests
    from webex_obs.obs_controller import OBSController
    from webex_obs.recording_backend import OBSRecordingBackend

    def response(**data):
        res = requests.GetRecordStatus()
        res.input(data, True)
        return res

    path = tmp_path / "owned.mkv"
    path.write_bytes(b"synthetic")
    controller = OBSController(address="old", exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    controller.recorded_files = [str(path)]
    controller.selected_window_id = 123
    controller.ws = Mock()
    if failure == "rejected-stop":
        rejected = response()
        rejected.status = False
        controller.ws.call.side_effect = lambda request: (rejected if request.name == "StopRecord"
                                                        else response(outputActive=True))
    else:
        controller.ws.call.return_value = response(outputActive=None)
    backend = OBSRecordingBackend(controller)
    with patch.object(controller, "check_connection", return_value=failure != "connection-loss"), \
         patch("webex_obs.obs_controller.time.sleep"), \
         patch.object(controller, "disconnect") as disconnect:
        backend.configure(Config(_env_file=None, obs_ws_host="new", exit_obs_on_recording_stop=False))
        assert backend.stop_recording() == []
        backend.configure(Config(_env_file=None, obs_ws_host="newer", exit_obs_on_recording_stop=False))
        assert not backend.stop_verified
        assert backend.is_recording
        assert controller.address == "old"
        assert controller.recorded_files == [str(path)]
        assert controller.selected_window_id == 123
        disconnect.assert_not_called()

        controller.ws.call.side_effect = None
        controller.ws.call.return_value = response(outputActive=False)
        with patch.object(controller, "check_connection", return_value=True):
            assert backend.stop_recording() == [str(path)]
        disconnect.assert_called_once()
        assert controller.address == "newer"
        assert backend.stop_verified
        assert controller.recorded_files == []
        assert path.exists()
