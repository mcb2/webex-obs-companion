import logging
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from obswebsocket import events, obsws, requests
from obswebsocket.base_classes import Baserequests

from .macos_displays import active_displays
from .cleaner import MediaCleaner

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
    def __init__(self, address: str = "localhost", port: int = 4455, password: str = "", exit_on_stop: bool = True,
                 recording_directory: Path | None = None):
        self.address = address
        self.port = port
        self.password = password
        self.exit_on_stop = exit_on_stop
        self.recording_directory = recording_directory
        self.ws: obsws | None = None
        self.is_connected = False
        self.is_recording = False
        # Status evidence is separate from an unresolved application session.
        self.recording_status: bool | None = None
        self.stop_verified = False
        self._owns_recording = False
        self._ownership_lost = False
        self._owned_output_active = False
        self._current_output_path: str | None = None
        self._last_output_duration: float | None = None
        self.recorded_files: list[str] = []
        # Callbacks run on obs-websocket-py's receiver. Only the lifecycle
        # worker mutates ownership and paths; callbacks must never make RPCs.
        self._record_events: SimpleQueue[tuple[int, int | None, str, dict]] = SimpleQueue()
        self._connection_generation = 0
        self._segment_paths: list[list[str]] = []
        self._segment_index: int | None = None
        self._accepted_segments: set[int] = set()
        self._path_probe_warned = False
        self._last_record_event_state: str | None = None
        self.current_scene = "Webex-Audio"
        self.selected_window_id: int | None = None
        self.selected_display_uuid: str | None = None
        self._capture_reset_pending = False
        self._seen_window_ids: set[int] = set()
        self._candidate_polls: dict[int, int] = {}
        self._selected_missing_polls = 0
        self._missing_selected_window_reported = False

    def _call(self, request: Baserequests) -> Baserequests:
        """obs-websocket-py 1.0 returns rejected requests rather than raising."""
        response = self.ws.call(request)
        if response.status is not True:
            raise RuntimeError(f"OBS rejected {request.name}")
        return response

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
        """Launch/connect OBS at service startup and always leave it running.

        exit_on_stop applies only to a completed recording, not this readiness
        check. Idle maintenance owns preventative restarts in keep-open mode.
        """
        return self.connect()

    def outputs_confirmed_idle(self) -> bool:
        """Fail closed: recording, streaming and replay must all report idle."""
        if self.is_recording or not self.check_connection():
            return False
        try:
            for request in (requests.GetRecordStatus, requests.GetStreamStatus,
                            requests.GetReplayBufferStatus):
                data = self._call(request()).datain
                if data.get("outputActive") is not False:
                    return False
            return True
        except Exception:
            logger.debug("Could not verify all OBS outputs idle; maintenance deferred.", exc_info=True)
            return False

    def connect(self) -> bool:
        """Establish connection to OBS WebSocket server."""
        self._drain_record_events()
        if self.is_recording:
            self._ownership_lost = True
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
                self._connection_generation += 1
                try:
                    self.ws.disconnect()
                except Exception:
                    pass
            attempts = 15 if started_here else 3
            for attempt in range(attempts):
                try:
                    self._connection_generation += 1
                    generation = self._connection_generation
                    self.ws = obsws(self.address, self.port, self.password, legacy=False)
                    def enqueue(event, generation=generation):
                        self._record_events.put((generation, self._segment_index,
                                                 event.name, dict(event.datain)))
                    self.ws.register(enqueue, events.RecordStateChanged)
                    self.ws.register(enqueue, events.RecordFileChanged)
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
                self._call(requests.GetVersion())
                return True
            except Exception:
                logger.warning("OBS WebSocket connection was lost. Reconnecting...")
                self.disconnect()

        return self.connect()

    def disconnect(self):
        """Disconnect WebSocket cleanly."""
        self._drain_record_events()
        self._connection_generation += 1
        if self.ws and self.is_connected:
            try:
                self.ws.disconnect()
            except Exception:
                pass
        self.is_connected = False
        self.ws = None
        self.recording_status = None
        if self.is_recording:
            # OBS provides no recording ID in GetRecordStatus. After a gap we
            # cannot distinguish our output from one independently restarted.
            self._ownership_lost = True

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
                response = self._call(requests.GetInputPropertiesListPropertyItems(
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
            info = self._call(requests.GetInputSettings(inputName=source_name)).datain
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
            # Even a lost response may have enabled screen capture in OBS.
            # This safety flag is separate from the confirmed source cache.
            self._capture_reset_pending = True
            self._call(requests.SetInputSettings(
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
            info = self._call(requests.GetInputSettings(inputName=source_name)).datain
            kind = info.get("inputKind")
            if kind == "screen_capture":
                settings = {"type": 1, "window": chosen.window_id}
            elif kind == "window_capture":
                settings = {"window_name": chosen.title, "owner_name": chosen.owner}
            else:
                logger.warning("OBS source '%s' has unsupported input kind '%s'.", source_name, kind)
                return False
            self._call(requests.SetInputSettings(
                inputName=source_name,
                inputSettings=settings,
                overlay=True
            ))
            self.selected_window_id = chosen.window_id
            self.selected_display_uuid = None
            self._capture_reset_pending = False
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

    def _reset_capture_selection(self) -> bool:
        """Remove session-only full-display capture before another video session."""
        if not self._capture_reset_pending:
            return True
        try:
            self._call(requests.SetInputSettings(
                inputName="Webex-Meeting-Window",
                inputSettings={"type": 1, "window": 0}, overlay=True,
            ))
            self._capture_reset_pending = False
            return True
        except Exception as exc:
            logger.warning("Could not reset OBS screen selection; video start remains guarded: %s", exc)
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

    def _restart_audio_capture(self) -> None:
        """Attempt the recovery button before a start, under the lifecycle lock.

        OBS disables this button for healthy capture. Failure is advisory: an
        accepted button request also does not establish that audio is flowing.
        """
        try:
            inputs = self._call(requests.GetInputList(
                inputKind="sck_audio_capture",
            )).datain["inputs"]
        except Exception as e:
            logger.warning("Could not enumerate macOS Audio Capture sources before recording; "
                           "continuing without capture restart: %s", e)
            return
        for source in inputs:
            try:
                self._call(requests.PressInputPropertiesButton(
                    inputName=source["inputName"], propertyName="reactivate_capture",
                ))
                logger.info("OBS accepted macOS Audio Capture Restart Capture request.")
            except Exception as e:
                logger.warning("Could not press macOS Audio Capture Restart Capture "
                               "(OBS disables it for healthy capture); continuing recording: %s", e)

    def _apply_recording_directory(self) -> bool:
        """Called only before a start with confirmed idle output, never on AppKit."""
        if self.recording_directory is None:
            return True
        try:
            directory = self.recording_directory.expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            # Verify local write access without leaving a probe behind.
            with tempfile.TemporaryFile(dir=directory):
                pass
            current = self._call(requests.GetRecordDirectory()).datain.get("recordDirectory")
            if current != str(directory):
                self._call(requests.SetRecordDirectory(recordDirectory=str(directory)))
            verified = self._call(requests.GetRecordDirectory()).datain.get("recordDirectory")
            if verified != str(directory):
                raise RuntimeError("recording directory verification failed")
        except Exception as exc:
            logger.error(
                "Recording folder could not be applied and verified (%s). "
                "Check folder permissions and OBS WebSocket 5.3+ support; recording start blocked.",
                type(exc).__name__,
            )
            return False
        return True

    def start_recording(self, scene_name: str = "Webex-Audio") -> bool:
        for attempt in range(3):
            active = self._refresh_recording_status()
            if active is True:
                if self._owns_recording and not self._ownership_lost:
                    return True
                logger.warning("OBS has an independently started or unidentifiable recording; leaving it untouched.")
                return False
            if active is None:
                logger.warning("OBS status is unknown; refusing to start another recording.")
                return False
            # A positively idle output permits a new owned segment.
            if self._ownership_lost and self._segment_index is not None:
                # Late events cannot grant new paths to an ambiguous segment.
                self._accepted_segments.discard(self._segment_index)
            self._owns_recording = False
            self._ownership_lost = False
            try:
                if (scene_name == "Webex-Video" and self._capture_reset_pending
                        and not self.is_screen_recording):
                    if not self._reset_capture_selection() and not self.bind_webex_video_window():
                        logger.error("Video start blocked: previous screen selection could not be reset.")
                        return False
                if not self.switch_scene(scene_name):
                    raise RuntimeError("OBS scene is not ready")
                if scene_name == "Webex-Video" and not self.is_screen_recording:
                    if not self.bind_webex_video_window():
                        logger.warning("Video recording started without a bound Webex window.")
                self._restart_audio_capture()
                if not self._apply_recording_directory():
                    return False
                self.stop_verified = False
                # A lost response is ambiguous, not authorization to adopt OBS.
                self.is_recording = True
                self._segment_index = len(self._segment_paths)
                self._segment_paths.append([])
                self._current_output_path = None
                self._last_output_duration = None
                self._path_probe_warned = False
                self._last_record_event_state = None
                self._call(requests.StartRecord())
                self._accepted_segments.add(self._segment_index)
                # Scene preparation may have reconnected after the idle probe.
                # This accepted start establishes ownership on the new socket.
                self._ownership_lost = False
                self._owns_recording = True
                self._owned_output_active = False
                for _ in range(3):
                    active = self._refresh_recording_status()
                    if active is True and self._owns_recording and not self._ownership_lost:
                        logger.info("OBS recording started in scene '%s'.", scene_name)
                        return True
                    if active is None:
                        return False
                    time.sleep(0.4)
                raise RuntimeError("OBS accepted StartRecord but did not report an active recording")
            except Exception as e:
                active = self._refresh_recording_status()
                if active is True and self._owns_recording and not self._ownership_lost:
                    return True
                logger.warning("OBS start attempt %d/3 failed: %s", attempt + 1, e)
                if active is not False:
                    return False
                self.is_recording = bool(self.recorded_files)
                if attempt < 2:
                    time.sleep(1.0)
        logger.error("Could not start OBS recording after three attempts.")
        return False

    def _remember_owned_path(self, output_path: str | None, segment: int | None = None) -> None:
        if isinstance(output_path, str) and output_path and output_path not in self.recorded_files:
            MediaCleaner.protect_recordings([output_path])
            segment = self._segment_index if segment is None else segment
            if segment is None:
                self.recorded_files.append(output_path)
                return
            known = {path for paths in self._segment_paths for path in paths}
            legacy = [path for path in self.recorded_files if path not in known]
            self._segment_paths[segment].append(output_path)
            self.recorded_files = legacy + [path for paths in self._segment_paths for path in paths]

    def _capture_owned_output_path(self) -> None:
        """Read a live file target before OBS can exit and lose its last path.

        GetRecordStatus has no outputPath in v5. GetOutputSettings is generic;
        these are OBS Studio's built-in recording outputs and their local-file
        settings, not a protocol guarantee for third-party output plugins.
        Never search a directory or use another output as recording evidence.
        """
        if not self._owns_recording or self._ownership_lost or self._segment_index is None:
            return
        try:
            outputs = self._call(requests.GetOutputList()).datain.get("outputs", [])
            matches = [output for output in outputs if output.get("outputName") in {
                "simple_file_output", "simple_ffmpeg_output", "adv_file_output", "adv_ffmpeg_output",
            } and output.get("outputActive") is True]
            if len(matches) != 1:
                if not self._path_probe_warned:
                    logger.warning("OBS recording file target is unavailable; stop events/responses are required for recovery.")
                    self._path_probe_warned = True
                return
            settings = self._call(requests.GetOutputSettings(
                outputName=matches[0]["outputName"],
            )).datain.get("outputSettings", {})
            self._drain_record_events()
            if not self._owns_recording or self._ownership_lost:
                return
            field = "url" if matches[0]["outputName"].endswith("ffmpeg_output") else "path"
            path = settings.get(field)
            if isinstance(path, str) and Path(path).is_absolute():
                # Split-file outputs can retain their initial settings path.
                # Only an unfamiliar target signals an independent restart.
                if self._current_output_path and path not in self._segment_paths[self._segment_index]:
                    self._ownership_lost = True
                    logger.warning("OBS file target changed independently; leaving the new recording untouched.")
                    return
                if self._current_output_path is None:
                    self._current_output_path = path
                self._remember_owned_path(path)
        except Exception as exc:
            if not self._path_probe_warned:
                logger.warning("Could not read OBS recording file target; retaining event/response tracking: %s", exc)
                self._path_probe_warned = True

    def _drain_record_events(self) -> None:
        """Consume ordered events under the backend lifecycle lock.

        Socket generations reject late callbacks after reconnect. Segment
        indices preserve order when an event arrives after a recovery start.
        A stopped event alone never establishes ownership of an unknown output.
        """
        while True:
            try:
                generation, segment, name, data = self._record_events.get_nowait()
            except Empty:
                return
            if generation != self._connection_generation or not self.is_recording:
                continue
            path = data.get("outputPath")
            state = data.get("outputState")
            if (name == "RecordStateChanged" and segment == self._segment_index
                    and state in {"OBS_WEBSOCKET_OUTPUT_STARTING", "OBS_WEBSOCKET_OUTPUT_STARTED"}):
                # Consecutive duplicates are harmless. A new start transition
                # after our started output identifies an independent restart,
                # even if its stopped event or duration reset was missed.
                if (self._last_record_event_state == "OBS_WEBSOCKET_OUTPUT_STARTED"
                        and state == "OBS_WEBSOCKET_OUTPUT_STARTING"):
                    self._ownership_lost = True
                    logger.warning("OBS restarted recording independently; leaving the new output untouched.")
                self._last_record_event_state = state
            elif name == "RecordStateChanged" and state == "OBS_WEBSOCKET_OUTPUT_STOPPED":
                if data.get("outputActive") is not False:
                    continue
                if segment != self._segment_index and segment in self._accepted_segments:
                    # Fill an unreported earlier segment, but never attach an
                    # unfamiliar late file to a segment already identified.
                    if not self._segment_paths[segment]:
                        self._remember_owned_path(path, segment)
                    continue
                # Duplicates for an older file must not revoke a new segment.
                if path in self.recorded_files and path != self._current_output_path:
                    continue
                if segment != self._segment_index:
                    continue
                if (self._owns_recording or segment in self._accepted_segments) and not self._ownership_lost:
                    if self._current_output_path and path and path != self._current_output_path:
                        self._ownership_lost = True
                        continue
                    self._remember_owned_path(path)
                    if isinstance(path, str) and path:
                        self._current_output_path = path
                    self._owns_recording = False
                    self._owned_output_active = False
            elif name == "RecordFileChanged" and segment == self._segment_index:
                if self._owns_recording and not self._ownership_lost:
                    path = data.get("newOutputPath")
                    if isinstance(path, str) and Path(path).is_absolute():
                        if path in self.recorded_files:
                            continue
                        self._remember_owned_path(path)
                        self._current_output_path = path

    def _refresh_recording_status(self) -> bool | None:
        """Probe status without adopting an external recording or losing segments."""
        return self._recording_active_status()

    def ensure_recording(self, scene_name: str | None = None) -> bool:
        """Recover only from positively idle OBS; unknown outputs stay untouched."""
        active = self._refresh_recording_status()
        if active is True:
            return self._owns_recording and not self._ownership_lost
        if active is None:
            return False
        logger.warning("OBS recording stopped. Resuming in a new segment...")
        return self.start_recording(scene_name=scene_name or self.current_scene)

    def switch_scene(self, scene_name: str) -> bool:
        if not self.check_connection():
            return False
        try:
            self._call(requests.SetCurrentProgramScene(sceneName=scene_name))
            self.current_scene = scene_name
            logger.info(f"Switched OBS scene to '{scene_name}'.")
            return True
        except Exception as e:
            logger.warning(f"Could not switch to scene '{scene_name}': {e}")
            return False

    def stop_recording(self) -> list[str]:
        """Return segments only after a verified stop; retain everything otherwise."""
        owned_session = self.is_recording
        self.stop_verified = False
        active = self._recording_active_status()
        if active is None:
            logger.warning("Cannot verify OBS status; keeping the session for an explicit stop retry.")
            return []
        if active is True:
            if not self._owns_recording or self._ownership_lost:
                logger.warning("Refusing to stop an independently started or unidentifiable OBS recording.")
                return []
            try:
                res = self._call(requests.StopRecord())
                self._remember_owned_path(res.datain.get("outputPath"))
            except Exception as e:
                logger.error("Error stopping OBS recording: %s", e)
            # StopRecord can return before OBS finishes finalizing output.
            for _ in range(10):
                if self._recording_is_confirmed_stopped():
                    break
                time.sleep(0.5)
            else:
                logger.warning("OBS stop could not be verified; keeping session and segment state.")
                return []

        self.stop_verified = True
        self.is_recording = False
        self._owns_recording = False
        self._ownership_lost = False
        self._owned_output_active = False
        self._current_output_path = None
        self._last_output_duration = None
        files = list(self.recorded_files)
        self.recorded_files.clear()
        self._segment_paths.clear()
        self._accepted_segments.clear()
        self._segment_index = None
        self._last_record_event_state = None
        # Reset OBS as well as our session cache. A rejected reset must never
        # silently authorize full-screen capture in the next video session.
        self._reset_capture_selection()
        self.selected_window_id = None
        self.selected_display_uuid = None
        self._seen_window_ids.clear()
        self._candidate_polls.clear()
        self._selected_missing_polls = 0
        self._missing_selected_window_reported = False
        if self.exit_on_stop and owned_session:
            # Recheck idle outputs immediately before graceful shutdown.
            self.quit_obs(idle_guard=self.outputs_confirmed_idle)
        return files

    def _recording_is_confirmed_stopped(self) -> bool:
        return self._recording_active_status() is False

    def _recording_active_status(self) -> bool | None:
        """True = active, False = stopped, None = unknown. Never coerce a value."""
        self.recording_status = None
        self._drain_record_events()
        if not self.check_connection():
            self._ownership_lost = self._ownership_lost or self.is_recording
            return None
        try:
            res = self._call(requests.GetRecordStatus())
            data = res.datain
            self._drain_record_events()
            active = data.get("outputActive")
            if res.status is not True or type(active) is not bool:
                logger.warning("OBS recording status response was unsuccessful or lacked a boolean outputActive.")
                self._ownership_lost = self._ownership_lost or self.is_recording
                return None
            self.recording_status = active
            if not active and self._owned_output_active:
                self._owns_recording = False
                self._owned_output_active = False
            if active and self._owns_recording and not self._ownership_lost:
                duration = data.get("outputDuration")
                reset_duration = (isinstance(duration, (int, float))
                                  and self._last_output_duration is not None
                                  and duration < self._last_output_duration)
                if reset_duration:
                    self._ownership_lost = True
                    logger.warning("OBS output changed independently; leaving the new recording untouched.")
                else:
                    self._owned_output_active = True
                    if isinstance(duration, (int, float)):
                        self._last_output_duration = duration
                    if self._segment_index is not None:
                        self._capture_owned_output_path()
            return active
        except Exception as e:
            logger.warning("Could not verify OBS recording status: %s", e)
            self._ownership_lost = self._ownership_lost or self.is_recording
            return None

    def switch_to_video_mode(self) -> bool:
        # Switching scenes keeps the same output, avoiding an unverified
        # stop/start transition and preserving all segment bookkeeping.
        if self.current_scene == "Webex-Video":
            return False
        if (self._recording_active_status() is True and self._owns_recording
                and not self._ownership_lost):
            if self._capture_reset_pending and not self.is_screen_recording:
                if not self._reset_capture_selection() and not self.bind_webex_video_window():
                    return False
            if self.switch_scene("Webex-Video"):
                if not self.is_screen_recording:
                    self.bind_webex_video_window()
                return True
        return False
