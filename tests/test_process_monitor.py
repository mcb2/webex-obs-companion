import socket
import types
import unittest
import threading

import pytest
from unittest.mock import patch


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


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    # UDP fallback tests explicitly model a host without the per-process API.
    monkeypatch.setattr("webex_obs.process_monitor.CoreAudioProcessMonitor",
                        lambda: _AudioMonitor(AudioActivity(available=False)))
    monkeypatch.setattr("webex_obs.process_monitor.psutil.process_iter", lambda attrs: [])
    monkeypatch.setattr("webex_obs.process_monitor.subprocess.run",
                        lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout="", stderr=""))


class ProcessMonitorTests(unittest.TestCase):
    def test_strong_evidence_prepares_obs_before_window_validation(self):
        audio = _AudioMonitor(AudioActivity(available=True, input_pids=(123,)))
        monitor = ProcessMonitor(audio_monitor=audio)
        events = []
        monitor.on_strong_evidence = lambda: events.append("prepare")
        def window():
            events.append("window")
            return "quarterly-results.pdf"
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("Webex", 5004)]), \
             patch.object(monitor, "_active_call_window_name", side_effect=window), \
             patch.object(monitor, "_active_window_name", return_value=None):
            # Warming OBS must not bypass the existing attachment rejection.
            self.assertFalse(monitor.is_call_active())
        self.assertEqual(events, ["prepare", "window"])

    def test_idle_hook_is_not_called_for_unconfirmed_positive_evidence(self):
        monitor = ProcessMonitor()
        from unittest.mock import Mock
        monitor.on_idle = Mock()
        with patch.object(monitor, "is_call_active", return_value=True), \
             patch("webex_obs.process_monitor.time.sleep"):
            self.assertTrue(monitor.wait_for_state_change())
        monitor.on_idle.assert_not_called()

    def test_manual_start_wakes_detection_without_call_evidence(self):
        monitor = ProcessMonitor()
        requested = threading.Event()
        requested.set()
        with patch.object(monitor, "is_call_active") as detect:
            self.assertTrue(monitor.wait_for_state_change(requested))
        detect.assert_not_called()

    def test_idle_webex_socket_is_not_enough(self):
        monitor = ProcessMonitor()
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("Webex", 5004)]), \
             patch.object(monitor, "_active_call_window_name", return_value=None):
            self.assertFalse(monitor.is_webex_running())

    def test_general_webex_socket_requires_call_window(self):
        monitor = ProcessMonitor()
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("Webex", 5004)]), \
             patch.object(monitor, "_active_call_window_name", return_value="Synthetic Caller"):
            self.assertTrue(monitor.is_webex_running())
            self.assertIn("call window 'Synthetic Caller'", monitor._last_detection_reason)
            self.assertEqual(monitor.current_call_title, "Synthetic Caller")

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
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("Webex", 5004)]), \
             patch("webex_obs.process_monitor.subprocess.run", return_value=result):
            self.assertIsNone(monitor._active_call_window_name())

    def test_lightweight_webex_window_probe_returns_meeting_title(self):
        monitor = ProcessMonitor()
        result = types.SimpleNamespace(
            returncode=0,
            stdout="Weekly Sync\n",
            stderr="",
        )
        with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[_Process("Webex", 5004)]), \
             patch("webex_obs.process_monitor.subprocess.run", return_value=result):
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

    def test_window_title_change_does_not_clear_declined_call_suppression(self):
        monitor = ProcessMonitor()
        monitor.current_platform = WEBEX
        monitor.current_call_title = "Weekly Sync"
        monitor.suppress_current_call_prompt()
        monitor.current_call_title = "Shared screen - Weekly Sync"
        self.assertTrue(monitor.should_suppress_automatic_prompt())
        self.assertEqual(monitor._suppressed_call_title, "Weekly Sync")

    def test_untitled_declined_call_remains_suppressed_when_title_appears(self):
        monitor = ProcessMonitor()
        with patch.object(monitor, "_active_call_window_name", return_value=None):
            monitor.suppress_current_call_prompt()
            self.assertTrue(monitor.should_suppress_automatic_prompt())
        with patch.object(monitor, "_active_call_window_name", return_value="New Meeting"):
            self.assertTrue(monitor.should_suppress_automatic_prompt())

    def test_declined_call_suppression_clears_after_end_grace_period(self):
        monitor = ProcessMonitor(call_end_grace_seconds=15)
        monitor.current_platform = WEBEX
        monitor.current_call_title = "Weekly Sync"
        monitor.suppress_current_call_prompt()

        with patch(
            "webex_obs.process_monitor.time.monotonic",
            side_effect=[100, 114.9, 115],
        ):
            monitor._track_prompt_suppression(False)
            self.assertTrue(monitor.should_suppress_automatic_prompt())
            monitor._track_prompt_suppression(False)
            self.assertTrue(monitor.should_suppress_automatic_prompt())
            monitor._track_prompt_suppression(False)

        self.assertFalse(monitor.should_suppress_automatic_prompt())
        self.assertIsNone(monitor._suppressed_call_title)

    def test_active_call_resets_decline_end_grace_timer(self):
        monitor = ProcessMonitor(call_end_grace_seconds=15)
        monitor.current_platform = WEBEX
        monitor.current_call_title = "Weekly Sync"
        monitor.suppress_current_call_prompt()

        with patch("webex_obs.process_monitor.time.monotonic", return_value=100):
            monitor._track_prompt_suppression(False)
        monitor._track_prompt_suppression(True)

        self.assertIsNone(monitor._suppression_inactive_since)
        self.assertTrue(monitor.should_suppress_automatic_prompt())

    def test_wait_for_state_change_rearms_prompt_after_declined_call_ends(self):
        monitor = ProcessMonitor(poll_interval=0, call_end_grace_seconds=15)
        monitor.current_platform = WEBEX
        monitor.current_call_title = "Weekly Sync"
        monitor.suppress_current_call_prompt()
        states = iter([False, False, True, True])

        with patch.object(
            monitor, "is_call_active", side_effect=lambda: next(states)
        ), patch(
            "webex_obs.process_monitor.time.monotonic", side_effect=[100, 115]
        ), patch(
            "webex_obs.process_monitor.time.sleep"
        ):
            self.assertTrue(monitor.wait_for_state_change())

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
        monitor.current_platform = ZOOM
        self.assertTrue(monitor.should_suppress_automatic_prompt())

    def test_webex_default_title_remains_backward_compatible(self):
        monitor = ProcessMonitor()
        monitor.current_platform = WEBEX
        self.assertEqual(monitor.default_call_title, "Webex Session")


