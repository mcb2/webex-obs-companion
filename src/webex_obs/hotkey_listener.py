import logging
from collections.abc import Callable

from pynput import keyboard

logger = logging.getLogger(__name__)


def display_hotkey(hotkey: str) -> str:
    """Convert pynput hotkey syntax into a compact, user-facing label."""
    names = {
        "<cmd>": "Cmd",
        "<cmd_l>": "Left Cmd",
        "<cmd_r>": "Right Cmd",
        "<ctrl>": "Ctrl",
        "<alt>": "Option",
        "<shift>": "Shift",
    }
    return "+".join(
        names.get(part, part.strip("<>").upper()) for part in hotkey.split("+")
    )


def validate_hotkeys(*hotkeys: str) -> None:
    """Reject invalid or duplicate pynput hotkey combinations."""
    if len(set(hotkeys)) != len(hotkeys):
        raise ValueError("Each global hotkey must use a different key combination.")
    for hotkey in hotkeys:
        try:
            keyboard.HotKey.parse(hotkey)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid global hotkey '{hotkey}': {exc}") from exc


class HotkeyListener:
    def __init__(
        self,
        on_video_switch: Callable[[], None],
        on_show_dialog: Callable[[], None] | None = None,
        on_stop_transcribe: Callable[[], None] | None = None,
        video_hotkey: str = "<cmd>+<shift>+v",
        menu_hotkey: str = "<cmd>+<shift>+r",
        stop_transcribe_hotkey: str = "<cmd>+<shift>+s",
    ):
        self.on_video_switch = on_video_switch
        self.on_show_dialog = on_show_dialog
        self.on_stop_transcribe = on_stop_transcribe
        self.video_hotkey = video_hotkey
        self.menu_hotkey = menu_hotkey
        self.stop_transcribe_hotkey = stop_transcribe_hotkey
        self.listener: keyboard.GlobalHotKeys | None = None

    def start(self):
        validate_hotkeys(
            self.video_hotkey, self.menu_hotkey, self.stop_transcribe_hotkey
        )
        hotkeys = {
            self.video_hotkey: self._handle_video_hotkey,
            self.menu_hotkey: self._handle_dialog_hotkey,
            self.stop_transcribe_hotkey: self._handle_stop_hotkey,
        }
        self.listener = keyboard.GlobalHotKeys(hotkeys)
        self.listener.start()
        logger.info(
            "Global hotkey listener registered: "
            "%s (Video), %s (Menu), %s (Stop & Transcribe).",
            display_hotkey(self.video_hotkey),
            display_hotkey(self.menu_hotkey),
            display_hotkey(self.stop_transcribe_hotkey),
        )

    def _handle_video_hotkey(self):
        logger.info(
            "Hotkey %s triggered (Direct Video Switch).",
            display_hotkey(self.video_hotkey),
        )
        self.on_video_switch()

    def _handle_dialog_hotkey(self):
        logger.info(
            "Hotkey %s triggered (Show Recording Menu).",
            display_hotkey(self.menu_hotkey),
        )
        if self.on_show_dialog:
            self.on_show_dialog()

    def _handle_stop_hotkey(self):
        logger.info(
            "Hotkey %s triggered (Stop & Transcribe Now).",
            display_hotkey(self.stop_transcribe_hotkey),
        )
        if self.on_stop_transcribe:
            self.on_stop_transcribe()

    def stop(self):
        if self.listener:
            self.listener.stop()
