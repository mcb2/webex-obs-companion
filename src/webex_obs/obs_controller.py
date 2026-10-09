import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from obswebsocket import obsws, requests

from .macos_displays import active_displays

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebexWindow:
    window_id: int
    title: str
    owner: str
    width: int
    height: int

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class CaptureDisplay:
    display_uuid: str
    label: str


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
        self.selected_window_id: int | None = None
        self.selected_display_uuid: str | None = None
        self._seen_window_ids: set[int] = set()
        self._candidate_polls: dict[int, int] = {}
        self._selected_missing_polls = 0
        self._missing_selected_window_reported = False

    def obs_is_running(self) -> bool:
        return subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode == 0

    def quit_obs(self, idle_guard=None) -> bool:
        """Ask OBS to finish shutting down; never force-kill a recording."""
        for attempt in range(2):
            try:
                if not self.obs_is_running():
                    self.disconnect()
                    return True
                if idle_guard is not None and not idle_guard():
                    logger.info("OBS idle shutdown cancelled: recording request or output activity.")
                    return False
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
        """Verify OBS and keep it warm unless exit-after-recording is selected."""
        try:
            was_running = self.obs_is_running()
        except Exception as e:
            logger.warning("Could not check whether OBS is running: %s", e)
            return False
        if not self.connect():
            return False
        if not was_running and self.exit_on_stop:
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

    def outputs_confirmed_idle(self) -> bool:
        """Fail closed: recording, streaming and replay must all report idle."""
        if self.is_recording or not self.check_connection():
            return False
        try:
            for request in (requests.GetRecordStatus, requests.GetStreamStatus,
                            requests.GetReplayBufferStatus):
                data = self.ws.call(request()).datain
                if data.get("outputActive") is not False:
                    return False
            return True
        except Exception:
            logger.debug("Could not verify all OBS outputs idle; maintenance deferred.", exc_info=True)
            return False

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

    def list_webex_windows(self) -> list[WebexWindow]:
        window_list = []
        if HAS_QUARTZ:
            options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
            window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
        by_id = {int(item.get("kCGWindowNumber") or 0): item for item in window_list}
        matches: dict[int, WebexWindow] = {}

        def add(window_id: int, title: str, owner: str, item: dict) -> None:
            title = title.strip()
            if not any(name in owner.casefold() for name in (
                "webex", "ciscocollabhost", "washost", "meeting center"
            )) or not title or window_id <= 0:
                return
            if title.casefold() in {"webex", "webex multitasking floating window"}:
                return
            if any(name in title.casefold() for name in (
                "item-0", "control bar", "floating bar", "panel"
            )) or item.get("kCGWindowLayer", 0) != 0:
                return
            bounds = item.get("kCGWindowBounds") or {}
            width = int(bounds.get("Width", 0))
            height = int(bounds.get("Height", 0))
            if width and height and (width < 320 or height < 200):
                return
            matches[window_id] = WebexWindow(window_id, title, owner, width, height)

        for item in window_list:
            add(int(item.get("kCGWindowNumber") or 0),
                str(item.get("kCGWindowName") or ""),
                str(item.get("kCGWindowOwnerName") or ""), item)

        # OBS has Screen Recording permission even when the companion does not.
        # Its own window dropdown is the authoritative list of capturable IDs.
        if self.ws and self.is_connected:
            try:
                response = self.ws.call(requests.GetInputPropertiesListPropertyItems(
                    inputName="Webex-Meeting-Window", propertyName="window"
                )).datain
                for option in response.get("propertyItems", []):
                    label = str(option.get("itemName") or "")
                    if not label.startswith("[") or "] " not in label:
                        continue
                    owner, title = label[1:].split("] ", 1)
                    window_id = int(option.get("itemValue") or 0)
                    if by_id and window_id not in by_id:
                        continue
                    add(window_id, title, owner, by_id.get(window_id, {}))
            except Exception as exc:
                logger.debug("OBS window property list unavailable: %s", exc)
        return sorted(matches.values(), key=lambda w: w.area, reverse=True)

    @property
    def is_screen_recording(self) -> bool:
        return self.selected_display_uuid is not None

    def list_capture_displays(self, source_name: str = "Webex-Meeting-Window") -> list[CaptureDisplay]:
        """Return displays available to the macOS Screen Capture OBS source."""
        if not self.ws or not self.is_connected:
            return []
        try:
            info = self.ws.call(requests.GetInputSettings(inputName=source_name)).datain
            if info.get("inputKind") != "screen_capture":
                return []
            # OBS 32.2.2 crashes serializing the NULL placeholder in this
            # source's display_uuid property list (obs-studio issue #13905).
            # macOS supplies the same UUIDs without touching that OBS path.
            return [CaptureDisplay(value, label) for value, label in active_displays()]
        except Exception as exc:
            logger.warning("Could not list OBS capture displays: %s", exc)
            return []

    def select_capture_display(
        self, display_uuid: str, source_name: str = "Webex-Meeting-Window"
    ) -> bool:
        """Switch the existing video source to full-display capture."""
        if not display_uuid or not any(
            display.display_uuid == display_uuid for display in self.list_capture_displays(source_name)
        ):
            logger.warning("Selected display is not available to OBS: %s", display_uuid)
            return False
        try:
            self.ws.call(requests.SetInputSettings(
                inputName=source_name,
                inputSettings={"type": 0, "display_uuid": display_uuid},
                overlay=True,
            ))
            self.selected_display_uuid = display_uuid
            self.selected_window_id = None
            self._seen_window_ids.clear()
            self._candidate_polls.clear()
            self._selected_missing_polls = 0
            self._missing_selected_window_reported = False
            logger.info("Bound OBS source '%s' to full display %s.", source_name, display_uuid)
            return True
        except Exception as exc:
            logger.warning("Could not bind OBS video source to display: %s", exc)
            return False

    def bind_webex_video_window(
        self, window_id: int | None = None, source_name: str = "Webex-Meeting-Window"
    ) -> bool:
        windows = self.list_webex_windows()
        chosen = next((w for w in windows if w.window_id == window_id), None)
        if window_id is not None and chosen is None:
            logger.warning("Selected Webex window %s is no longer available.", window_id)
            return False
        if chosen is None:
            chosen = next((w for w in windows if w.window_id == self.selected_window_id), None)
        if chosen is None and windows:
            chosen = max(windows, key=lambda w: w.area)
        if chosen is None or not self.check_connection():
            return False
        try:
            info = self.ws.call(requests.GetInputSettings(inputName=source_name)).datain
            kind = info.get("inputKind")
            if kind == "screen_capture":
                settings = {"type": 1, "window": chosen.window_id}
            elif kind == "window_capture":
                settings = {"window_name": chosen.title, "owner_name": chosen.owner}
            else:
                logger.warning("OBS source '%s' has unsupported input kind '%s'.", source_name, kind)
                return False
            self.ws.call(requests.SetInputSettings(
                inputName=source_name,
                inputSettings=settings,
                overlay=True
            ))
            self.selected_window_id = chosen.window_id
            self.selected_display_uuid = None
            self._seen_window_ids = {w.window_id for w in windows}
            self._candidate_polls.clear()
            self._selected_missing_polls = 0
            self._missing_selected_window_reported = False
            logger.info("Bound OBS source '%s' to Webex window %s: '%s'", source_name,
                        chosen.window_id, chosen.title)
            return True
        except Exception as e:
            logger.warning("Could not bind OBS video source to Webex window: %s", e)
            return False

    def poll_webex_window_change(self) -> list[WebexWindow]:
        """Offer only stable, plausible replacements for the recorded window."""
        if not self.is_recording or self.current_scene != "Webex-Video" or self.is_screen_recording:
            return []
        windows = self.list_webex_windows()
        present = {w.window_id for w in windows}
        selected = next((w for w in windows if w.window_id == self.selected_window_id), None)
        if selected:
            self._selected_missing_polls = 0
            self._missing_selected_window_reported = False
        elif self.selected_window_id is not None:
            # macOS can briefly omit a window while Webex changes layouts.
            self._selected_missing_polls += 1
            if self._selected_missing_polls < 2 or self._missing_selected_window_reported:
                return []
            replacements = [w for w in windows if not self._is_transient_webex_window(w)]
            if replacements:
                self._missing_selected_window_reported = True
                return [max(replacements, key=self._recording_window_priority)]
            return []

        new = []
        for window in windows:
            if window.window_id == self.selected_window_id:
                continue
            if self._is_transient_webex_window(window):
                continue
            if selected and not self._is_better_recording_window(window, selected):
                continue
            count = self._candidate_polls.get(window.window_id, 0) + 1
            self._candidate_polls[window.window_id] = count
            if count >= 2 and window.window_id not in self._seen_window_ids:
                new.append(window)

        self._candidate_polls = {
            window_id: count for window_id, count in self._candidate_polls.items()
            if window_id in present
        }
        if new:
            self._seen_window_ids.update(w.window_id for w in new)
        # Forget windows that disappear so a recreated meeting window can be
        # considered again, but retain the selected ID until it is rebound.
        self._seen_window_ids.intersection_update(present)
        return [max(new, key=self._recording_window_priority)] if new else []

    @staticmethod
    def _is_transient_webex_window(window: WebexWindow) -> bool:
        title = window.title.casefold()
        return bool(re.search(r"\b(chat|preview|notification|message|floating)\b", title)) or (
            window.width > 0 and window.height > 0
            and (window.width < 640 or window.height < 360)
        )

    @staticmethod
    def _is_better_recording_window(candidate: WebexWindow, selected: WebexWindow) -> bool:
        # Keep the existing meeting/share window unless the newcomer is a
        # clearly identified share or a substantially larger main window. If Quartz
        # cannot supply dimensions, require an explicit share-like title.
        if OBSController._is_share_title(candidate.title):
            return True
        return bool(candidate.area and selected.area and candidate.area >= selected.area * 1.2)

    @staticmethod
    def _is_share_title(title: str) -> bool:
        return bool(re.search(
            r"\b(shared? content|screen shar(?:e|ing)|presentation)\b",
            title.casefold(),
        ))

    @staticmethod
    def _recording_window_priority(window: WebexWindow) -> tuple[bool, int]:
        return OBSController._is_share_title(window.title), window.area

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
                if scene_name == "Webex-Video" and not self.is_screen_recording:
                    if not self.bind_webex_video_window():
                        logger.warning("Video recording started without a bound Webex window.")
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
            self.selected_window_id = None
            self._seen_window_ids.clear()
            self._candidate_polls.clear()
            self._selected_missing_polls = 0
            self._missing_selected_window_reported = False
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
        self.selected_window_id = None
        self._seen_window_ids.clear()
        self._candidate_polls.clear()
        self._selected_missing_polls = 0
        self._missing_selected_window_reported = False
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
            if not self.is_screen_recording:
                self.bind_webex_video_window()
            self.ws.call(requests.StartRecord())
            self.is_recording = True
            logger.info("Started new Video segment in OBS.")
        except Exception as e:
            logger.error(f"Failed during video mode transition: {e}")
