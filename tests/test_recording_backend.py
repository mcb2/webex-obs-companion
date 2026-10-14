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
                                           password="secret", exit_on_stop=False,
                                           recording_directory=config.recordings_dir)

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


class _ObservedLock:
    """Expose a contender reaching acquire, without timing-based sleeps."""

    def __init__(self, contender_name):
        import threading
        self.lock = threading.RLock()
        self.contender_name = contender_name
        self.attempted = threading.Event()

    def __enter__(self):
        import threading
        if threading.current_thread().name == self.contender_name:
            self.attempted.set()
        self.lock.acquire()

    def __exit__(self, *exc):
        self.lock.release()


@pytest.mark.parametrize("operation", [
    "stop", "recover", "switch", "window", "display", "windows", "displays", "poll",
    "connect", "initialize", "prepare", "disconnect", "maintain",
])
def test_all_obs_operations_wait_for_inflight_switch(operation):
    import threading
    from unittest.mock import Mock
    from webex_obs.recording_backend import OBSRecordingBackend

    controller = Mock(is_recording=True, current_scene="Webex-Video")
    backend = OBSRecordingBackend(controller)
    backend._lifecycle_lock = _ObservedLock("contender")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def switch():
        entered.set()
        if not release.wait(3):
            raise AssertionError("test did not release switch")
        return True

    controller.switch_to_video_mode.side_effect = switch
    operations = {
        "stop": backend.stop_recording, "recover": backend.ensure_recording,
        "switch": backend.switch_to_video_mode,
        "window": lambda: backend.select_webex_window(101),
        "display": lambda: backend.select_capture_display("synthetic-display"),
        "windows": backend.list_webex_windows, "displays": backend.list_capture_displays,
        "poll": backend.poll_webex_window_change, "connect": backend.connect,
        "initialize": backend.initialize, "prepare": backend.prepare_recording,
        "disconnect": backend.disconnect, "maintain": lambda: backend.maintain_idle(lambda: True),
    }

    def run(action, completed=None):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if completed:
                completed.set()

    owner = threading.Thread(target=run, args=(backend.switch_to_video_mode,), name="owner")
    contender = threading.Thread(target=run, args=(operations[operation], done), name="contender")
    owner.start()
    try:
        assert entered.wait(3)
        contender.start()
        assert backend._lifecycle_lock.attempted.wait(3)
        assert not done.is_set()
        assert [call[0] for call in controller.mock_calls] == ["switch_to_video_mode"]
    finally:
        release.set()
        owner.join(3)
        if contender.ident:
            contender.join(3)
    assert not owner.is_alive() and not contender.is_alive()
    assert not errors
    assert done.is_set()


def test_settings_queue_does_not_wait_for_obs_and_applies_after_verified_stop():
    import threading
    from unittest.mock import Mock
    from webex_obs.recording_backend import OBSRecordingBackend

    controller = Mock(address="old", port=4455, password="synthetic", is_recording=True)
    backend = OBSRecordingBackend(controller)
    entered, release, configured = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def stop():
        entered.set()
        assert release.wait(3)
        controller.is_recording = False
        controller.stop_verified = True
        return []

    controller.stop_recording.side_effect = stop

    def run_stop():
        try:
            backend.stop_recording()
        except BaseException as exc:
            errors.append(exc)

    def configure():
        try:
            backend.configure(Config(_env_file=None, obs_ws_host="new", exit_obs_on_recording_stop=False))
            configured.set()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_stop)
    settings = threading.Thread(target=configure)
    worker.start()
    try:
        assert entered.wait(3)
        settings.start()
        assert configured.wait(3), "Settings waited for blocking OBS work"
        assert controller.address == "old"
        controller.disconnect.assert_not_called()
    finally:
        release.set()
        worker.join(3)
        if settings.ident:
            settings.join(3)
    assert not errors
    assert not worker.is_alive() and not settings.is_alive()
    assert controller.address == "new"
    controller.disconnect.assert_called_once()


