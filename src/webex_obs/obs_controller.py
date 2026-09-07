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
    def __init__(self, address: str = "localhost", port: int = 4455, password: str = "", relaunch_per_call: bool = True):
        self.address = address
        self.port = port
        self.password = password
        self.relaunch_per_call = relaunch_per_call
        self.ws: obsws | None = None
        self.is_connected = False
        self.is_recording = False
        self.recorded_files: list[str] = []

    def relaunch_obs(self) -> bool:
        """Gracefully quit any existing OBS instance and launch fresh to clear macOS CoreAudio stalls."""
        logger.info("Restarting OBS Studio to guarantee fresh CoreAudio capture buffers...")
        self.disconnect()

        try:
            # Request graceful quit via AppleScript
            subprocess.run(["osascript", "-e", 'quit app "OBS"'], capture_output=True)
            for _ in range(6):
                time.sleep(0.5)
                if subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode != 0:
                    break

            # Force terminate if still lingering
            if subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode == 0:
                subprocess.run(["pkill", "-9", "-x", "OBS"], capture_output=True)
                time.sleep(0.5)

            # Launch OBS in background
            subprocess.run(["open", "-g", "-a", "OBS"])

            # Poll for WebSocket availability
            for attempt in range(1, 15):
                time.sleep(1.0)
                if self.connect():
                    logger.info("OBS Studio successfully restarted and reconnected.")
                    return True

            logger.warning("OBS Studio relaunched but WebSocket connection timed out.")
            return False
        except Exception as e:
            logger.warning(f"Error during OBS relaunch: {e}")
            return self.connect()

    def ensure_obs_running(self) -> None:
        """Launch OBS Studio in the background if not currently running."""
        try:
            res = subprocess.run(["pgrep", "-x", "OBS"], capture_output=True)
            if res.returncode != 0:
                logger.info("OBS Studio is not running. Auto-launching in background (-g)...")
                subprocess.run(["open", "-g", "-a", "OBS"])
                for _ in range(8):
                    time.sleep(1.0)
                    if subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode == 0:
                        break
                time.sleep(1.5)
        except Exception as e:
            logger.warning(f"Could not auto-launch OBS Studio: {e}")

    def connect(self) -> bool:
        """Establish connection to OBS WebSocket server."""
        self.ensure_obs_running()
        try:
            if self.ws:
                try:
                    self.ws.disconnect()
                except Exception:
                    pass
            self.ws = obsws(self.address, self.port, self.password)
            self.ws.connect()
            self.is_connected = True
            logger.info("Connected to OBS Studio WebSocket v5.")
            return True
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

    def start_recording(self, scene_name: str = "Webex-Audio", relaunch: bool = False) -> bool:
        if relaunch or self.relaunch_per_call:
            self.relaunch_obs()
        elif not self.check_connection():
            return False

        try:
            self.switch_scene(scene_name)
            if scene_name == "Webex-Video":
                self.bind_webex_video_window()
            self.ws.call(requests.StartRecord())
            self.is_recording = True
            logger.info(f"OBS recording started in scene '{scene_name}'.")
            return True
        except Exception as e:
            logger.error(f"Failed to start OBS recording: {e}")
            # Try one reconnect attempt
            if self.connect():
                try:
                    self.switch_scene(scene_name)
                    self.ws.call(requests.StartRecord())
                    self.is_recording = True
                    logger.info(f"OBS recording started in scene '{scene_name}' after reconnect.")
                    return True
                except Exception as retry_err:
                    logger.error(f"Retry start recording failed: {retry_err}")
            return False

    def switch_scene(self, scene_name: str) -> bool:
        if not self.check_connection():
            return False
        try:
            self.ws.call(requests.SetCurrentProgramScene(sceneName=scene_name))
            logger.info(f"Switched OBS scene to '{scene_name}'.")
            return True
        except Exception as e:
            logger.warning(f"Could not switch to scene '{scene_name}': {e}")
            return False

    def stop_recording(self) -> list[str]:
        if not self.check_connection():
            logger.warning("Cannot stop OBS recording cleanly: WebSocket is not connected.")
            self.is_recording = False
            files = list(self.recorded_files)
            self.recorded_files.clear()
            return files

        try:
            res = self.ws.call(requests.StopRecord())
            self.is_recording = False
            output_path = getattr(res, "datain", {}).get("outputPath", "")
            if output_path and os.path.exists(output_path):
                self.recorded_files.append(output_path)
            logger.info("OBS recording stopped.")
        except Exception as e:
            logger.error(f"Error stopping OBS recording: {e}")
            self.is_recording = False

        time.sleep(1.0)
        files = list(self.recorded_files)
        self.recorded_files.clear()
        return files

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
