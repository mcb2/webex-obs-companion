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

from webex_obs.core_audio_monitor import AudioActivity
from webex_obs.process_monitor import TEAMS, WEBEX, ZOOM, ProcessMonitor


class _Connection:
    type = socket.SOCK_DGRAM

    def __init__(self, port):
        self.raddr = types.SimpleNamespace(port=port)


class _Process:
    def __init__(self, name, port, pid=123):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "exe": ""}
        self._port = port

    def net_connections(self, kind):
        return [_Connection(self._port)]


class _AudioMonitor:
    available = True

    def __init__(self, activity):
        self.activity = activity
        self.requested_pids = []

    def activity_for_pids(self, pids):
        self.requested_pids.append(pids)
        return self.activity


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

    def test_webex_media_process_without_call_controls_is_not_enough(self):
        audio = _AudioMonitor(AudioActivity(available=False))
        monitor = ProcessMonitor(audio_monitor=audio)
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("CiscoCollabHost", 9000)]), \
             patch.object(monitor, "_active_call_window_name", return_value=None):
            self.assertFalse(monitor.is_webex_running())

    def test_webex_chat_attachment_does_not_trigger_call(self):
        audio = _AudioMonitor(AudioActivity(available=True, output_pids=(123,)))
        monitor = ProcessMonitor(audio_monitor=audio)
        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("CiscoCollabHost", 9000)],
        ), patch.object(
            monitor, "_active_call_window_name", return_value=None
        ):
            self.assertFalse(monitor.is_webex_running())

    def test_webex_input_audio_and_udp_detect_call_without_window(self):
        audio = _AudioMonitor(AudioActivity(available=True, input_pids=(123,)))
        monitor = ProcessMonitor(audio_monitor=audio)
        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("CiscoCollabHost", 9000, pid=123)],
        ), patch.object(
            monitor, "_active_call_window_name", return_value=None
        ):
            self.assertTrue(monitor.is_webex_running())
            self.assertIs(monitor.current_platform, WEBEX)
            self.assertIn("Core Audio input", monitor._last_detection_reason)

    def test_webex_attachment_window_is_rejected_with_output_audio(self):
        audio = _AudioMonitor(AudioActivity(available=True, output_pids=(123,)))
        monitor = ProcessMonitor(audio_monitor=audio)
        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("WebexHelper", 5004, pid=123)],
        ), patch.object(
            monitor,
            "_active_call_window_name",
            return_value="quarterly-results.pdf",
        ):
            self.assertFalse(monitor.is_webex_running())

    def test_webex_listen_only_call_is_detected(self):
        audio = _AudioMonitor(AudioActivity(available=True, output_pids=(123,)))
        monitor = ProcessMonitor(audio_monitor=audio)
        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("CiscoCollabHost", 9000)],
        ), patch.object(
            monitor, "_active_call_window_name", return_value="Weekly Sync"
        ):
            self.assertTrue(monitor.is_webex_running())
            self.assertIn("output-only Core Audio", monitor._last_detection_reason)

    def test_webex_window_probe_returns_none_without_non_idle_window(self):
        monitor = ProcessMonitor()
        result = types.SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("webex_obs.process_monitor.subprocess.run", return_value=result):
            self.assertIsNone(monitor._active_call_window_name())

    def test_lightweight_webex_window_probe_returns_meeting_title(self):
        monitor = ProcessMonitor()
        result = types.SimpleNamespace(
            returncode=0,
            stdout="Weekly Sync\n",
            stderr="",
        )
        with patch("webex_obs.process_monitor.subprocess.run", return_value=result):
            self.assertEqual(monitor._active_call_window_name(), "Weekly Sync")

    def test_call_start_requires_two_consecutive_checks(self):
        monitor = ProcessMonitor(poll_interval=0)
        states = iter([True, True])
        with patch.object(monitor, "is_call_active", side_effect=lambda: next(states)), \
             patch("webex_obs.process_monitor.time.sleep"):
            self.assertTrue(monitor.wait_for_state_change())
            self.assertTrue(monitor.is_in_meeting)

    def test_transient_detection_loss_does_not_end_call(self):
        monitor = ProcessMonitor(call_end_grace_seconds=15)
        with patch.object(monitor, "is_call_active", side_effect=[False, True]), \
             patch("webex_obs.process_monitor.time.monotonic", return_value=100):
            self.assertFalse(monitor.has_call_ended())
            self.assertFalse(monitor.has_call_ended())
            self.assertIsNone(monitor._inactive_since)

    def test_sustained_detection_loss_ends_call_after_grace_period(self):
        monitor = ProcessMonitor(call_end_grace_seconds=15)
        with patch.object(monitor, "is_call_active", return_value=False), \
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

    def test_zoom_call_requires_media_socket_and_call_window(self):
        monitor = ProcessMonitor()

        def window_for(platform):
            return "Quarterly Planning" if platform is ZOOM else None

        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("zoom.us", 8801)],
        ), patch.object(
            monitor, "_active_call_window_for_platform", side_effect=window_for
        ):
            self.assertTrue(monitor.is_call_active())
            self.assertIs(monitor.current_platform, ZOOM)
            self.assertEqual(monitor.current_call_title, "Quarterly Planning")
            self.assertIn("Zoom call window", monitor._last_detection_reason)

    def test_teams_call_requires_media_socket_and_call_window(self):
        monitor = ProcessMonitor()

        def window_for(platform):
            return "Customer Review | Microsoft Teams" if platform is TEAMS else None

        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("MSTeams", 3480)],
        ), patch.object(
            monitor, "_active_call_window_for_platform", side_effect=window_for
        ):
            self.assertTrue(monitor.is_call_active())
            self.assertIs(monitor.current_platform, TEAMS)
            self.assertEqual(monitor.default_call_title, "Microsoft Teams Session")

    def test_zoom_window_without_zoom_media_port_is_not_a_call(self):
        monitor = ProcessMonitor()

        def window_for(platform):
            return "Open Zoom Chat" if platform is ZOOM else None

        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("zoom.us", 443)],
        ), patch.object(
            monitor, "_active_call_window_for_platform", side_effect=window_for
        ):
            self.assertFalse(monitor.is_call_active())

    def test_core_audio_detects_tcp_only_zoom_call(self):
        audio = _AudioMonitor(AudioActivity(available=True, input_pids=(321,)))
        monitor = ProcessMonitor(audio_monitor=audio)

        def window_for(platform):
            return "TCP Customer Call" if platform is ZOOM else None

        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("zoom.us", 443, pid=321)],
        ), patch.object(
            monitor, "_active_call_window_for_platform", side_effect=window_for
        ):
            self.assertTrue(monitor.is_call_active())
            self.assertIs(monitor.current_platform, ZOOM)
            self.assertIn("Core Audio input", monitor._last_detection_reason)
            self.assertIn([321], audio.requested_pids)

    def test_core_audio_detects_teams_call_on_dynamic_udp_port(self):
        audio = _AudioMonitor(AudioActivity(available=True, output_pids=(654,)))
        monitor = ProcessMonitor(audio_monitor=audio)

        def window_for(platform):
            return "Direct Call | Microsoft Teams" if platform is TEAMS else None

        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("MSTeams", 55000, pid=654)],
        ), patch.object(
            monitor, "_active_call_window_for_platform", side_effect=window_for
        ):
            self.assertTrue(monitor.is_call_active())
            self.assertIs(monitor.current_platform, TEAMS)
            self.assertIn("Core Audio output", monitor._last_detection_reason)

    def test_core_audio_without_call_window_does_not_trigger(self):
        audio = _AudioMonitor(AudioActivity(available=True, input_pids=(321,)))
        monitor = ProcessMonitor(audio_monitor=audio)
        with patch(
            "webex_obs.process_monitor.psutil.process_iter",
            return_value=[_Process("zoom.us", 443, pid=321)],
        ), patch.object(
            monitor, "_active_call_window_for_platform", return_value=None
        ):
            self.assertFalse(monitor.is_call_active())

    def test_declined_zoom_call_does_not_suppress_teams_call(self):
        monitor = ProcessMonitor()
        monitor.current_platform = ZOOM
        monitor.current_call_title = "Weekly Sync"
        monitor.suppress_current_call_prompt()

        monitor.current_platform = TEAMS
        monitor.current_call_title = "Weekly Sync"
        self.assertFalse(monitor.should_suppress_automatic_prompt())
        self.assertIsNone(monitor._suppressed_call_title)

    def test_webex_default_title_remains_backward_compatible(self):
        monitor = ProcessMonitor()
        monitor.current_platform = WEBEX
        self.assertEqual(monitor.default_call_title, "Webex Session")


if __name__ == "__main__":
    unittest.main()