@pytest.mark.parametrize("contending_operation", ["switch", "recover", "window", "display"])
def test_completed_stop_prevents_waiting_mutations_from_restarting_or_rebinding(
    obs_v5_client, contending_operation
):
    import threading
    from webex_obs.obs_controller import OBSController
    from webex_obs.recording_backend import OBSRecordingBackend

    entered, release = threading.Event(), threading.Event()
    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = True
    controller.is_connected = True
    controller.current_scene = "Webex-Video" if contending_operation in ("window", "display") else "Webex-Audio"
    active = True
    calls, errors, results = [], [], []

    def respond(name, data):
        nonlocal active
        calls.append(name)
        if name == "StopRecord":
            entered.set()
            assert release.wait(3)
            active = False
            return True, {"outputPath": ""}
        if name == "GetRecordStatus":
            return True, {"outputActive": active}
        return True, None

    controller.ws = obs_v5_client(respond)
    backend = OBSRecordingBackend(controller)
    backend._lifecycle_lock = _ObservedLock("contender")
    operations = {
        "switch": backend.switch_to_video_mode, "recover": backend.ensure_recording,
        "window": lambda: backend.select_webex_window(101),
        "display": lambda: backend.select_capture_display("synthetic-display"),
    }

    def run(action):
        try:
            results.append(action())
        except BaseException as exc:
            errors.append(exc)

    stopper = threading.Thread(target=run, args=(backend.stop_recording,))
    contender = threading.Thread(target=run, args=(operations[contending_operation],), name="contender")
    stopper.start()
    try:
        assert entered.wait(3)
        contender.start()
        assert backend._lifecycle_lock.attempted.wait(3)
    finally:
        release.set()
        stopper.join(3)
        if contender.ident:
            contender.join(3)
    assert not errors
    assert not stopper.is_alive() and not contender.is_alive()
    assert controller.stop_verified and not controller.is_recording
    assert False in results
    assert not {"StartRecord", "SetCurrentProgramScene", "SetInputSettings"}.intersection(calls)


@pytest.mark.parametrize("owner_operation", ["switch", "recover"])
def test_switch_and_recovery_share_one_output_and_repeated_switch_is_idempotent(obs_v5_client, owner_operation):
    import threading
    from webex_obs.obs_controller import OBSController
    from webex_obs.recording_backend import OBSRecordingBackend

    controller = OBSController(exit_on_stop=False)
    controller.is_recording = controller._owns_recording = controller.is_connected = True
    controller.recorded_files = ["synthetic-prior-segment.mkv"]
    active = owner_operation == "switch"
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []
    blocked_request = "SetCurrentProgramScene" if owner_operation == "switch" else "StartRecord"

    def respond(name, data):
        nonlocal active
        calls.append((name, data))
        if name == blocked_request and not entered.is_set():
            entered.set()
            assert release.wait(3)
        if name == "StartRecord":
            active = True
        return True, {"outputActive": active} if name == "GetRecordStatus" else None

    controller.ws = obs_v5_client(respond)
    backend = OBSRecordingBackend(controller)
    backend._lifecycle_lock = _ObservedLock("contender")

    def run(action):
        try:
            assert action() is True
        except BaseException as exc:
            errors.append(exc)

    first_action = backend.switch_to_video_mode if owner_operation == "switch" else backend.ensure_recording
    second_action = backend.ensure_recording if owner_operation == "switch" else backend.switch_to_video_mode
    owner = threading.Thread(target=run, args=(first_action,))
    contender = threading.Thread(target=run, args=(second_action,), name="contender")
    with patch.object(controller, "bind_webex_video_window", return_value=True):
        owner.start()
        try:
            assert entered.wait(3)
            contender.start()
            assert backend._lifecycle_lock.attempted.wait(3)
        finally:
            release.set()
            owner.join(3)
            if contender.ident:
                contender.join(3)
        assert not backend.switch_to_video_mode()
    assert not errors
    assert not owner.is_alive() and not contender.is_alive()
    assert controller.is_recording and controller._owns_recording
    assert controller.current_scene == "Webex-Video"
    assert controller.recorded_files == ["synthetic-prior-segment.mkv"]
    assert sum(name == "StartRecord" for name, _ in calls) == (owner_operation == "recover")
    assert sum(name == "SetCurrentProgramScene" and data["sceneName"] == "Webex-Video" for name, data in calls) == 1
    assert not any(name == "StopRecord" for name, _ in calls)