if __name__ == "__main__":
    unittest.main()


def test_inaccessible_process_does_not_hide_a_detectable_call():
    import psutil

    inaccessible = _Process("Webex", 5004, pid=100)
    accessible = _Process("Webex", 5004, pid=200)
    monitor = ProcessMonitor()
    with patch.object(inaccessible, "net_connections", side_effect=psutil.AccessDenied(pid=100)), \
         patch("webex_obs.process_monitor.psutil.process_iter", return_value=[inaccessible, accessible]), \
         patch.object(monitor, "_active_call_window_name", return_value="Synthetic meeting"):
        assert monitor.is_webex_running()
        assert monitor.current_call_title == "Synthetic meeting"


@pytest.mark.parametrize("platform,name", [(WEBEX, "Webex"), (ZOOM, "zoom.us"), (TEAMS, "MSTeams")])
@pytest.mark.parametrize("transport,port", [(socket.SOCK_STREAM, 443), (socket.SOCK_DGRAM, 55000)])
@pytest.mark.parametrize("direction", ["input", "output"])
def test_audio_and_window_detect_all_platforms_without_known_udp(platform, name, transport, port, direction):
    # Input state means an open stream, including a muted microphone; no sound
    # level or microphone permission is required for the output-only case.
    activity = AudioActivity(available=True, **{f"{direction}_pids": (123,)})
    monitor = ProcessMonitor(audio_monitor=_AudioMonitor(activity))
    proc = _Process(name, port)
    connection = _Connection(port)
    connection.type = transport
    with patch.object(proc, "net_connections", return_value=[connection]), \
         patch("webex_obs.process_monitor.psutil.process_iter", return_value=[proc]), \
         patch.object(monitor, "_active_call_window_for_platform", side_effect=lambda p: "Synthetic call" if p is platform else None):
        assert monitor.is_call_active() is True
        assert monitor.current_platform is platform
        assert monitor.current_call_title == "Synthetic call"
        assert not monitor._active_media_streams(platform)


