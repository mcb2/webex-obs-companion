# Webex OBS Companion Architecture

## UI and recording boundaries

On macOS the main thread owns AppKit (`macos_ui.MacOSUI`): a status item,
recording confirmation and control alerts, and Settings. The call lifecycle
in `daemon.WebexOBSDaemon` runs on a worker thread. Menu actions present dialogs
on the main thread; hotkey requests cross a queue to the main run loop. The
confirmation retains the 15-second default to audio. The legacy `UIBanner`
remains available to headless tests and handles notifications.

The lifecycle depends on `recording_backend.RecordingBackend`, which defines
connect, start, verify/recover, switch to video, stop (returning all completed
segment paths), and disconnect. `create_recording_backend` currently creates
an `OBSRecordingBackend` adapter around `OBSController`. A native recorder can
implement the same contract and handle its own permissions, audio/video devices,
recovery and output files. The adapter maps audio/video modes to OBS scene names.
OBS start verifies output status and retries transient startup failures against
the same OBS instance. Service startup leaves OBS running; recording stop can
optionally exit it after verifying that recording has stopped.
`RecordingSession`, `Transcriber`, and
`WebexClient` consume backend-independent file paths and meeting titles.

Settings updates validate values, atomically change the existing `.env`, and
replace the running detector, transcription, delivery, and hotkey configuration.
The OBS adapter defers connection changes until an active recording stops.
LaunchAgent stdout/stderr paths and OBS's recording output path are managed by
their respective applications.

During Webex video recording, `OBSController` enumerates capturable Webex
windows through Quartz and the OBS source's window list. New windows must
persist across two polls and pass a meeting/share plausibility check; temporary
chat previews do not displace the selected recording window. A qualifying
window is selected automatically or presented in an AppKit picker according to
`SHARED_WINDOW_BEHAVIOR`. Picker results return to the lifecycle worker before
OBS source settings are changed, so the recording output stays active while
the selected video window changes. The same picker can switch the macOS Screen
Capture source to a macOS-listed full display; window polling is suspended until
a Webex window is selected again. This selection applies to the current
session only because macOS window IDs change when Webex recreates windows.

## Detection, maintenance, and post-processing

`ProcessMonitor` combines native Webex/Zoom/Teams window evidence with
per-process Core Audio input/output state and known UDP media sockets. Core
Audio permits detection independently of media transport; UDP is an additional
signal and fallback. Automatic start requires sustained evidence across two
positive polls, and call end uses a configurable grace period (15 seconds by
default). OBS preparation precedes slow window validation and title enrichment.

`OBSRecordingBackend` delegates guarded idle restarts to `OBSMaintenance`.
OBS stays open between recordings by default. The default 60-minute minimum
uptime, two quiet minutes, safe local-hour windows, output-idle checks, and
pending-request checks gate maintenance. Exit-after-recording mode disables
idle maintenance. Window binding is implemented within `OBSController`, rather
than a separate WindowBinder component.

Four configurable hotkeys request audio start, video start/switch, video source
selection, and stop/transcribe. The status menu exposes actions appropriate to
the recording mode. Manual starts display a timed consent notice after capture
begins; discard requires a verified recording stop before deleting segments.

Completed sessions enter `PostProcessingWorker`, a FIFO queue on a separate
background thread. Each job retains its transcription, delivery, and retention
configuration while call monitoring continues. `Transcriber` uses MLX Whisper
and optionally `NeuralDiarizer` (pyannote, with a rough acoustic fallback).
Speaker labels are anonymous. `WebexClient` sends transcripts to a configured
room or personal recipient, with room ID taking precedence. Long transcripts
are attached as text files. My Agent forwarding is a manual user action.
`MediaCleaner` prunes old media after each job using the configured retention
period (14 days by default).

```text
Native Webex / Zoom / Teams
           |
           v
ProcessMonitor (windows + Core Audio + UDP)
           |
           v
WebexOBSDaemon lifecycle worker <--- AppKit status menu / hotkeys / Settings
           |
           +--> OBSRecordingBackend --> OBSController <--WebSocket--> OBS
           |            |                  |
           |            v                  +--> Webex window / display binding
           |       OBSMaintenance               Webex-Audio / Webex-Video scenes
           |
           +--> finalized RecordingSession
                        |
                        v
                PostProcessingWorker (FIFO background queue)
                        |
                        +--> Transcriber --> MLX Whisper + optional diarization
                        +--> WebexClient --> personal recipient or Webex room
                        +--> MediaCleaner (configured media retention)
```
