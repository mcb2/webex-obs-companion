import logging
import socket
import subprocess
import time
import psutil

logger = logging.getLogger(__name__)

# Standard RTP audio/video media ports for Webex
WEBEX_RTP_PORTS = {5004, 9000, 33434}
WEBEX_CORE_PROCESSES = ["webex", "ciscospark", "ciscocollabhost", "meeting center", "washost"]


class ProcessMonitor:
    def __init__(self, poll_interval: float = 3.0):
        self.poll_interval = poll_interval
        self.is_in_meeting = False
        self._idle_poll_count = 0

    def _has_active_rtp_media_stream(self) -> bool:
        """
        Check if Webex process has active UDP datagram sockets connected
        to remote RTP media port 5004 (strictly UDP, ignoring TCP 443 chat connections).
        """
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                pexe = (proc.info.get("exe") or "").lower()
                if not any(target in pname or target in pexe for target in WEBEX_CORE_PROCESSES):
                    continue

                conns = proc.connections(kind="inet")
                for conn in conns:
                    # MUST be a UDP datagram socket (SOCK_DGRAM)
                    if conn.type != socket.SOCK_DGRAM:
                        continue

                    raddr = getattr(conn, "raddr", None)
                    if not raddr:
                        continue

                    rport = getattr(raddr, "port", 0)
                    if rport in WEBEX_RTP_PORTS:
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        return False

    def _has_active_call_window(self) -> bool:
        """
        Check if Webex has an active multitasking floating call window open.
        """
        apple_script = 'tell application "System Events" to tell process "Webex" to get name of windows'
        try:
            res = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and res.stdout:
                windows = res.stdout.strip()
                if "Webex multitasking floating window" in windows:
                    return True
        except Exception:
            pass
        return False

    def is_webex_running(self) -> bool:
        """
        Returns True only during an active voice/video call or meeting.
        """
        # Primary check: Active UDP RTP media socket on port 5004
        if self._has_active_rtp_media_stream():
            return True

        # Secondary check: Webex multitasking floating in-call window
        if self._has_active_call_window():
            return True

        return False

    def wait_for_state_change(self) -> bool:
        while True:
            running = self.is_webex_running()
            if running and not self.is_in_meeting:
                self.is_in_meeting = True
                self._idle_poll_count = 0
                logger.info("Detected active Webex call / meeting session (UDP port 5004 active).")
                return True
            elif not running and self.is_in_meeting:
                self._idle_poll_count += 1
                if self._idle_poll_count >= 2:  # 6 seconds debounce
                    self.is_in_meeting = False
                    self._idle_poll_count = 0
                    logger.info("Webex call / meeting media stream has ended.")
                    return False
            else:
                self._idle_poll_count = 0

            time.sleep(self.poll_interval)