@pytest.mark.parametrize("platform", [WEBEX, ZOOM, TEAMS])
@pytest.mark.parametrize("sensor", ["audio", "window", "sockets"])
def test_failed_sensor_is_unknown_and_does_not_end_or_rearm_call(platform, sensor):
    monitor = ProcessMonitor(call_end_grace_seconds=0)
    monitor.current_platform = platform
    monitor.current_call_title = "Synthetic call"
    monitor.bind_current_platform()
    monitor.suppress_current_call_prompt()
    # No positive corroboration can bypass the failed sensor in these cases.
    audio = AudioActivity(available=True, input_pids=(123,))
    if sensor == "audio":
        audio = AudioActivity(available=True, failed_pids=(123,))
    elif sensor == "sockets":
        audio = AudioActivity(available=False)
    with patch.object(monitor, "_audio_activity", return_value=audio), \
         patch.object(monitor, "_active_media_streams", return_value=[],
                      side_effect=OSError("synthetic denied") if sensor == "sockets" else None), \
         patch.object(monitor, "_active_call_window_for_platform", return_value="Synthetic call",
                      side_effect=OSError("synthetic denied") if sensor == "window" else None):
        assert monitor.is_call_active() is None
        monitor._inactive_since = 100
        assert not monitor.has_call_ended()
        assert monitor._inactive_since is None
        assert monitor.should_suppress_automatic_prompt()


@pytest.mark.parametrize("platform", [WEBEX, ZOOM, TEAMS])
def test_supported_but_failed_audio_does_not_enable_udp_fallback(platform):
    monitor = ProcessMonitor(audio_monitor=_AudioMonitor(AudioActivity(available=True, failed_pids=(123,))))
    monitor.current_platform = platform
    monitor.bind_current_platform()
    with patch.object(monitor, "_active_media_streams", return_value=[("synthetic", 3478, False)]), \
         patch.object(monitor, "_active_call_window_for_platform", return_value="Synthetic call"):
        assert monitor.is_call_active() is None
        monitor.audio_monitor.activity = AudioActivity(available=True)
        assert monitor.is_call_active() is False
        monitor.audio_monitor.activity = AudioActivity(available=False)
        assert monitor.is_call_active() is True


