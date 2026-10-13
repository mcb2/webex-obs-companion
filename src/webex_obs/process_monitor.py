from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

import psutil

from .core_audio_monitor import AudioActivity, CoreAudioProcessMonitor

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class CallPlatform:
    key: str
    display_name: str
    process_names: tuple[str, ...]
    apple_process_names: tuple[str, ...]
    udp_ports: frozenset[int]
    udp_port_ranges: tuple[tuple[int, int], ...] = ()
    media_process_names: tuple[str, ...] = ()
    non_call_window_names: frozenset[str] = frozenset()
    require_window_for_output: bool = False

    def uses_udp_port(self, port: int) -> bool:
        return port in self.udp_ports or any(
            start <= port <= end for start, end in self.udp_port_ranges
        )


WEBEX = CallPlatform(
    key="webex",
    display_name="Webex",
    process_names=("webex", "ciscospark", "ciscocollabhost", "meeting center", "washost"),
    apple_process_names=("Webex",),
    udp_ports=frozenset({5004, 9000, 33434}),
    media_process_names=("ciscocollabhost", "meeting center", "washost"),
    non_call_window_names=frozenset({"webex", "webex multitasking floating window"}),
    require_window_for_output=True,
)

ZOOM = CallPlatform(
    key="zoom",
    display_name="Zoom",
    process_names=("zoom.us", "zoom workplace", "cpthost"),
    apple_process_names=("zoom.us", "Zoom Workplace"),
    udp_ports=frozenset({3478, 3479}),
    udp_port_ranges=((8801, 8810),),
    non_call_window_names=frozenset({"zoom", "zoom.us", "zoom workplace"}),
)

TEAMS = CallPlatform(
    key="teams",
    display_name="Microsoft Teams",
    process_names=("msteams", "microsoft teams", "teams helper"),
    apple_process_names=("Microsoft Teams", "MSTeams"),
    udp_ports=frozenset({3478, 3479, 3480, 3481}),
    non_call_window_names=frozenset({"microsoft teams", "msteams", "teams"}),
)

SUPPORTED_PLATFORMS = (WEBEX, ZOOM, TEAMS)


@dataclass(frozen=True)
class CallEvidence:
    # None means a required sensor failed, rather than confirmed inactivity.
    active: bool | None
    title: str | None = None
    reason: str = ""


# Backward-compatible constants used by older integrations and diagnostics.
WEBEX_RTP_PORTS = set(WEBEX.udp_ports)
WEBEX_CORE_PROCESSES = list(WEBEX.process_names)
WEBEX_MEDIA_PROCESSES = list(WEBEX.media_process_names)
NON_CALL_WINDOW_NAMES = set(WEBEX.non_call_window_names)


