from __future__ import annotations

import logging
import socket
import subprocess
import time
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)


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

# Backward-compatible constants used by older integrations and diagnostics.
WEBEX_RTP_PORTS = set(WEBEX.udp_ports)
WEBEX_CORE_PROCESSES = list(WEBEX.process_names)
WEBEX_MEDIA_PROCESSES = list(WEBEX.media_process_names)
NON_CALL_WINDOW_NAMES = set(WEBEX.non_call_window_names)


class ProcessMonitor:
    def __init__(self, poll_interval: float = 3.0, call_end_grace_seconds: float = 15.0):
        self.poll_interval = poll_interval
        self.call_end_grace_seconds = call_end_grace_seconds
        self.is_in_meeting = False
        self._idle_poll_count = 0
        self._active_poll_count = 0
        self._inactive_since: float | None = None
        self._last_detection_reason = ""
        self.current_call_title: str | None = None
        self.current_platform: CallPlatform | None = None
        self._suppressed_call_title: str | None = None
        self._suppressed_platform_key: str | None = None
        self._suppress_untitled_call = False

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

    def _active_media_streams(self, platform: CallPlatform) -> list[tuple[str, int, bool]]:
        """Return supported UDP media sockets owned by one meeting platform."""
        streams = []
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                pexe = (proc.info.get("exe") or "").lower()
                if not any(
                    target in pname or target in pexe
                    for target in platform.process_names
                ):
                    continue

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
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        return streams

    def _active_rtp_media_streams(self) -> list[tuple[str, int, bool]]:
        """Backward-compatible Webex media-stream query."""
        return self._active_media_streams(WEBEX)

    @staticmethod
    def _apple_script_process_name(process_name: str) -> str:
        return process_name.replace("\\", "\\\\").replace('"', '\\"')

    def _active_window_name(self, platform: CallPlatform) -> str | None:
        """Return a non-idle window title exposed by the platform on macOS."""
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
                if result.returncode != 0 or not result.stdout:
                    continue
                for window_name in result.stdout.splitlines():
                    window_name = window_name.strip()
                    if (
                        window_name
                        and window_name.casefold() not in platform.non_call_window_names
                    ):
                        return window_name
            except Exception:
                continue
        return None

    def _active_call_window_name(self) -> str | None:
        """Backward-compatible Webex call-window query."""
        return self._active_window_name(WEBEX)

    def _active_call_window_for_platform(self, platform: CallPlatform) -> str | None:
        # Keep the Webex wrapper patchable for existing diagnostics and tests.
        if platform is WEBEX:
            return self._active_call_window_name()
        return self._active_window_name(platform)

    def get_active_call_title(self) -> str | None:
        """Return the current supported call title, when macOS exposes one."""
        platforms = (
            (self.current_platform,) if self.current_platform else SUPPORTED_PLATFORMS
        )
        for platform in platforms:
            title = self._active_call_window_for_platform(platform)
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

    def _clear_prompt_suppression(self) -> None:
        self._suppressed_call_title = None
        self._suppressed_platform_key = None
        self._suppress_untitled_call = False

    def should_suppress_automatic_prompt(self) -> bool:
        """Return True when a re-detected call matches one the user already declined."""
        if self._suppressed_call_title is None and not self._suppress_untitled_call:
            return False

        if (
            self._suppressed_platform_key
            and self.current_platform
            and self._suppressed_platform_key != self.current_platform.key
        ):
            self._clear_prompt_suppression()
            return False

        title = self.current_call_title or self.get_active_call_title()
        if self._suppressed_call_title is not None:
            if title is None or title == self._suppressed_call_title:
                return True
            self._clear_prompt_suppression()
            return False

        if title is None:
            return True
        self._clear_prompt_suppression()
        return False

    def is_call_active(self) -> bool:
        """Return True only during a supported active voice/video call."""
        for platform in SUPPORTED_PLATFORMS:
            streams = self._active_media_streams(platform)
            call_window = self._active_call_window_for_platform(platform)

            # Webex exposes call-specific media helpers, which are strong evidence.
            media_streams = [stream for stream in streams if stream[2]]
            if media_streams:
                process, port, _ = media_streams[0]
                self.current_platform = platform
                if call_window:
                    self.current_call_title = call_window
                self._last_detection_reason = (
                    f"{platform.display_name} media process '{process}' using UDP port {port}"
                )
                return True

            # General app processes may retain idle sockets or windows. Requiring
            # both signals avoids treating an open client as an active call.
            if streams and call_window:
                process, port, _ = streams[0]
                self.current_platform = platform
                self.current_call_title = call_window
                self._last_detection_reason = (
                    f"{platform.display_name} call window '{call_window}' plus "
                    f"process '{process}' using UDP port {port}"
                )
                return True

        self._last_detection_reason = ""
        return False

    def is_webex_running(self) -> bool:
        """Backward-compatible alias for the multi-platform active-call check."""
        return self.is_call_active()

    def has_call_ended(self) -> bool:
        """Return True only after call evidence is absent for the full grace period."""
        if self.is_call_active():
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

    def wait_for_state_change(self) -> bool:
        if not self.is_in_meeting:
            # Do not let identity from the previous call leak into a new session.
            self.current_call_title = None
            self.current_platform = None
        while True:
            running = self.is_call_active()
            if running and not self.is_in_meeting:
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
            elif not running and self.is_in_meeting:
                self._active_poll_count = 0
                self._idle_poll_count += 1
                if self._idle_poll_count >= 2:
                    self.is_in_meeting = False
                    self._idle_poll_count = 0
                    logger.info(
                        "%s call / meeting media stream has ended.",
                        self.current_platform_name or "Meeting",
                    )
                    return False
            else:
                self._idle_poll_count = 0
                if not running:
                    self._active_poll_count = 0

            time.sleep(self.poll_interval)
