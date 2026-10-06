import subprocess


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
    def choose_webex_window(windows, suggested_window_id=None) -> int | None:
        """Picker for foreground CLI runs without the AppKit menu bar."""
        if not windows:
            return None
        labels = {
            f"{window.window_id} | {window.title} ({window.owner})": window.window_id
            for window in windows
        }
        items = ", ".join(f'"{_escape_applescript_string(label)}"' for label in labels)
        script = (
            f'set selectedWindow to choose from list {{{items}}} '
            'with title "Webex OBS Companion" '
            'with prompt "Select the Webex window to record"\n'
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
    def show_control_prompt(
        is_recording: bool = True,
        meeting_title: str = "Webex Session",
        is_video_recording: bool = False,
    ) -> str:
        if is_recording:
            meeting_title = _escape_applescript_string(meeting_title)
            prompt_text = (
                "🎛️ RECORDING CONTROLS — ACTIVE\\n\\n"
                f"MEETING\\n{meeting_title}\\n\\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\\n\\n"
                "KEYBOARD SHORTCUTS\\n"
                "• Direct Video: ⌘ + Shift + V\\n"
                "• Stop & Transcribe: ⌘ + Shift + S\\n"
                "• Reopen Menu: ⌘ + Shift + R\\n\\n"
                "Choose an action:"
            )
            buttons_str = (
                '{"Stop & Discard", "Stop & Transcribe", "Close Menu"}'
                if is_video_recording else
                '{"Stop & Discard", "Stop & Transcribe", "Switch to Video"}'
            )
            default_btn = "Stop & Transcribe"
        else:
            prompt_text = (
                "🎛️ RECORDING CONTROLS — IDLE\\n\\n"
                "RECORDING OPTIONS\\n"
                "• Start Recording (Audio): Click below\\n"
                "• Start Recording (Video): Click below\\n\\n"
                "Choose an action:"
            )
            buttons_str = '{"Close Menu", "Start Video Rec", "Start Audio Rec"}'
            default_btn = "Start Audio Rec"

        apple_script = f"""
        display dialog "{prompt_text}" ¬
            with title "Webex OBS Companion" ¬
            with icon note ¬
            buttons {buttons_str} ¬
            default button "{default_btn}" ¬
            giving up after 25
        """
        if not is_recording:
            apple_script = apple_script.replace(
                'giving up after 25',
                'cancel button "Close Menu" ¬\n            giving up after 25',
            )

        try:
            result = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True,
                text=True,
            )
            output = result.stdout.strip()
            if "Stop & Transcribe" in output or "button returned:Stop & Transcribe" in output:
                return "stop_transcribe"
            elif "Switch to Video" in output or "button returned:Switch to Video" in output:
                return "switch_video"
            elif "Start Audio Rec" in output or "button returned:Start Audio Rec" in output:
                return "start_audio"
            elif "Start Video Rec" in output or "button returned:Start Video Rec" in output:
                return "start_video"
            elif "Stop & Discard" in output or "button returned:Stop & Discard" in output:
                confirmation = subprocess.run(
                    ["osascript", "-e", '''display dialog "Stop and permanently delete this recording?" ¬
                        with title "Webex OBS Companion" ¬
                        with icon caution ¬
                        buttons {"No, keep recording", "Yes, stop and delete"} ¬
                        default button "No, keep recording" ¬
                        cancel button "No, keep recording"'''],
                    capture_output=True,
                    text=True,
                )
                return (
                    "stop_discard" if confirmation.returncode == 0
                    and "button returned:Yes, stop and delete" in confirmation.stdout
                    else "close"
                )
            else:
                return "close"
        except Exception:
            return "close"

    @staticmethod
    def show_notification(title: str, message: str) -> None:
        apple_script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", apple_script], capture_output=True)