class ProcessMonitor:
    def __init__(
        self,
        poll_interval: float = 3.0,
        call_end_grace_seconds: float = 15.0,
        audio_monitor: CoreAudioProcessMonitor | None = None,
    ):
        self.poll_interval = poll_interval
        self.call_end_grace_seconds = call_end_grace_seconds
        self.is_in_meeting = False
        self._idle_poll_count = 0
        self._active_poll_count = 0
        self._inactive_since: float | None = None
        self._last_detection_reason = ""
        self.current_call_title: str | None = None
        self.current_platform: CallPlatform | None = None
        self.bound_platform: CallPlatform | None = None
        self._platform_activity: dict[str, bool | None] = {}
        self._candidate_platform_key: str | None = None
        self._suppressed_platforms: dict[str, float | None] = {}
        self._suppressed_call_title: str | None = None
        self._suppressed_platform_key: str | None = None
        self._suppress_untitled_call = False
        self._suppression_inactive_since: float | None = None
        self.audio_monitor = audio_monitor or CoreAudioProcessMonitor()
        self.on_strong_evidence = None
        self.on_possible_evidence = None
        self.on_idle = None
        self.on_poll = None

    @property
    def current_platform_name(self) -> str | None:
        return self.current_platform.display_name if self.current_platform else None

    @property
    def default_call_title(self) -> str:
        platform_name = self.current_platform_name or "Webex"
        return f"{platform_name} Session"

    @staticmethod
    def _remote_port(connection) -> int:
        raddr = getattr(connection, "raddr", None)
        if not raddr:
            return 0
        port = getattr(raddr, "port", None)
        if port is not None:
            return int(port)
        return int(raddr[1]) if len(raddr) > 1 else 0

    def _matching_processes(self, platform: CallPlatform) -> list:
        processes = []
        try:
            for proc in psutil.process_iter(["pid", "name", "exe"]):
                pname = (proc.info.get("name") or "").lower()
                pexe = (proc.info.get("exe") or "").lower()
                if not pname and not pexe:
                    raise OSError("Process identity unavailable")
                if any(target in pname or target in pexe for target in platform.process_names):
                    processes.append(proc)
        except (psutil.Error, OSError) as exc:
            raise OSError("Process enumeration unavailable") from exc
        return processes

    def _active_media_streams(
        self, platform: CallPlatform
    ) -> list[tuple[str, int, bool]]:
        """Return supported UDP media sockets owned by one meeting platform."""
        streams = []
        failed = False
        for proc in self._matching_processes(platform):
            try:
                pname = (proc.info.get("name") or "").lower()
                pexe = (proc.info.get("exe") or "").lower()
                for connection in proc.net_connections(kind="inet"):
                    if connection.type != socket.SOCK_DGRAM:
                        continue
                    remote_port = self._remote_port(connection)
                    if not platform.uses_udp_port(remote_port):
                        continue
                    is_media_process = any(
                        target in pname or target in pexe
                        for target in platform.media_process_names
                    )
                    streams.append((pname or pexe, remote_port, is_media_process))
            except (psutil.Error, OSError):
                failed = True
        if failed and not streams:
            raise OSError("Platform socket inspection unavailable")
        return streams

    def _audio_activity(self, platform: CallPlatform) -> AudioActivity:
        pids = []
        for proc in self._matching_processes(platform):
            pid = proc.info.get("pid", getattr(proc, "pid", None))
            if isinstance(pid, int) and pid > 0:
                pids.append(pid)
        return self.audio_monitor.activity_for_pids(pids)

    def _active_rtp_media_streams(self) -> list[tuple[str, int, bool]]:
        """Backward-compatible Webex media-stream query."""
        return self._active_media_streams(WEBEX)

    @staticmethod
    def _apple_script_process_name(process_name: str) -> str:
        return process_name.replace("\\", "\\\\").replace('"', '\\"')

    def _active_window_name(self, platform: CallPlatform) -> str | None:
        """Return a non-idle window title exposed by the platform on macOS."""
        failed = False
        for process_name in platform.apple_process_names:
            escaped_name = self._apple_script_process_name(process_name)
            apple_script = f"""
            tell application "System Events"
                if not (exists process "{escaped_name}") then return ""
                tell process "{escaped_name}"
                    set windowNames to {{}}
                    repeat with callWindow in windows
                        set end of windowNames to name of callWindow
                    end repeat
                    set AppleScript's text item delimiters to linefeed
                    return windowNames as text
                end tell
            end tell
            """
            try:
                result = subprocess.run(
                    ["osascript", "-e", apple_script],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
                if result.returncode != 0:
                    failed = True
                    continue
                if not result.stdout:
                    continue
                for window_name in result.stdout.splitlines():
                    window_name = window_name.strip()
                    if (
                        window_name
                        and window_name.casefold() not in platform.non_call_window_names
                    ):
                        return window_name
            except (OSError, subprocess.SubprocessError):
                failed = True
        if failed:
            raise OSError("Platform window inspection unavailable")
        return None

    @staticmethod
    def _looks_like_attachment_window(window_name: str | None) -> bool:
        """Return True for Webex windows that are probably attachment previews."""
        if not window_name:
            return False

        normalized = window_name.casefold().strip()
        attachment_suffixes = (
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".heic",
            ".tif",
            ".tiff",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            ".txt",
            ".rtf",
        )
        attachment_markers = (
            "attachment preview",
            "document preview",
            "file preview",
            "image preview",
        )
        return normalized.endswith(attachment_suffixes) or any(
            marker in normalized for marker in attachment_markers
        )

    def _active_call_window_name(self) -> str | None:
        """Return a lightweight non-idle Webex window title."""
        return self._active_window_name(WEBEX)

    def _active_call_window_for_platform(self, platform: CallPlatform) -> str | None:
        # Keep the Webex wrapper patchable for existing diagnostics and tests.
        if platform is WEBEX:
            return self._active_call_window_name()
        return self._active_window_name(platform)

    def get_active_call_title(self) -> str | None:
        """Return the current supported call title, when macOS exposes one."""
        platforms = (
            (self.bound_platform,) if self.bound_platform else
            (self.current_platform,) if self.current_platform else SUPPORTED_PLATFORMS
        )
        for platform in platforms:
            title, _ = self._read_sensor(
                platform, "window", lambda: self._active_call_window_for_platform(platform)
            )
            if title:
                self.current_platform = platform
                self.current_call_title = title
                return title
        return None

    def suppress_current_call_prompt(self, title: str | None = None) -> None:
        """Suppress future automatic prompts for the currently declined call."""
        call_title = title or self.current_call_title or self.get_active_call_title()
        self._suppressed_call_title = call_title
        self._suppressed_platform_key = (
            self.current_platform.key if self.current_platform else None
        )
        self._suppress_untitled_call = call_title is None
        self._suppression_inactive_since = None
        self._suppressed_platforms[self._suppressed_platform_key or ""] = None

    def _clear_prompt_suppression(self) -> None:
        self._suppressed_call_title = None
        self._suppressed_platform_key = None
        self._suppress_untitled_call = False
        self._suppression_inactive_since = None
        self._suppressed_platforms.clear()

    def _track_prompt_suppression(self, call_active: bool | None) -> None:
        """Only confirmed inactivity on the declined platform rearms its prompt."""
        for key, inactive_since in list(self._suppressed_platforms.items()):
            active = self._platform_activity.get(key, call_active)
            if self.bound_platform and key != self.bound_platform.key:
                active = None  # This platform was not probed during capture.
            if active is not False:
                self._suppressed_platforms[key] = None
                if key == (self._suppressed_platform_key or ""):
                    self._suppression_inactive_since = None
                continue
            now = time.monotonic()
            inactive_since = now if inactive_since is None else inactive_since
            self._suppressed_platforms[key] = inactive_since
            if key == (self._suppressed_platform_key or ""):
                self._suppression_inactive_since = inactive_since
            if now - inactive_since >= self.call_end_grace_seconds:
                del self._suppressed_platforms[key]
                logger.info("Declined %s call has ended; automatic prompts rearmed.", key or "meeting")
                if key == (self._suppressed_platform_key or ""):
                    self._suppressed_call_title = None
                    self._suppressed_platform_key = None
                    self._suppress_untitled_call = False
                    self._suppression_inactive_since = None

    def should_suppress_automatic_prompt(self) -> bool:
        """Title changes and overlapping platforms do not undo a decline."""
        key = self.current_platform.key if self.current_platform else ""
        return key in self._suppressed_platforms or "" in self._suppressed_platforms

    @staticmethod
    def _read_sensor(
        platform: CallPlatform, sensor: str, read: Callable[[], T]
    ) -> tuple[T | None, bool]:
        try:
            return read(), True
        except (OSError, psutil.Error, subprocess.SubprocessError) as exc:
            logger.warning("%s %s sensor failed: %s", platform.display_name, sensor, exc)
            return None, False

    def _call_evidence(self, platform: CallPlatform) -> CallEvidence:
        streams, streams_ok = self._read_sensor(
            platform, "sockets", lambda: self._active_media_streams(platform)
        )
        audio, _ = self._read_sensor(
            platform, "audio", lambda: self._audio_activity(platform)
        )
        # Preparation precedes the potentially slow accessibility probe; it
        # never grants permission to start capture or bypass consent.
        if streams and audio and (audio.active or not audio.available) and self.on_possible_evidence:
            self.on_possible_evidence()
        if streams and audio and audio.input_pids and self.on_strong_evidence and not self.is_in_meeting:
            self.on_strong_evidence()
        window, window_ok = self._read_sensor(
            platform, "window", lambda: self._active_call_window_for_platform(platform)
        )
        if window and self.on_possible_evidence:
            self.on_possible_evidence()
        if platform is WEBEX and self._looks_like_attachment_window(window):
            return CallEvidence(False)

        # All clients support audio + window independently of transport.
        # Known UDP can replace the window, except for Webex output-only calls
        # (attachment helpers can own both output audio and UDP sockets).
        if audio and audio.available:
            if audio.active and (window or (streams and (
                audio.input_pids or not platform.require_window_for_output
            ))):
                direction = "input" if audio.input_pids else "output"
                reason = f"{platform.display_name} active Core Audio {direction}"
                if platform is WEBEX and not audio.input_pids:
                    reason = "Webex output-only Core Audio"
                if streams:
                    reason += " with known UDP media"
                if window:
                    reason += f" and call window '{window}'"
                return CallEvidence(True, window, reason)
            if not audio.active and not audio.failed_pids:
                return CallEvidence(False)
            if not window_ok or (not streams_ok and (
                audio.input_pids or audio.failed_pids or not platform.require_window_for_output
            )):
                return CallEvidence(None)
            if audio.failed_pids and (window or streams):
                return CallEvidence(None)
            return CallEvidence(False)

        if audio and not audio.available:
            # Unsupported API is a deliberate compatibility mode. Runtime
            # failures of a supported API must not enable this fallback.
            if window and streams:
                process, port, _ = streams[0]
                return CallEvidence(True, window, (
                    f"{platform.display_name} call window '{window}' plus "
                    f"process '{process}' using UDP port {port}"
                ))
            if (window_ok and not window) or (streams_ok and not streams):
                return CallEvidence(False)
            return CallEvidence(None)

        # Enumeration/audio failures cannot establish inactivity unless all
        # possible corroboration was successfully read and is absent.
        if window_ok and not window and streams_ok and not streams:
            return CallEvidence(False)
        return CallEvidence(None)

    def bind_current_platform(self) -> None:
        """Pin automatic capture and title enrichment until verified completion."""
        self.bound_platform = self.current_platform

    def release_platform(self) -> None:
        self.bound_platform = None

    def is_call_active(self) -> bool | None:
        """Return active, inactive, or unknown for the bound call (or candidates)."""
        platforms = (self.bound_platform,) if self.bound_platform else SUPPORTED_PLATFORMS
        evidence = [(platform, self._call_evidence(platform)) for platform in platforms]
        self._platform_activity = {platform.key: row.active for platform, row in evidence}
        positives = [(platform, row) for platform, row in evidence if row.active is True]
        if positives:
            # A declined app must not starve another platform's prompt. Retain
            # each decline so returning to it cannot silently rearm recording.
            platform, row = next(
                ((platform, row) for platform, row in positives
                 if platform.key not in self._suppressed_platforms), positives[0]
            )
            if self.current_platform is not platform:
                self.current_call_title = None
            self.current_platform = platform
            if row.title:
                self.current_call_title = row.title
            self._last_detection_reason = row.reason
            return True
        self._last_detection_reason = ""
        return None if any(row.active is None for _, row in evidence) else False

    def diagnostic_snapshot(self) -> list[dict[str, object]]:
        """Return call-detection evidence for troubleshooting on the host Mac."""
        snapshot = []
        for platform in SUPPORTED_PLATFORMS:
            processes, processes_ok = self._read_sensor(
                platform, "processes", lambda: self._matching_processes(platform)
            )
            process_rows = []
            for proc in processes or []:
                pid = proc.info.get("pid", getattr(proc, "pid", None))
                name = proc.info.get("name") or ""
                process_rows.append(f"{name} ({pid or '?'})")
            streams, streams_ok = self._read_sensor(
                platform, "sockets", lambda: self._active_media_streams(platform)
            )
            activity, audio_ok = self._read_sensor(
                platform, "audio", lambda: self._audio_activity(platform)
            )
            window, window_ok = self._read_sensor(
                platform, "window", lambda: self._active_call_window_for_platform(platform)
            )
            snapshot.append(
                {
                    "platform": platform.display_name,
                    "processes": process_rows,
                    "processes_available": processes_ok,
                    "window": window,
                    "window_available": window_ok,
                    "udp_available": streams_ok,
                    "udp_streams": [
                        f"{process}:{port}" for process, port, _ in streams or []
                    ],
                    "audio_available": activity.available if activity else self.audio_monitor.available,
                    "audio_failed": not audio_ok or bool(activity and activity.failed_pids),
                    "audio_input_pids": list(activity.input_pids) if activity else [],
                    "audio_output_pids": list(activity.output_pids) if activity else [],
                }
            )
        return snapshot

    def is_webex_running(self) -> bool | None:
        """Backward-compatible alias for the multi-platform active-call check."""
        return self.is_call_active()

    def has_call_ended(self) -> bool:
        """Return True only after call evidence is absent for the full grace period."""
        active = self.is_call_active()
        self._track_prompt_suppression(active)
        return self._track_call_end(active)

    def _track_call_end(self, active: bool | None) -> bool:
        if active is not False:
            self._inactive_since = None
            return False

        platform_name = self.current_platform_name or "Meeting"
        now = time.monotonic()
        if self._inactive_since is None:
            self._inactive_since = now
            logger.warning(
                "%s call evidence temporarily missing; keeping recording active "
                "for up to %.1f seconds.",
                platform_name,
                self.call_end_grace_seconds,
            )
            return self.call_end_grace_seconds == 0

        if now - self._inactive_since < self.call_end_grace_seconds:
            return False

        self._inactive_since = None
        logger.info("%s call / meeting media stream has ended.", platform_name)
        return True

    def wait_for_state_change(self, wake_event: threading.Event | None = None) -> bool:
        if wake_event is not None and wake_event.is_set():
            return True
        if not self.is_in_meeting:
            # Do not let identity from the previous call leak into a new session.
            self.current_call_title = None
            self.current_platform = None
            self._active_poll_count = 0
            self._candidate_platform_key = None
        while True:
            if self.on_poll:
                self.on_poll()
            if wake_event is not None and wake_event.is_set():
                return True
            running = self.is_call_active()
            if wake_event is not None and wake_event.is_set():
                return True
            self._track_prompt_suppression(running)
            if running is False and not self.is_in_meeting and self.on_idle:
                self.on_idle()
            if running is True and not self.is_in_meeting:
                key = self.current_platform.key if self.current_platform else None
                if key != self._candidate_platform_key:
                    self._active_poll_count = 0
                self._candidate_platform_key = key
                self._active_poll_count += 1
                if self._active_poll_count >= 2:
                    self.is_in_meeting = True
                    self._active_poll_count = 0
                    self._idle_poll_count = 0
                    self._inactive_since = None
                    logger.info(
                        "Detected active %s call / meeting session: %s.",
                        self.current_platform_name or "supported",
                        self._last_detection_reason,
                    )
                    return True
            elif self.is_in_meeting:
                self._active_poll_count = 0
                if self._track_call_end(running):
                    self.is_in_meeting = False
                    return False
            else:
                self._idle_poll_count = 0
                self._active_poll_count = 0
                self._candidate_platform_key = None
                if running is None:
                    self._inactive_since = None

            if wake_event is None:
                time.sleep(self.poll_interval)
            else:
                wake_event.wait(self.poll_interval)