@pytest.mark.parametrize("sensor", ["window-exit", "window-timeout", "processes", "sockets"])
def test_real_probe_errors_are_reported_in_diagnostics(sensor):
    import subprocess
    import psutil

    monitor = ProcessMonitor()
    proc = _Process("Webex", 5004)
    with patch("webex_obs.process_monitor.psutil.process_iter", return_value=[proc]) as processes, \
         patch("webex_obs.process_monitor.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as windows, \
         patch.object(proc, "net_connections", return_value=[]) as sockets:
        if sensor == "window-exit":
            windows.return_value.returncode = 1
        elif sensor == "window-timeout":
            windows.side_effect = subprocess.TimeoutExpired("osascript", 2)
        elif sensor == "processes":
            processes.side_effect = psutil.AccessDenied()
        else:
            sockets.side_effect = psutil.AccessDenied(pid=123)
        row = monitor.diagnostic_snapshot()[0]
        field = {"window-exit": "window_available", "window-timeout": "window_available",
                 "processes": "processes_available", "sockets": "udp_available"}[sensor]
        assert row[field] is False
        if sensor == "processes":
            assert row["audio_failed"]


@pytest.mark.parametrize("direction", ["input", "output"])
def test_positive_audio_and_window_survive_socket_failure(direction):
    monitor = ProcessMonitor(audio_monitor=_AudioMonitor(AudioActivity(available=True, **{f"{direction}_pids": (123,)})))
    monitor.current_platform = WEBEX
    monitor.bind_current_platform()
    with patch.object(monitor, "_active_media_streams", side_effect=OSError("synthetic denied")), \
         patch.object(monitor, "_active_call_window_for_platform", return_value="Synthetic call"):
        assert monitor.is_call_active() is True


def test_unknown_poll_breaks_start_confirmation_and_skips_idle_maintenance():
    from unittest.mock import Mock
    monitor = ProcessMonitor(poll_interval=0)
    monitor.on_idle = Mock()
    with patch.object(monitor, "is_call_active", side_effect=[True, None, True, True]) as probe, \
         patch("webex_obs.process_monitor.time.sleep"):
        assert monitor.wait_for_state_change()
    assert probe.call_count == 4
    monitor.on_idle.assert_not_called()


def test_start_confirmation_cannot_combine_different_platforms():
    monitor = ProcessMonitor(poll_interval=0)
    platforms = iter([WEBEX, ZOOM, ZOOM])

    def probe():
        monitor.current_platform = next(platforms)
        return True

    with patch.object(monitor, "is_call_active", side_effect=probe) as detect, \
         patch("webex_obs.process_monitor.time.sleep"):
        assert monitor.wait_for_state_change()
    assert detect.call_count == 3
    assert monitor.current_platform is ZOOM


@pytest.mark.parametrize("origin,other", [(ZOOM, WEBEX), (WEBEX, TEAMS), (TEAMS, ZOOM)])
def test_bound_call_ends_while_another_platform_remains_active_without_relabeling(origin, other):
    monitor = ProcessMonitor(call_end_grace_seconds=15)
    monitor.current_platform = origin
    monitor.current_call_title = "Origin synthetic call"
    monitor.bind_current_platform()
    ended = False

    def audio(platform):
        active = platform is other or (platform is origin and not ended)
        return AudioActivity(available=True, input_pids=(123,) if active else ())

    def window(platform):
        if platform is origin:
            return "Origin synthetic call"
        return "Other synthetic call" if platform is other else None

    with patch.object(monitor, "_active_media_streams", return_value=[]), \
         patch.object(monitor, "_audio_activity", side_effect=audio), \
         patch.object(monitor, "_active_call_window_for_platform", side_effect=window), \
         patch("webex_obs.process_monitor.time.monotonic", side_effect=[100, 114, 115]):
        assert monitor.is_call_active() is True
        assert monitor.get_active_call_title() == "Origin synthetic call"
        ended = True
        assert not monitor.has_call_ended()
        assert not monitor.has_call_ended()
        assert monitor.has_call_ended()
        assert monitor.current_platform is origin
        assert monitor.current_call_title == "Origin synthetic call"
        monitor.release_platform()
        assert monitor.is_call_active() is True
        assert monitor.current_platform is other
        assert monitor.current_call_title == "Other synthetic call"


def test_unknown_read_restarts_entire_end_grace_period():
    monitor = ProcessMonitor(call_end_grace_seconds=15)
    with patch.object(monitor, "is_call_active", side_effect=[False, None, False, False, False]), \
         patch("webex_obs.process_monitor.time.monotonic", side_effect=[100, 200, 214, 215]):
        assert not monitor.has_call_ended()
        assert not monitor.has_call_ended()
        assert not monitor.has_call_ended()
        assert not monitor.has_call_ended()
        assert monitor.has_call_ended()


def test_overlapping_declines_are_preserved_and_rearmed_per_platform():
    monitor = ProcessMonitor(call_end_grace_seconds=15)
    for platform in (WEBEX, ZOOM):
        monitor.current_platform = platform
        monitor.current_call_title = "Synthetic call"
        monitor.suppress_current_call_prompt()
    monitor._platform_activity = {"webex": False, "zoom": True}
    with patch("webex_obs.process_monitor.time.monotonic", side_effect=[100, 115]):
        monitor._track_prompt_suppression(True)
        monitor._track_prompt_suppression(True)
    assert monitor.should_suppress_automatic_prompt()  # Zoom is still active.
    monitor.current_platform = WEBEX
    assert not monitor.should_suppress_automatic_prompt()


def test_declined_platform_does_not_starve_an_overlapping_call():
    from webex_obs.process_monitor import CallEvidence
    monitor = ProcessMonitor()
    monitor.current_platform = WEBEX
    monitor.current_call_title = "Synthetic Webex call"
    monitor.suppress_current_call_prompt()
    with patch.object(monitor, "_call_evidence", side_effect=lambda p: CallEvidence(p is not TEAMS, f"Synthetic {p.key} call")):
        assert monitor.is_call_active() is True
        assert monitor.current_platform is ZOOM
        assert not monitor.should_suppress_automatic_prompt()
        monitor.current_platform = WEBEX
        assert monitor.should_suppress_automatic_prompt()


def test_state_change_wait_resets_grace_after_positive_poll():
    monitor = ProcessMonitor(poll_interval=0, call_end_grace_seconds=15)
    monitor.is_in_meeting = True
    with patch.object(monitor, "is_call_active", side_effect=[False, True, False, False, False]) as probe, \
         patch("webex_obs.process_monitor.time.monotonic", side_effect=[100, 200, 214, 215]), \
         patch("webex_obs.process_monitor.time.sleep"):
        assert monitor.wait_for_state_change() is False
    assert probe.call_count == 5


def test_failed_audio_and_sockets_cannot_confirm_absence_without_window():
    monitor = ProcessMonitor(audio_monitor=_AudioMonitor(AudioActivity(available=True, failed_pids=(123,))))
    monitor.current_platform = WEBEX
    monitor.bind_current_platform()
    with patch.object(monitor, "_active_media_streams", side_effect=OSError("synthetic denied")), \
         patch.object(monitor, "_active_call_window_for_platform", return_value=None):
        assert monitor.is_call_active() is None


@pytest.mark.parametrize("platform", [WEBEX, ZOOM, TEAMS])
@pytest.mark.parametrize("direction", ["input", "output"])
def test_known_udp_without_window_obeys_platform_audio_requirements(platform, direction):
    activity = AudioActivity(available=True, **{f"{direction}_pids": (123,)})
    monitor = ProcessMonitor(audio_monitor=_AudioMonitor(activity))
    monitor.current_platform = platform
    monitor.bind_current_platform()
    with patch.object(monitor, "_active_media_streams", return_value=[("synthetic", next(iter(platform.udp_ports)), False)]), \
         patch.object(monitor, "_active_call_window_for_platform", return_value=None):
        assert monitor.is_call_active() is (platform is not WEBEX or direction == "input")


def test_unknown_read_restarts_decline_grace_period():
    monitor = ProcessMonitor(call_end_grace_seconds=15)
    monitor.current_platform = WEBEX
    monitor.current_call_title = "Synthetic call"
    monitor.suppress_current_call_prompt()
    with patch("webex_obs.process_monitor.time.monotonic", side_effect=[100, 200, 214, 215]):
        monitor._track_prompt_suppression(False)
        monitor._track_prompt_suppression(None)
        monitor._track_prompt_suppression(False)
        monitor._track_prompt_suppression(False)
        assert monitor.should_suppress_automatic_prompt()
        monitor._track_prompt_suppression(False)
    assert not monitor.should_suppress_automatic_prompt()


@pytest.mark.parametrize("platform", [WEBEX, ZOOM, TEAMS])
def test_absent_platform_skips_applescript_in_detection_and_diagnostics(platform):
    monitor = ProcessMonitor()
    monitor.current_platform = platform
    monitor.bind_current_platform()
    with patch("webex_obs.process_monitor.subprocess.run") as windows:
        assert monitor.is_call_active() is False
        row = next(row for row in monitor.diagnostic_snapshot()
                   if row["platform"] == platform.display_name)
    windows.assert_not_called()
    assert row["processes_available"] is True
    assert row["window_available"] is True
    assert row["window"] is None


def test_failed_process_enumeration_does_not_establish_window_absence():
    monitor = ProcessMonitor()
    monitor.current_platform = ZOOM
    monitor.bind_current_platform()
    monitor.suppress_current_call_prompt()
    with patch.object(monitor, "_matching_processes", side_effect=OSError("Process enumeration unavailable")), \
         patch("webex_obs.process_monitor.subprocess.run") as windows:
        assert monitor.is_call_active() is None
        assert not monitor.has_call_ended()
        assert monitor.should_suppress_automatic_prompt()
        row = monitor.diagnostic_snapshot()[1]
    windows.assert_not_called()
    assert row["window_available"] is False


@pytest.mark.parametrize("failure, expected", [
    ("timeout", "timed out after 2 seconds"),
    ("exit", "osascript exited 1 (AppleScript error -1743)"),
    ("launch", "osascript failed (OSError)"),
])
def test_window_failure_details_exclude_private_output(failure, expected, caplog):
    import subprocess

    monitor = ProcessMonitor()
    private_title = "Synthetic private window title"
    result = types.SimpleNamespace(returncode=1, stdout=private_title,
                                   stderr=f"{private_title}: access denied (-1743)\n")
    error = None
    if failure == "timeout":
        error = subprocess.TimeoutExpired("osascript", 2, output=private_title, stderr=private_title)
    elif failure == "launch":
        error = OSError(private_title)
    with patch.object(monitor, "_matching_processes", return_value=[_Process("zoom.us", 8801)]), \
         patch("webex_obs.process_monitor.subprocess.run", return_value=result, side_effect=error):
        value, available = monitor._read_sensor(ZOOM, "window", lambda: monitor._active_window_name(ZOOM))
    assert value is None
    assert available is False
    assert expected in caplog.text
    assert private_title not in caplog.text


def test_sensor_warnings_are_throttled_independently_and_reset_after_recovery(caplog):
    monitor = ProcessMonitor()

    def fail():
        raise OSError("Synthetic sensor failure")

    with patch("webex_obs.process_monitor.time.monotonic", return_value=100) as now:
        assert monitor._read_sensor(ZOOM, "window", fail) == (None, False)
        now.return_value = 159
        assert monitor._read_sensor(ZOOM, "window", fail) == (None, False)
        assert len(caplog.records) == 1
        assert monitor._read_sensor(TEAMS, "window", fail) == (None, False)
        assert monitor._read_sensor(ZOOM, "sockets", fail) == (None, False)
        assert len(caplog.records) == 3
        now.return_value = 160
        assert monitor._read_sensor(ZOOM, "window", fail) == (None, False)
        assert len(caplog.records) == 4
        assert monitor._read_sensor(ZOOM, "window", lambda: None) == (None, True)
        assert monitor._read_sensor(ZOOM, "window", fail) == (None, False)
        assert len(caplog.records) == 5


@pytest.mark.parametrize("platform", [WEBEX, ZOOM, TEAMS])
@pytest.mark.parametrize("same_title", [True, False])
def test_quick_next_call_rearms_canceled_recording(platform, same_title):
    monitor = ProcessMonitor(poll_interval=0, call_end_grace_seconds=15)
    monitor.current_platform = platform
    monitor.current_call_title = "Synthetic original"
    monitor.suppress_current_call_prompt()
    next_title = "Synthetic original" if same_title else "Synthetic next"
    states = iter(["idle", "active", "active"])
    state = "active"

    def poll():
        nonlocal state
        state = next(states)

    monitor.on_poll = poll
    with patch.object(monitor, "_audio_activity", side_effect=lambda p: AudioActivity(
        available=True, input_pids=(123,) if p is platform and state == "active" else ()
    )), patch.object(monitor, "_active_media_streams", return_value=[]), \
         patch.object(monitor, "_active_call_window_for_platform", side_effect=lambda p:
                      next_title if p is platform and state == "active" else None), \
         patch("webex_obs.process_monitor.time.monotonic", return_value=100), \
         patch("webex_obs.process_monitor.time.sleep"):
        assert monitor.wait_for_state_change()
    assert monitor.current_platform is platform
    assert monitor.current_call_title == next_title
    assert not monitor.should_suppress_automatic_prompt()


@pytest.mark.parametrize("platform", [WEBEX, ZOOM, TEAMS])
def test_quick_next_call_ends_bound_session_before_grace(platform):
    monitor = ProcessMonitor(call_end_grace_seconds=15)
    monitor.current_platform = platform
    monitor.current_call_title = "Synthetic call"
    monitor.bind_current_platform()
    audio = _AudioMonitor(AudioActivity(available=True))
    monitor.audio_monitor = audio
    with patch.object(monitor, "_audio_activity", side_effect=lambda p: audio.activity), \
         patch.object(monitor, "_active_media_streams", return_value=[]), \
         patch.object(monitor, "_active_call_window_for_platform", return_value=None) as window, \
         patch("webex_obs.process_monitor.time.monotonic", return_value=100):
        assert not monitor.has_call_ended()
        audio.activity = AudioActivity(available=True, input_pids=(123,))
        window.return_value = "Synthetic call"
        assert not monitor.has_call_ended()
        assert monitor.has_call_ended()
        monitor.release_platform()
        assert not monitor._departed_platforms
        assert not monitor._restarted_platforms


@pytest.mark.parametrize("incomplete", [
    "audio", "window", "sockets", "unsupported", "failed-audio",
    "window-retained", "sockets-retained", "audio-retained",
])
@pytest.mark.parametrize("suppressed", [True, False])
def test_partial_idle_or_sensor_failure_cannot_split_or_rearm_call(incomplete, suppressed):
    monitor = ProcessMonitor(call_end_grace_seconds=15)
    monitor.current_platform = WEBEX
    monitor.current_call_title = "Synthetic original"
    monitor.bind_current_platform()
    if suppressed:
        monitor.suppress_current_call_prompt()
    with patch.object(monitor, "_audio_activity", return_value=AudioActivity(available=True)) as audio, \
         patch.object(monitor, "_active_media_streams", return_value=[]) as streams, \
         patch.object(monitor, "_active_call_window_for_platform", return_value=None) as window, \
         patch("webex_obs.process_monitor.time.monotonic", return_value=100):
        if incomplete == "audio":
            audio.side_effect = OSError("synthetic failure")
        elif incomplete == "window":
            window.side_effect = OSError("synthetic failure")
        elif incomplete == "sockets":
            streams.side_effect = OSError("synthetic failure")
        elif incomplete == "unsupported":
            audio.return_value = AudioActivity(available=False)
        elif incomplete == "failed-audio":
            audio.return_value = AudioActivity(available=True, failed_pids=(123,))
        elif incomplete == "window-retained":
            window.return_value = "Synthetic original"
        elif incomplete == "sockets-retained":
            streams.return_value = [("synthetic", 5004, True)]
        else:
            audio.return_value = AudioActivity(available=True, input_pids=(123,))
        assert not monitor.has_call_ended()
        audio.side_effect = window.side_effect = streams.side_effect = None
        streams.return_value = []
        audio.return_value = AudioActivity(available=True, input_pids=(123,))
        window.return_value = "Synthetic different title"
        assert not monitor.has_call_ended()
        assert not monitor.has_call_ended()
        assert monitor.should_suppress_automatic_prompt() is suppressed


@pytest.mark.parametrize("interruption", ["unknown", "window-retained", "sockets-retained"])
def test_short_gap_transition_requires_uninterrupted_confirmation(interruption):
    from webex_obs.process_monitor import CallEvidence

    monitor = ProcessMonitor(call_end_grace_seconds=15)
    monitor.current_platform = WEBEX
    monitor.bind_current_platform()
    monitor.suppress_current_call_prompt("Synthetic call")
    interrupted = CallEvidence(None if interruption == "unknown" else False)
    rows = [CallEvidence(False, fully_idle=True), CallEvidence(True), interrupted,
            CallEvidence(True), CallEvidence(True)]
    with patch.object(monitor, "_call_evidence", side_effect=rows), \
         patch("webex_obs.process_monitor.time.monotonic", return_value=100):
        for _ in rows:
            assert not monitor.has_call_ended()
            assert monitor.should_suppress_automatic_prompt()


def test_quick_transition_only_rearms_its_own_platform():
    from webex_obs.process_monitor import CallEvidence

    monitor = ProcessMonitor()
    for platform in (WEBEX, ZOOM):
        monitor.current_platform = platform
        monitor.suppress_current_call_prompt("Synthetic call")
    state = CallEvidence(False, fully_idle=True)
    with patch.object(monitor, "_call_evidence", side_effect=lambda p:
                      state if p is WEBEX else CallEvidence(True)):
        monitor.is_call_active()
        monitor._track_prompt_suppression(True)
        state = CallEvidence(True, "Synthetic next")
        monitor.is_call_active()
        assert "webex" in monitor._suppressed_platforms
        monitor.is_call_active()
    assert monitor.current_platform is WEBEX
    assert not monitor.should_suppress_automatic_prompt()
    monitor.current_platform = ZOOM
    assert monitor.should_suppress_automatic_prompt()
