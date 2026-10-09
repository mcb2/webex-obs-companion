import subprocess

from .control_dialog import CONSENT_NOTICE


def _escape_applescript_string(value: str) -> str:
    """Escape dynamic text before embedding it in an AppleScript string."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


class UIBanner:
    @staticmethod
    def show_recording_menu() -> None:
        UIBanner.show_notification("Webex OBS Companion", "Use the recording actions in the macOS menu bar.")

    @staticmethod
    def show_manual_consent_prompt(meeting_title: str = "Webex Session") -> str:
        text = _escape_applescript_string(
            f"Recording started: {meeting_title}\n\nTwo-party / all-party consent\n"
            f"{CONSENT_NOTICE}\n\nThis notice closes after 10 seconds; recording continues."
        )
        script = f'''display dialog "{text}" ¬
            with title "Webex OBS Companion" ¬
            with icon caution ¬
            buttons {{"Cancel & Discard", "OK"}} ¬
            default button "OK" ¬
            cancel button "Cancel & Discard" ¬
            giving up after 10'''
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            if "gave up:true" in result.stdout or "button returned:OK" in result.stdout:
                return "ok"
            # AppleScript represents an explicit cancel/Escape with error -128.
            if "button returned:Cancel & Discard" in result.stdout or "(-128)" in result.stderr:
                return "cancel"
        except Exception:
            pass
        return "ok"

    @staticmethod
    def confirm_discard() -> bool:
        script = '''display dialog "Stop and permanently delete this recording?" ¬
            with title "Webex OBS Companion" ¬
            with icon caution ¬
            buttons {"No, keep recording", "Yes, stop and delete"} ¬
            default button "No, keep recording" ¬
            cancel button "No, keep recording"'''
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            return result.returncode == 0 and "button returned:Yes, stop and delete" in result.stdout
        except Exception:
            return False

    @staticmethod
    def choose_webex_window(
        windows, suggested_window_id=None, displays=(), selected_display_uuid=None
    ) -> int | str | None:
        """Picker for foreground CLI runs without the AppKit menu bar."""
        if not windows and not displays:
            return None
        labels = {
            f"{window.window_id} | {window.title} ({window.owner})": window.window_id
            for window in windows
        }
        labels.update({
            f"Entire screen | {display.label}": display.display_uuid
            for display in displays
        })
        items = ", ".join(f'"{_escape_applescript_string(label)}"' for label in labels)
        script = (
            f'set selectedWindow to choose from list {{{items}}} '
            'with title "Webex OBS Companion" '
            'with prompt "Select a Webex window or entire screen to record"\n'
            'if selectedWindow is false then return ""\n'
            'return item 1 of selectedWindow'
        )
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            return labels.get(result.stdout.strip()) if result.returncode == 0 else None
        except Exception:
            return None

    @staticmethod
    def show_startup_prompt(meeting_title: str = "Webex Session") -> str:
        meeting_title = _escape_applescript_string(meeting_title)
        prompt_text = (
            "🔴 RECORDING STARTED — AUDIO\\n\\n"
            f"MEETING\\n{meeting_title}\\n\\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\\n\\n"
            "⚠️ RECORDING CONSENT\\n"
            "Recording laws vary by location. Obtain permission from all participants "
            "when required by applicable law.\\n\\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\\n\\n"
            "KEYBOARD SHORTCUTS\\n"
            "• Direct Video: ⌘ + Shift + V\\n"
            "• Stop & Transcribe: ⌘ + Shift + S\\n"
            "• Reopen Menu: ⌘ + Shift + R\\n\\n"
            "Choose an action (auto-keeps Audio in 15s):"
        )
        apple_script = f"""
        display dialog "{prompt_text}" ¬
            with title "Webex OBS Companion" ¬
            with icon caution ¬
            buttons {{"Cancel & Discard", "Switch to Video", "Keep Audio"}} ¬
            default button "Keep Audio" ¬
            cancel button "Cancel & Discard" ¬
            giving up after 15
        """
        try:
            result = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True,
                text=True,
            )
            output = result.stdout.strip()
            if "Switch to Video" in output or "button returned:Switch to Video" in output:
                return "switch_video"
            elif "Keep Audio" in output or "gave up:true" in output:
                return "keep_audio"
            else:
                return "cancel"
        except Exception:
            return "keep_audio"


    @staticmethod
    def show_notification(title: str, message: str) -> None:
        apple_script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", apple_script], capture_output=True)
