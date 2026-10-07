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
the same OBS instance before attempting another per-call restart.
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
Capture source to an OBS-listed full display; window polling is suspended until
a Webex window is selected again. This selection applies to the current
session only because macOS window IDs change when Webex recreates windows.

```
+-------------------------------------------------------------+
|                     macOS User Session                      |
|                                                             |
|  +---------------------+      +--------------------------+  |
|  | Meeting Clients     |      |        OBS Studio        |  |
|  | Webex/Zoom/Teams    |      |  - WebSocket Server 4455 |  |
|  +----------+----------+      |  - Scene: Webex-Audio    |  |
|             |                 |  - Scene: Webex-Video    |  |
|             v                 +-------------+------------+  |
|     [ Process Polling ]                     ^               |
|             |                               | WebSocket v5  |
|             v                               v               |
|  +-------------------------------------------------------+  |
|  |            webex_obs Background Daemon                |  |
|  |                                                       |  |
|  |  - ProcessMonitor: Tracks Webex/Zoom/Teams lifecycles |  |
|  |  - OBSController: Starts/stops audio & video captures |  |
|  |  - WindowBinder: Quartz dynamic Webex window binding  |  |
|  |  - HotkeyListener: Cmd+Shift+V for instant video swap |  |
|  |  - Transcriber: Local Apple MLX Whisper pipeline     |  |
|  |  - WebexClient: Posts transcripts to My Agent space   |  |
|  |  - MediaCleaner: 14-day rolling media retention       |  |
|  +--------------------------+----------------------------+  |
|                             |                               |
|                             v                               |
|              +------------------------------+               |
|              | Apple MLX Whisper (Local GPU)|               |
|              +--------------+---------------+               |
+-----------------------------|-------------------------------+
                              v HTTPS API
               +------------------------------+
               |     Cisco Webex Platform     |
               |      (My Agent 1:1 Bot)      |
               +------------------------------+
```
