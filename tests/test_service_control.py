import subprocess
from unittest.mock import patch

import pytest

from webex_obs.service_control import LAUNCH_AGENT_LABEL, stop_launch_agent


def test_quit_unloads_loaded_launch_agent():
    with patch("webex_obs.service_control.LAUNCH_AGENT_PATH") as path, \
         patch("webex_obs.service_control.subprocess.run") as run:
        path.exists.return_value = True
        run.side_effect = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
        assert stop_launch_agent() is True
        assert run.call_args_list[0].args[0] == ["launchctl", "list", LAUNCH_AGENT_LABEL]
        assert run.call_args_list[1].args[0] == ["launchctl", "unload", str(path)]


def test_foreground_quit_does_not_unload_anything():
    with patch("webex_obs.service_control.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 113)
        assert stop_launch_agent() is False
        assert run.call_count == 1


def test_failed_unload_does_not_report_success():
    with patch("webex_obs.service_control.LAUNCH_AGENT_PATH") as path, \
         patch("webex_obs.service_control.subprocess.run") as run:
        path.exists.return_value = True
        run.side_effect = [subprocess.CompletedProcess([], 0),
                           subprocess.CompletedProcess([], 1, stderr="load failed")]
        with pytest.raises(RuntimeError, match="load failed"):
            stop_launch_agent()
