"""Recording menu actions and consent prompts shared with UI tests."""

from dataclasses import dataclass


CONSENT_NOTICE = (
    "Recording laws vary by location. Obtain permission from all participants "
    "when required by applicable law."
)


@dataclass(frozen=True)
class ControlDialog:
    heading: str
    detail: str
    choices: tuple[tuple[str, str], ...]
    default_choice: str | None = None


def recording_menu_choices(
    is_recording: bool, is_video_recording: bool = False,
    transitioning: bool = False, source_picker_open: bool = False,
) -> tuple[tuple[str, str], ...]:
    if transitioning:
        return ()
    if not is_recording:
        return (("Start Audio Recording", "start_audio"),
                ("Start Video Recording", "start_video"))
    choices = ()
    if not is_video_recording:
        choices += (("Switch to Video", "switch_video"),)
    elif not source_picker_open:
        choices += (("Select Video Source…", "select_window"),)
    return choices + (("Stop & Transcribe", "stop_transcribe"),
                      ("Stop & Discard…", "stop_discard"))


def manual_consent_dialog(meeting_title: str) -> ControlDialog:
    return ControlDialog(
        "Recording started — recording consent",
        f"Meeting: {meeting_title}\n\nTwo-party / all-party consent\n{CONSENT_NOTICE}\n\n"
        "This notice closes after 10 seconds; recording continues.",
        (("OK", "ok"), ("Cancel & Discard", "cancel")),
        default_choice="ok",
    )


def startup_dialog(meeting_title: str) -> ControlDialog:
    return ControlDialog(
        "Recording started",
        f"Meeting: {meeting_title}\n\nRecording consent\n{CONSENT_NOTICE}\n\n"
        "Audio continues automatically after 15 seconds.",
        (("Keep Audio", "keep_audio"),
         ("Switch to Video", "switch_video"),
         ("Cancel & Discard", "cancel")),
        default_choice="keep_audio",
    )
