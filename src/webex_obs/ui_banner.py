import subprocess

class UIBanner:
    @staticmethod
    def show_startup_prompt() -> str:
        prompt_text = (
            "🔴 Webex Meeting Detected — Recording Started (Audio)\\n\\n"
            "⚠️ Recording Consent Notice\\n"
            "Recording laws vary by location. Obtain permission from all participants "
            "when required by applicable law.\\n\\n"
            "• Direct Video: ⌘ + Shift + V\\n"
            "• Stop & Transcribe: ⌘ + Shift + S\\n"
            "• Reopen Menu: ⌘ + Shift + R\\n\\n"
            "Choose an action (auto-keeps Audio in 15s):"
        )
        apple_script = f"""
        display dialog "{prompt_text}" ¬
            with title "Webex OBS Companion" ¬
            buttons {{"Cancel & Discard", "Switch to Video", "Keep Audio"}} ¬
            default button "Keep Audio" ¬
            cancel button "Cancel & Discard" ¬
            giving up after 15
        """
        try:
            result = subprocess.run(["osascript", "-e", apple_script], capture_output=True, text=True)
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
    def show_control_prompt(is_recording: bool = True) -> str:
        if is_recording:
            prompt_text = (
                "🎛️ Webex Recording Controls (Active Recording)\\n\\n"
                "• Direct Video: ⌘ + Shift + V\\n"
                "• Stop & Transcribe: ⌘ + Shift + S\\n"
                "• Reopen Menu: ⌘ + Shift + R\\n\\n"
                "Choose an action:"
            )
            buttons_str = '{"Cancel & Discard", "Stop & Transcribe", "Switch to Video"}'
            default_btn = "Stop & Transcribe"
        else:
            prompt_text = (
                "🎛️ Webex Recording Controls (Idle)\\n\\n"
                "• Start Recording (Audio): Click below\\n"
                "• Start Recording (Video): Click below\\n\\n"
                "Choose an action:"
            )
            buttons_str = '{"Close Menu", "Start Video Rec", "Start Audio Rec"}'
            default_btn = "Start Audio Rec"

        apple_script = f"""
        display dialog "{prompt_text}" ¬
            with title "Webex OBS Companion" ¬
            buttons {buttons_str} ¬
            default button "{default_btn}" ¬
            cancel button "Cancel" ¬
            giving up after 25
        """
        # Replace cancel button gracefully
        if not is_recording:
            apple_script = apple_script.replace('cancel button "Cancel"', 'cancel button "Close Menu"')
        else:
            apple_script = apple_script.replace('cancel button "Cancel"', 'cancel button "Cancel & Discard"')

        try:
            result = subprocess.run(["osascript", "-e", apple_script], capture_output=True, text=True)
            output = result.stdout.strip()
            if "Stop & Transcribe" in output or "button returned:Stop & Transcribe" in output:
                return "stop_transcribe"
            elif "Switch to Video" in output or "button returned:Switch to Video" in output:
                return "switch_video"
            elif "Start Audio Rec" in output or "button returned:Start Audio Rec" in output:
                return "start_audio"
            elif "Start Video Rec" in output or "button returned:Start Video Rec" in output:
                return "start_video"
            elif "Cancel & Discard" in output or "button returned:Cancel & Discard" in output:
                return "cancel"
            else:
                return "close"
        except Exception:
            return "close"

    @staticmethod
    def show_notification(title: str, message: str) -> None:
        apple_script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", apple_script], capture_output=True)
