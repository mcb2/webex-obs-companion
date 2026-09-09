import logging
import socket
import subprocess
import time
import psutil

logger = logging.getLogger(__name__)

# Standard RTP audio/video media ports for Webex
WEBEX_RTP_PORTS = {5004, 9000, 33434}
WEBEX_CORE_PROCESSES = ["webex", "ciscospark", "ciscocollabhost", "meeting center", "washost"]
WEBEX_MEDIA_PROCESSES = ["ciscocollabhost", "meeting center", "washost"]
NON_CALL_WINDOW_NAMES = {"webex", "webex multitasking floating window"}


class ProcessMonitor:
    def __init__(self, poll_interval: float = 3.0, call_end_grace_seconds: float = 15.0):
        self.poll_interval = poll_interval
        self.call_end_grace_seconds = call_end_grace_seconds
        self.is_in_meeting = False
        self._idle_poll_count = 0
        self._active_poll_count = 0
        self._inactive_since: float | None = None
        self._last_detection_reason = ""

    def _active_rtp_media_streams(self) -> list[tuple[str, int, bool]]:
        """
        Check if Webex process has active UDP datagram sockets connected
        to remote RTP media port 5004 (strictly UDP, ignoring TCP 443 chat connections).
        """
        streams = []
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                pexe = (proc.info.get("exe") or "").lower()
                if not any(target in pname or target in pexe for target in WEBEX_CORE_PROCESSES):
                    continue

                conns = proc.net_connections(kind="inet")
                for conn in conns:
                    # MUST be a UDP datagram socket (SOCK_DGRAM)
                    if conn.type != socket.SOCK_DGRAM:
                        continue

                    raddr = getattr(conn, "raddr", None)
                    if not raddr:
                        continue

                    rport = getattr(raddr, "port", 0)
                    if rport in WEBEX_RTP_PORTS:
                        is_media_process = any(target in pname or target in pexe for target in WEBEX_MEDIA_PROCESSES)
                        streams.append((pname or pexe, rport, is_media_process))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        return streams

    def _active_call_window_name(self) -> str | None:
        """
        Return a distinct Webex call window, excluding ordinary and unreliable
        idle/floating windows.
        """
        apple_script = """
        tell application "System Events" to tell process "Webex"
            set windowNames to {}
            repeat with webexWindow in windows
                set end of windowNames to name of webexWindow
            end repeat
            set AppleScript's text item delimiters to linefeed
            return windowNames as text
        end tell
        """
        try:
            res = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and res.stdout:
                for window_name in res.stdout.splitlines():
                    window_name = window_name.strip()
                    if window_name and window_name.casefold() not in NON_CALL_WINDOW_NAMES:
                        return window_name
        except Exception:
            pass
        return None

    def is_webex_running(self) -> bool:
        """
        Returns True only during an active voice/video call or meeting.
        """
        streams = self._active_rtp_media_streams()
        call_window = self._active_call_window_name()

        # A call-specific Webex media helper with RTP is strong evidence by itself.
        media_streams = [stream for stream in streams if stream[2]]
        if media_streams:
            process, port, _ = media_streams[0]
            self._last_detection_reason = f"media process '{process}' using UDP port {port}"
            return True

        # The general Webex app can keep media-port sockets open while idle, and the
        # floating window can linger. Require both weaker signals together.
        if streams and call_window:
            process, port, _ = streams[0]
            self._last_detection_reason = (
                f"call window '{call_window}' plus process '{process}' using UDP port {port}"
            )
            return True

        self._last_detection_reason = ""
        return False

    def has_call_ended(self) -> bool:
        """Return True only after call evidence is absent for the full grace period."""
        if self.is_webex_running():
            self._inactive_since = None
            return False

        now = time.monotonic()
        if self._inactive_since is None:
            self._inactive_since = now
            logger.warning(
                "Webex call evidence temporarily missing; keeping recording active "
                "for up to %.1f seconds.",
                self.call_end_grace_seconds,
            )
            return self.call_end_grace_seconds == 0

        if now - self._inactive_since < self.call_end_grace_seconds:
            return False

        self._inactive_since = None
        logger.info("Webex call / meeting media stream has ended.")
        return True

    def wait_for_state_change(self) -> bool:
        while True:
            running = self.is_webex_running()
            if running and not self.is_in_meeting:
                self._active_poll_count += 1
                if self._active_poll_count >= 2:
                    self.is_in_meeting = True
                    self._active_poll_count = 0
                    self._idle_poll_count = 0
                    self._inactive_since = None
                    logger.info(f"Detected active Webex call / meeting session: {self._last_detection_reason}.")
                    return True
            elif not running and self.is_in_meeting:
                self._active_poll_count = 0
                self._idle_poll_count += 1
                if self._idle_poll_count >= 2:  # 6 seconds debounce
                    self.is_in_meeting = False
                    self._idle_poll_count = 0
                    logger.info("Webex call / meeting media stream has ended.")
                    return False
            else:
                self._idle_poll_count = 0
                if not running:
                    self._active_poll_count = 0

            time.sleep(self.poll_interval)
