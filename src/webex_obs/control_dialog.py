"""Recording control choices shared by the native UI and its tests."""

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


def control_dialog(is_recording: bool, meeting_title: str) -> ControlDialog:
    if is_recording:
        return ControlDialog(
            "Recording controls",
            f"Recording: {meeting_title}",
            (("Switch to Video", "switch_video"),
             ("Stop & Transcribe", "stop_transcribe"),
             ("Cancel", "close")),
        )
    return ControlDialog(
        "Start recording",
        f"Meeting: {meeting_title}\n\nRecording consent\n{CONSENT_NOTICE}",
        (("Start Audio", "start_audio"),
         ("Start Video", "start_video"),
         ("Cancel", "close")),
    )
