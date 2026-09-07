from collections.abc import Callable
import logging
from pynput import keyboard

logger = logging.getLogger(__name__)

class HotkeyListener:
    def __init__(
        self,
        on_video_switch: Callable[[], None],
        on_show_dialog: Callable[[], None] | None = None,
        on_stop_transcribe: Callable[[], None] | None = None,
    ):
        self.on_video_switch = on_video_switch
        self.on_show_dialog = on_show_dialog
        self.on_stop_transcribe = on_stop_transcribe
        self.listener: keyboard.GlobalHotKeys | None = None

    def start(self):
        hotkeys = {
            "<cmd>+<shift>+v": self._handle_video_hotkey,
            "<cmd>+<shift>+r": self._handle_dialog_hotkey,
            "<cmd>+<shift>+s": self._handle_stop_hotkey,
        }
        self.listener = keyboard.GlobalHotKeys(hotkeys)
        self.listener.start()
        logger.info(
            "Global hotkey listener registered: "
            "Cmd+Shift+V (Video), Cmd+Shift+R (Menu), Cmd+Shift+S (Stop & Transcribe)."
        )

    def _handle_video_hotkey(self):
        logger.info("Hotkey Cmd+Shift+V triggered (Direct Video Switch).")
        self.on_video_switch()

    def _handle_dialog_hotkey(self):
        logger.info("Hotkey Cmd+Shift+R triggered (Show Recording Menu).")
        if self.on_show_dialog:
            self.on_show_dialog()

    def _handle_stop_hotkey(self):
        logger.info("Hotkey Cmd+Shift+S triggered (Stop & Transcribe Now).")
        if self.on_stop_transcribe:
            self.on_stop_transcribe()

    def stop(self):
        if self.listener:
            self.listener.stop()
