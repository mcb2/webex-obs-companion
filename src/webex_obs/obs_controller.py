import logging
import os
import subprocess
import time
from obswebsocket import obsws, requests

logger = logging.getLogger(__name__)

try:
    import Quartz
    HAS_QUARTZ = True
except ImportError:
    HAS_QUARTZ = False


class OBSController:
    def __init__(self, address: str = "localhost", port: int = 4455, password: str = "", exit_on_stop: bool = True):
        self.address = address
        self.port = port
        self.password = password
        self.exit_on_stop = exit_on_stop
        self.ws: obsws | None = None
        self.is_connected = False
        self.is_recording = False
        self.recorded_files: list[str] = []
        self.current_scene = "Webex-Audio"

    def obs_is_running(self) -> bool:
        return subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode == 0

    def quit_obs(self) -> bool:
        """Ask OBS to finish shutting down; never force-kill a recording."""
        for attempt in range(2):
            try:
                if not self.obs_is_running():
                    self.disconnect()
                    return True
                result = subprocess.run(
                    ["osascript", "-e", 'quit app "OBS"'],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode:
                    logger.warning("OBS quit request failed: %s", result.stderr.strip())
                for _ in range(20):
                    if not self.obs_is_running():
                        self.disconnect()
                        logger.info("OBS Studio exited.")
                        return True
                    time.sleep(0.5)
            except Exception as e:
                logger.warning("Could not quit OBS Studio: %s", e)
            if attempt == 0:
                # A quit command can be ignored while OBS is still completing
                # startup. Do not send it again if recording began meanwhile.
                if self._recording_active_status() is not False:
                    logger.warning("OBS is no longer confirmed idle; skipping another quit request.")
                    return False
                logger.warning("OBS remains open; retrying the graceful quit request.")
        logger.warning("OBS Studio did not exit after two quit requests.")
        return False

    def initialize(self) -> bool:
        """Check OBS at service start, closing only an instance started for this check."""
        try:
            was_running = self.obs_is_running()
        except Exception as e:
            logger.warning("Could not check whether OBS is running: %s", e)
            return False
        if not self.connect():
            return False
        if not was_running:
            for attempt in range(10):
                active = self._recording_active_status()
                if active is False:
                    if not self.quit_obs():
                        logger.warning("Startup OBS check succeeded, but OBS could not be closed.")
                    break
                if active is True:
                    logger.warning("OBS is recording after startup; leaving it open.")
                    break
                if attempt < 9:
                    time.sleep(0.5)
            else:
                logger.warning("OBS recording status remained unavailable; leaving it open.")
        return True

    def connect(self) -> bool:
        """Establish connection to OBS WebSocket server."""
        try:
            started_here = not self.obs_is_running()
            if started_here:
                logger.info("OBS Studio is not running. Launching in background...")
                subprocess.run(["open", "-g", "-a", "OBS"], check=True)
        except Exception as e:
            logger.warning("Could not launch OBS Studio: %s", e)
            return False
        try:
            if self.ws:
                try:
                    self.ws.disconnect()
                except Exception:
                    pass
            attempts = 15 if started_here else 3
            for attempt in range(attempts):
                try:
                    self.ws = obsws(self.address, self.port, self.password)
                    self.ws.connect()
                    self.is_connected = True
                    logger.info("Connected to OBS Studio WebSocket v5.")
                    return True
                except Exception:
                    self.ws = None
                    if attempt < attempts - 1:
                        time.sleep(1.0)
            raise ConnectionError("OBS WebSocket did not become available")
        except Exception as e:
            logger.debug(f"Could not connect to OBS Studio at {self.address}:{self.port}: {e}")
            self.is_connected = False
            self.ws = None
            return False

    def check_connection(self) -> bool:
        """Verify if WebSocket is alive; reconnect automatically if dropped."""
        if self.ws and self.is_connected:
            try:
                # Fast heartbeat ping
                self.ws.call(requests.GetVersion())
                return True
            except Exception:
                logger.warning("OBS WebSocket connection was lost. Reconnecting...")
                self.disconnect()

        return self.connect()

    def disconnect(self):
        """Disconnect WebSocket cleanly."""
        if self.ws and self.is_connected:
            try:
                self.ws.disconnect()
            except Exception:
                pass
        self.is_connected = False
        self.ws = None

    def find_webex_window_title(self) -> str | None:
        if not HAS_QUARTZ:
            return None
        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        for window in window_list:
            owner = window.get("kCGWindowOwnerName", "")
            name = window.get("kCGWindowName", "")
            if any(t.lower() in owner.lower() for t in ["Webex", "CiscoCollabHost", "washost", "Meeting Center"]):
                if name and not any(ign in name for ign in ["Item-0", "Control Bar", "Floating Bar", "Panel"]):
                    return name
        return None

    def bind_webex_video_window(self, source_name: str = "Webex-Meeting-Window") -> bool:
        title = self.find_webex_window_title()
        if not title or not self.check_connection():
            return False
        try:
            self.ws.call(requests.SetInputSettings(
                inputName=source_name,
                inputSettings={"window_name": title, "owner_name": "CiscoCollabHost"},
                overlay=True
            ))
            logger.info(f"Dynamically bound OBS source '{source_name}' to live window: '{title}'")
            return True
        except Exception as e:
            logger.debug(f"Dynamic window bind notice: {e}")
            return False

    def start_recording(self, scene_name: str = "Webex-Audio") -> bool:
        for attempt in range(3):
            # The previous command may have succeeded even if its response was
            # interrupted. Never send a second StartRecord in that case.
            if self._refresh_recording_status():
                logger.info("OBS recording is already active in scene '%s'.", scene_name)
                return True
            try:
                if not self.switch_scene(scene_name):
                    raise RuntimeError("OBS scene is not ready")
                if scene_name == "Webex-Video":
                    self.bind_webex_video_window()
                self.ws.call(requests.StartRecord())
                for _ in range(3):
                    if self._refresh_recording_status():
                        logger.info("OBS recording started in scene '%s'.", scene_name)
                        return True
                    time.sleep(0.4)
                raise RuntimeError("OBS accepted StartRecord but did not report an active recording")
            except Exception as e:
                if self._refresh_recording_status():
                    logger.info("OBS recording is active after an interrupted start command.")
                    return True
                logger.warning("OBS start attempt %d/3 failed: %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(1.0)
        logger.error("Could not start OBS recording after three attempts.")
        return False

    def _refresh_recording_status(self) -> bool:
        """Query OBS instead of trusting the last command sent over WebSocket."""
        if not self.check_connection():
            self.is_recording = False
            return False
        try:
            res = self.ws.call(requests.GetRecordStatus())
            data = getattr(res, "datain", {})
            self.is_recording = bool(data.get("outputActive", False))
            output_path = data.get("outputPath", "")
            if output_path and output_path not in self.recorded_files:
                self.recorded_files.append(output_path)
            return self.is_recording
        except Exception as e:
            logger.warning(f"Could not verify OBS recording status: {e}")
            self.is_recording = False
            return False

    def ensure_recording(self, scene_name: str | None = None) -> bool:
        """Keep a live call recording even if OBS exits or its output stops."""
        if self._refresh_recording_status():
            return True
        logger.warning("OBS recording is no longer active. Resuming in a new segment...")
        return self.start_recording(scene_name=scene_name or self.current_scene)

    def switch_scene(self, scene_name: str) -> bool:
        if not self.check_connection():
            return False
        try:
            self.ws.call(requests.SetCurrentProgramScene(sceneName=scene_name))
            self.current_scene = scene_name
            logger.info(f"Switched OBS scene to '{scene_name}'.")
            return True
        except Exception as e:
            logger.warning(f"Could not switch to scene '{scene_name}': {e}")
            return False

    def stop_recording(self) -> list[str]:
        if not self.check_connection():
            logger.warning("Cannot stop OBS recording cleanly: WebSocket is not connected.")
            files = list(self.recorded_files)
            self.recorded_files.clear()
            return files

        try:
            res = self.ws.call(requests.StopRecord())
            output_path = getattr(res, "datain", {}).get("outputPath", "")
            if output_path and os.path.exists(output_path):
                if output_path not in self.recorded_files:
                    self.recorded_files.append(output_path)
            logger.info("OBS StopRecord command returned.")
        except Exception as e:
            logger.error(f"Error stopping OBS recording: {e}")
        # OBS may still be finalizing its output after StopRecord returns.
        stopped = False
        for _ in range(10):
            if self._recording_is_confirmed_stopped():
                stopped = True
                break
            time.sleep(0.5)
        if stopped and self.exit_on_stop:
            self.quit_obs()
        elif not stopped:
            logger.warning("OBS recording stop could not be verified; leaving OBS open.")
        else:
            logger.info("OBS recording stopped; keeping OBS open as configured.")
        files = list(self.recorded_files)
        self.recorded_files.clear()
        return files

    def _recording_is_confirmed_stopped(self) -> bool:
        return self._recording_active_status() is False

    def _recording_active_status(self) -> bool | None:
        """Return OBS's status, or None if it could not be verified."""
        if not self.check_connection():
            return None
        try:
            data = self.ws.call(requests.GetRecordStatus()).datain
            if "outputActive" not in data:
                logger.warning("OBS recording status response lacked outputActive.")
                return None
            active = bool(data["outputActive"])
            self.is_recording = active
            return active
        except Exception as e:
            logger.warning("Could not verify OBS recording status: %s", e)
            return None

    def switch_to_video_mode(self) -> None:
        if not self.is_recording:
            self.start_recording(scene_name="Webex-Video")
            return
        logger.info("Transitioning from Audio recording to Video recording...")
        try:
            res = self.ws.call(requests.StopRecord())
            output_path = getattr(res, "datain", {}).get("outputPath", "")
            if output_path and os.path.exists(output_path):
                self.recorded_files.append(output_path)
            time.sleep(1.0)
            self.switch_scene("Webex-Video")
            self.bind_webex_video_window()
            self.ws.call(requests.StartRecord())
            self.is_recording = True
            logger.info("Started new Video segment in OBS.")
        except Exception as e:
            logger.error(f"Failed during video mode transition: {e}")
