import socket
import sys
import types
import unittest
from unittest.mock import patch


class _PsutilError(Exception):
    pass


fake_psutil = types.ModuleType("psutil")
fake_psutil.NoSuchProcess = _PsutilError
fake_psutil.AccessDenied = _PsutilError
fake_psutil.ZombieProcess = _PsutilError
fake_psutil.process_iter = lambda attrs: []
sys.modules.setdefault("psutil", fake_psutil)

from webex_obs.process_monitor import ProcessMonitor


class _Connection:
    type = socket.SOCK_DGRAM

    def __init__(self, port):
        self.raddr = types.SimpleNamespace(port=port)


class _Process:
    def __init__(self, name, port):
        self.info = {"name": name, "exe": ""}
        self._port = port

    def connections(self, kind):
        return [_Connection(self._port)]


class ProcessMonitorTests(unittest.TestCase):
    def test_idle_webex_socket_is_not_enough(self):
        monitor = ProcessMonitor()
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("Webex", 5004)]), \
             patch.object(monitor, "_has_active_call_window", return_value=False):
            self.assertFalse(monitor.is_webex_running())

    def test_general_webex_socket_requires_call_window(self):
        monitor = ProcessMonitor()
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("Webex", 5004)]), \
             patch.object(monitor, "_has_active_call_window", return_value=True):
            self.assertTrue(monitor.is_webex_running())
            self.assertIn("Webex window plus", monitor._last_detection_reason)

    def test_call_specific_media_process_is_strong_evidence(self):
        monitor = ProcessMonitor()
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("CiscoCollabHost", 9000)]), \
             patch.object(monitor, "_has_active_call_window", return_value=False):
            self.assertTrue(monitor.is_webex_running())
            self.assertIn("ciscocollabhost", monitor._last_detection_reason)

    def test_call_start_requires_two_consecutive_checks(self):
        monitor = ProcessMonitor(poll_interval=0)
        states = iter([True, True])
        with patch.object(monitor, "is_webex_running", side_effect=lambda: next(states)), \
             patch("webex_obs.process_monitor.time.sleep"):
            self.assertTrue(monitor.wait_for_state_change())
            self.assertTrue(monitor.is_in_meeting)


if __name__ == "__main__":
    unittest.main()
