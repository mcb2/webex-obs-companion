from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from webex_obs.obs_maintenance import OBSMaintenance


def ready():
    policy = OBSMaintenance()
    policy.last_refresh = 0
    policy.last_busy = 0
    controller = Mock(exit_on_stop=False, address="localhost", is_recording=False)
    controller.obs_is_running.return_value = True
    controller.outputs_confirmed_idle.return_value = True
    controller.quit_obs.return_value = True
    controller.connect.return_value = True
    return policy, controller


def run(policy, controller, minute=15, interval=60, pending=lambda: False):
    with patch("webex_obs.obs_maintenance.time.monotonic", return_value=4000), \
         patch("webex_obs.obs_maintenance.datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 9, 12, minute)
        return policy.maintain(controller, interval, pending)


@pytest.mark.parametrize("minute", [0, 1, 4, 25, 29, 30, 34, 55, 59])
def test_never_restart_near_hour_or_half_hour(minute):
    policy, controller = ready()
    assert not run(policy, controller, minute)
    controller.outputs_confirmed_idle.assert_not_called()
    controller.quit_obs.assert_not_called()


@pytest.mark.parametrize("minute", [5, 15, 24, 35, 45, 54])
def test_due_idle_restart_reopens_obs_away_from_call_boundaries(minute):
    policy, controller = ready()
    assert run(policy, controller, minute)
    controller.quit_obs.assert_called_once()
    controller.connect.assert_called_once()
    assert policy.last_refresh == 4000


@pytest.mark.parametrize("condition", ["disabled", "not_due", "recent_evidence", "pending", "recording", "exit", "remote", "outputs", "retry"])
def test_unsafe_or_unnecessary_restart_is_deferred(condition):
    policy, controller = ready()
    interval, pending = 60, lambda: False
    if condition == "disabled":
        interval = 0
    elif condition == "not_due":
        policy.last_refresh = 1000
    elif condition == "recent_evidence":
        policy.last_busy = 3900
    elif condition == "pending":
        pending = lambda: True
    elif condition == "recording":
        controller.is_recording = True
    elif condition == "exit":
        controller.exit_on_stop = True
    elif condition == "remote":
        controller.address = "remote.example"
    elif condition == "outputs":
        controller.outputs_confirmed_idle.return_value = False
    else:
        policy.retry_after = 4100
    assert not run(policy, controller, interval=interval, pending=pending)
    controller.quit_obs.assert_not_called()


def test_request_arriving_during_output_probe_cancels_restart():
    policy, controller = ready()
    assert not run(policy, controller, pending=Mock(side_effect=[False, True]))
    controller.outputs_confirmed_idle.assert_called_once()
    controller.quit_obs.assert_not_called()


def test_failed_shutdown_does_not_launch_second_obs_and_backs_off():
    policy, controller = ready()
    controller.quit_obs.return_value = False
    assert not run(policy, controller)
    controller.connect.assert_not_called()
    assert policy.retry_after == 4120
    controller.quit_obs.reset_mock()
    assert not run(policy, controller)
    controller.quit_obs.assert_not_called()


def test_absent_obs_is_reopened_without_restarting_fresh_instance():
    policy, controller = ready()
    controller.obs_is_running.return_value = False
    assert run(policy, controller)
    controller.connect.assert_called_once()
    controller.quit_obs.assert_not_called()
    controller.outputs_confirmed_idle.assert_not_called()


def test_keep_open_defaults_and_interval_validation():
    from pydantic import ValidationError
    from webex_obs.config import Config
    config = Config(_env_file=None)
    assert config.exit_obs_on_recording_stop is False
    assert config.obs_idle_restart_minutes == 60
    assert Config(_env_file=None, OBS_IDLE_RESTART_MINUTES="0").obs_idle_restart_minutes == 0
    for interval in (-1, 1441, "invalid"):
        with pytest.raises(ValidationError):
            Config(_env_file=None, obs_idle_restart_minutes=interval)


def test_restart_interval_is_persisted_and_reloaded(tmp_path):
    from webex_obs.config import Config
    from webex_obs.settings_store import save_settings
    path = tmp_path / ".env"
    save_settings({"obs_idle_restart_minutes": "0", "exit_obs_on_recording_stop": False}, path)
    config = Config(_env_file=path)
    assert config.obs_idle_restart_minutes == 0
    assert config.exit_obs_on_recording_stop is False
