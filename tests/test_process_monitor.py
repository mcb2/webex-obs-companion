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

    def net_connections(self, kind):
        return [_Connection(self._port)]


class ProcessMonitorTests(unittest.TestCase):
    def test_idle_webex_socket_is_not_enough(self):
        monitor = ProcessMonitor()
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("Webex", 5004)]), \
             patch.object(monitor, "_active_call_window_name", return_value=None):
            self.assertFalse(monitor.is_webex_running())

    def test_general_webex_socket_requires_call_window(self):
        monitor = ProcessMonitor()
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("Webex", 5004)]), \
             patch.object(monitor, "_active_call_window_name", return_value="Mark Bahler"):
            self.assertTrue(monitor.is_webex_running())
            self.assertIn("call window 'Mark Bahler'", monitor._last_detection_reason)
            self.assertEqual(monitor.current_call_title, "Mark Bahler")

    def test_call_specific_media_process_is_strong_evidence(self):
        monitor = ProcessMonitor()
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("CiscoCollabHost", 9000)]), \
             patch.object(monitor, "_active_call_window_name", return_value=None):
            self.assertTrue(monitor.is_webex_running())
            self.assertIn("ciscocollabhost", monitor._last_detection_reason)

    def test_main_and_lingering_floating_windows_are_not_call_windows(self):
        monitor = ProcessMonitor()
        result = types.SimpleNamespace(
            returncode=0,
            stdout="Webex\nWebex multitasking floating window\n",
            stderr="",
        )
        with patch("webex_obs.process_monitor.subprocess.run", return_value=result):
            self.assertIsNone(monitor._active_call_window_name())

    def test_call_start_requires_two_consecutive_checks(self):
        monitor = ProcessMonitor(poll_interval=0)
        states = iter([True, True])
        with patch.object(monitor, "is_webex_running", side_effect=lambda: next(states)), \
             patch("webex_obs.process_monitor.time.sleep"):
            self.assertTrue(monitor.wait_for_state_change())
            self.assertTrue(monitor.is_in_meeting)

    def test_transient_detection_loss_does_not_end_call(self):
        monitor = ProcessMonitor(call_end_grace_seconds=15)
        with patch.object(monitor, "is_webex_running", side_effect=[False, True]), \
             patch("webex_obs.process_monitor.time.monotonic", return_value=100):
            self.assertFalse(monitor.has_call_ended())
            self.assertFalse(monitor.has_call_ended())
            self.assertIsNone(monitor._inactive_since)

    def test_sustained_detection_loss_ends_call_after_grace_period(self):
        monitor = ProcessMonitor(call_end_grace_seconds=15)
        with patch.object(monitor, "is_webex_running", return_value=False), \
             patch(
                 "webex_obs.process_monitor.time.monotonic",
                 side_effect=[100, 114.9, 115],
             ):
            self.assertFalse(monitor.has_call_ended())
            self.assertFalse(monitor.has_call_ended())
            self.assertTrue(monitor.has_call_ended())

    def test_declined_call_is_suppressed_when_same_title_returns(self):
        monitor = ProcessMonitor()
        monitor.current_call_title = "Weekly Sync"
        monitor.suppress_current_call_prompt()
        monitor.current_call_title = None
        with patch.object(monitor, "_active_call_window_name", return_value="Weekly Sync"):
            self.assertTrue(monitor.should_suppress_automatic_prompt())

    def test_different_call_title_clears_declined_call_suppression(self):
        monitor = ProcessMonitor()
        monitor.current_call_title = "Weekly Sync"
        monitor.suppress_current_call_prompt()
        monitor.current_call_title = None
        with patch.object(monitor, "_active_call_window_name", return_value="Customer Call"):
            self.assertFalse(monitor.should_suppress_automatic_prompt())
            self.assertIsNone(monitor._suppressed_call_title)

    def test_untitled_declined_call_remains_suppressed_until_title_changes(self):
        monitor = ProcessMonitor()
        with patch.object(monitor, "_active_call_window_name", return_value=None):
            monitor.suppress_current_call_prompt()
            self.assertTrue(monitor.should_suppress_automatic_prompt())
        with patch.object(monitor, "_active_call_window_name", return_value="New Meeting"):
            self.assertFalse(monitor.should_suppress_automatic_prompt())


if __name__ == "__main__":
    unittest.main()
