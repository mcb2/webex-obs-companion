# Webex OBS Companion — Setup & Operations Guide

Follow this guide to configure and run the Webex OBS Companion on macOS.

---

## 1. Prerequisites

1. **macOS on Apple Silicon**. The current MLX transcription backend requires Apple Silicon; Intel Macs are not supported by this backend.
   **Python 3.11+** is required.
2. **OBS Studio** (v28+ with OBS WebSocket enabled).
3. **ffmpeg**: Required for audio extraction & decoding.
   ```bash
   brew install ffmpeg
   ```
4. **Python package manager (`uv`)**:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
5. **Neural speaker diarization dependencies** (required when
   `ENABLE_DIARIZATION=true`):
   ```bash
   uv sync --locked --extra neural-diarization
   ```
   Neural diarization also requires a free Hugging Face account. Complete both
   access steps before running the setup wizard:

   1. Accept the conditions for
      [`pyannote/speaker-diarization-community-1`](https://huggingface.co/pyannote/speaker-diarization-community-1).
   2. Create a **Read** token at
      [Hugging Face Access Tokens](https://huggingface.co/settings/tokens).

   The setup wizard prompts for this token and saves it as `HF_TOKEN` only in
   your local `.env` file. A plain `uv sync` does not install the optional
   neural-diarization dependency group.

   Community-1 is the local pipeline supported by the locked pyannote.audio 4
   API. Its agreement is separate from the older 3.1 model conditions, and its
   weights must be cached on first use before offline inference is possible.
   Hugging Face and pyannote telemetry are disabled at application import time.
   The app supplies normalized mono PCM audio in memory, avoiding TorchCodec's
   file-decoder/shared-library requirements. `ffmpeg` remains necessary for
   extraction. Missing dependencies, inaccessible models, and inference failures
   use the existing acoustic fallback. Speaker labels remain anonymous.

---

## 2. OBS Studio WebSocket Setup

1. Open **OBS Studio**.
2. Go to **Tools ➔ WebSocket Server Settings**.
3. Check **Enable WebSocket server**.
4. Set Server Port to `4455`.
5. Check **Enable Authentication** and set a password (or copy the generated password).
6. Set your recording save path in **Settings ➔ Output ➔ Recording ➔ Recording Path** (e.g. `~/Movies` or `~/Movies/WebexRecordings`).
7. Create scenes named exactly **Webex-Audio** and **Webex-Video**. The companion switches between these names and does not create the scenes for you.
8. Configure microphone and meeting/system-audio capture in both scenes, using OBS audio sources appropriate to your macOS version. Verify both sides of a test conversation are audible in an OBS recording; the companion controls recording but does not configure audio routing.
9. In `Webex-Video`, add a **macOS Screen Capture** source named exactly `Webex-Meeting-Window` and set its method to **Window Capture**. Grant OBS Screen Recording permission in macOS Settings.

In the companion's **Settings… ➔ OBS recording**, set **New Webex window (video only)** to
**Prompt me (recommended)** (the default) or **Always switch**. When a new Webex window
appears during video recording, the companion offers or switches only when it
looks like a persistent meeting or shared-content window. Brief chat previews
and small pop-ups are ignored while the selected window remains available. The
**Select Webex window or screen…** menu-bar command is visible only during video
recording. It lets you select a Webex window or an entire screen without stopping
the recording. Entire-screen capture pauses automatic Webex-window switching and
prompts until you select a window again. The screen choices come from macOS and
require a macOS Screen Capture source; the legacy Window Capture source supports
window choices only. Window titles and IDs depend on what
Webex exposes to macOS; content rendered inside the meeting window stays
part of that window.

### OBS Lifecycle & Auto-Reconnection
- **Startup check**: The service launches/connects OBS, verifies WebSocket connectivity, and leaves OBS running, including when exit-after-recording is enabled. If startup connectivity cannot be verified, a recording request retries the connection.
- **Recording**: Service startup always launches/connects OBS and leaves it running; there is no startup shutdown cycle, even with a legacy exit-after-recording preference enabled. By default, OBS also stays open and connected between calls (`EXIT_OBS_ON_RECORDING_STOP=false`). Existing explicit true values and legacy `RELAUNCH_OBS_PER_CALL` values apply only after a recording stops: turn off **Exit OBS on recording stop** in Settings to enable keep-open behavior and idle maintenance between calls. OBS is prepared immediately on a manual recording request or strong audio/network call evidence, before slow window validation. Automatic recordings still require the existing window/attachment checks and two positive polls; meeting-title enrichment happens after recording starts.
- **Idle maintenance**: **Idle restart interval (minutes; 0 = off)** / `OBS_IDLE_RESTART_MINUTES` defaults to 60. This is a minimum OBS uptime, not a fixed wall-clock schedule. Maintenance waits for at least two minutes without potential call evidence and runs only during minutes 05–24 or 35–54 of the local hour, avoiding five minutes either side of :00 and :30. Recording, streaming, replay-buffer activity, unknown output status, or a pending recording request defer the restart. OBS is gracefully restarted and immediately reconnected; failures back off for two minutes. Maintenance applies only to local OBS in keep-open mode, and never runs from the active recording lifecycle. A request arriving during shutdown is handled after OBS reconnects. The timer is preventative, not an audio-health check.
- **Auto-Reconnection**: The daemon constantly monitors WebSocket health. If you stop or restart OBS manually, the companion automatically reconnects without needing a service restart.
- **After Updating**: Pulling new source code does not reload an already-running LaunchAgent. Run `uv run webex-obs start` from the repository after every update so the service uses the new code.
- **Supported meeting apps**: Automatic call start/stop detection supports the macOS desktop apps for Webex, Zoom, and Microsoft Teams. Browser-based meetings are not detected automatically.
- **Transport-independent detection**: Webex, Zoom, and Teams can use a non-idle call window plus per-process Core Audio input/output even with TCP-only or dynamic-port media. Unsupported Core Audio requires known UDP plus a window. Webex output-only also requires a non-attachment window. See the [per-platform requirements](README.md). `uv run --locked --no-sync webex-obs diagnose-calls` shows process, window, audio, and UDP evidence; failed reads display “Read failed (unknown).”
- **Call-end grace period**: A transient loss of meeting window or media-socket evidence does not immediately stop a recording. `CALL_END_GRACE_SECONDS` defaults to 15 seconds of confirmed continuous inactivity on the originating platform. Unknown sensor reads reset this timer and retain the recording; persistent failures require manual Stop. Overlapping platforms cannot extend or rename the recording. Prompt declines remain suppressed per platform through title changes and sensor failures.

---

## 3. Webex Bot Setup (Option 1: Direct 1:1 Delivery)

Webex Bots provide **permanent access tokens** that never expire (unlike personal user tokens that expire every 12 hours).

1. Go to [developer.webex.com/my-apps](https://developer.webex.com/my-apps) and click **Create a New App ➔ Create a Bot**.
2. Name your bot (e.g. `My Meeting Companion`) and pick a username (e.g. `mark-meeting-bot@webex.bot`).
3. Copy the generated **Bot Access Token**.

### Direct 1:1 Delivery Configuration
In the repository’s `.env` file:
```ini
WEBEX_DELIVERY_ENABLED=true
WEBEX_ACCESS_TOKEN=your_bot_token_here
WEBEX_RECIPIENT_EMAIL=your_webex_email@example.com
MY_AGENT_EMAIL=your_agent@webex.bot
WEBEX_ROOM_ID=
```
- Set `WEBEX_DELIVERY_ENABLED=false`, or turn off **Deliver transcripts to Webex**
  in Settings, to save transcripts locally without automatically sending them.
- **How it Works**: When a meeting ends, your bot sends the full transcript directly to your personal 1:1 Webex chat.
- **AI Summary**: You can forward or paste the transcript directly into your 1:1 chat with the My Agent address configured as `MY_AGENT_EMAIL` to request executive summaries and action items. The companion does not automatically forward transcripts to My Agent or generate summaries.

### Space Delivery (Option 2)

Add the bot to the intended Webex space, run `uv run webex-obs list-rooms`, and
save its ID as `WEBEX_ROOM_ID` in `.env` or Settings. A nonempty room ID takes
precedence over `WEBEX_RECIPIENT_EMAIL`; clear it to return to personal delivery.
Transcripts shorter than 6,000 characters are sent inline; longer transcripts
are attached as `.txt` files.

---

## 4. Configuration Wizard & Testing

### Step A: Run the Setup Wizard
The setup wizard reuses existing `.env` values as defaults, so you can press **Enter** to keep your current configuration:
```bash
uv run webex-obs setup
```

Webex tokens and OBS passwords use hidden input and never display existing
secret defaults. Press **Enter** to retain them. Secret entry requires a terminal
that supports hidden input; setup aborts if input would be echoed.
Setup creates or replaces `.env`
atomically with owner-only read/write permissions (`0600`); Settings saves also
enforce these permissions.

When neural diarization is enabled, the wizard requires a Hugging Face read
token. It displays the token and model-access links above, masks newly entered
tokens, and can retain an existing token without displaying it.

The wizard also configures four global keyboard shortcuts. They use `pynput`
syntax, such as `<cmd>+<shift>+v`. Restart the background service after changing
them through the wizard so the new shortcuts are registered. Settings-dialog
changes apply immediately. Defaults: Start Audio Cmd+Shift+A (`HOTKEY_AUDIO`),
Start / Switch Video Cmd+Shift+V (`HOTKEY_VIDEO`), Select Video Source Cmd+Shift+R
(`HOTKEY_VIDEO_SOURCE`), and Stop Recording / Transcribe Cmd+Shift+S
(`HOTKEY_STOP_TRANSCRIBE`). The old `HOTKEY_MENU` value is accepted as a legacy
source-selection shortcut until the new setting is saved; there is no Menu hotkey.

The macOS menu-bar icon shows recording status and offers controls and a
**Settings…** dialog for the configuration offered by setup, including masked
passwords and tokens, plus OBS host and polling interval. Clicking **OK**
saves and applies changes without a service restart. OBS connection changes
take effect after the current recording finishes. Keep the OBS recording
output path aligned with the recordings folder you select here. LaunchAgent
stdout/stderr log paths are fixed by the installed service definition.

Recording actions now live directly in the status menu. Only actions available
in the current state appear. Video source
selection still opens a separate picker, and **Stop & Discard…** opens a separate
confirmation with **No** as the default. Manual audio/video starts show a consent
notice while recording is already running: **OK** (default) or waiting 10 seconds
continues recording; **Cancel & Discard** stops and deletes the session without
transcription. The automatic call-detection prompt retains its existing behavior.
The Settings sections are OBS recording, Transcription, Files and detection,
Keyboard shortcuts, and finally Webex delivery.

Setup and native Settings use the same validated atomic writer. Rerunning setup
retains unedited settings, comments, compatibility aliases, and zero values.
New configurations use application defaults, including keeping OBS open. Dotenv
values are literal: `${HOME}` is not interpolated; use `~` or an absolute folder
path. Settings persistence and runtime application run in a background worker.
Failures restore the original file and changed runtime components; rollback
errors are reported explicitly and require a restart after recording finishes.

### Step B: Pre-fetch Local Whisper Models
Download and cache the Apple Silicon MLX Whisper model weights locally:
```bash
uv run webex-obs prefetch-model
```

Each full repository name has its own hashed cache directory. Prefetch succeeds
only when `config.json` has the required model dimensions and
`weights.safetensors` passes header, tensor-layout, and file-length checks. It
exits nonzero for missing/corrupt required artifacts, including HTTP 404. The
MLX tokenizer is bundled, so repository tokenizer files are not downloaded.
Structural checks do not prove inference compatibility or detect every change
to weight data. Existing basename-only caches are retained but ignored; rerun
prefetch before offline use.

### Step C: Test 1:1 Webex Delivery
Send a verification message through your Webex Bot into your 1:1 Webex chat:
```bash
uv run webex-obs test-webex
```

---

## 5. Background Service Management

Install the companion daemon as a background macOS LaunchAgent (`com.cisco.webex-obs`):

```bash
# Install, start now, and auto-start at login
uv run webex-obs install-service

# Check service status
uv run webex-obs status

# Stream live background logs
uv run webex-obs logs

# Stop or Start anytime
uv run webex-obs stop
uv run webex-obs start
```

The menu bar's **Quit (stop service)** action is equivalent to stopping the
LaunchAgent for the current login session. Use `uv run webex-obs start` to
resume it; the installed service starts again on the next login. Quit and SIGTERM
coordinate recording finalization and retain pending processing jobs. Cleanup
has a 10-second application deadline, with at most 2 seconds waiting for current
processing; Quit then unloads launchd using calls bounded to 15 seconds combined.
If capture stop is unresolved or shutdown times out, check OBS directly; media
is preserved. `status` reports LaunchAgent registration and application heartbeat
health separately. See [shutdown and recovery](README.md#shutdown-and-restart-recovery)
for checkpoint states and ambiguous delivery handling.

---

## 6. Directory Structure & Files

- **Text Transcripts**: `~/Documents/WebexTranscripts/YYYY-MM-DD_HH-MM-SS - Meeting Title - JobID.txt`
- **OBS Media Recordings**: `~/Movies/` or `~/Movies/WebexRecordings/`
- **Private Processing Checkpoints**: `LOG_DIR/jobs/` (default `~/Library/Logs/WebexOBS/jobs/`); keep this path stable across restarts.
- **Background Service Logs**: `~/Library/Logs/WebexOBS/stdout.log`
- **Local Model Cache**: `~/.cache/webex_obs/models/`

Session filenames use the call-start date/time and a filesystem-safe meeting
title. Multi-segment recordings receive `part-01`, `part-02`, and subsequent
suffixes; the original title is retained inside the transcript.

Speaker labels are anonymous. If neural diarization cannot load or run, the app
uses a rough two-group acoustic fallback; it does not identify participant names.
Completed sessions are transcribed and delivered by a background queue so call
monitoring can continue while processing the previous meeting.

### Safe completion and retention

OBS must explicitly report `outputActive=false` before session completion or
discard. If stop fails or status is unknown, files and session state remain
intact, connection changes stay pending, and new app recordings are blocked.
Check OBS and retry the pending Stop action from the menu. The original discard
choice is retained. After a lost connection, stop the ambiguous recording in
OBS first; the app cannot safely identify an active output after reconnecting.
Independently started OBS recordings are left untouched.

Recovery retains exact paths observed from OBS's built-in recording output
settings, stop responses, and recording events. A verified stop returns all
known segments once in session order, even after OBS restarts during the session.
`GetRecordStatus` itself has no file path. Files whose paths were never observed
remain on disk for manual recovery; recent files are never adopted by scanning
the recording directory. Custom output plugins and missing file-split events
may require manual recovery. Segment tracking does not survive restarting the
companion itself.

`RETENTION_DAYS=0` deletes only eligible media immediately after successful
transcription and any enabled delivery. Positive values retain eligible media
until its modification time is that many days old (default 14). Cleanup uses
persisted exact file identities in `.webex-obs-retention/`, never a media-folder
scan or matching filename pattern. Active, queued, failed, recoverable, unrelated,
and legacy media without success records stays on disk. Missing segments or
partial transcription preserve all session media for manual recovery. Changing
or deleting the retention index disables cleanup of those files; it does not
cause legacy files to be enrolled. Keep OBS's output directory aligned with
`RECORDINGS_DIR`; files outside it are not cleaned automatically.


## macOS manual-check plan

These checks are a plan, not evidence of real-device validation. Do not record
real calls, run the installed service, send Webex messages, or use personal media.
Use an isolated checkout/test process, temporary recordings/transcripts/state,
delivery disabled, idle maintenance disabled, and an injected recording backend
that only writes synthetic test files. Keep AppKit on the main thread.

1. **Idle sensor inspection:** with meeting clients idle, run
   `uv run --locked --no-sync webex-obs diagnose-calls`. Confirm home windows and
   idle audio do not constitute a call. Check a separate test process with
   Accessibility/Automation access withheld: failed window reads must display
   unknown, while unsupported Core Audio displays the UDP fallback. Do not
   change permissions for the installed companion.
2. **Synthetic lifecycle and consent:** inject the sensor sequences exercised in
   `tests/test_process_monitor.py` into the isolated process. For each platform,
   use audio plus a synthetic call title with TCP-only and dynamic UDP evidence,
   then muted input and output-only states. Confirm capture begins only after
   two positive polls for that platform and the automatic consent prompt keeps
   its existing timeout/default/cancel behavior. Confirm the manual-start consent
   reminder still appears immediately after the fake backend starts.
3. **Overlap and suppression:** start a synthetic Zoom session, activate synthetic
   Webex/Teams evidence, then end Zoom. Confirm the title stays Zoom and Stop
   occurs after Zoom's grace period. Decline both synthetic Webex and Zoom calls;
   confirm title changes and platform switches do not reprompt either until its
   own confirmed inactivity reaches the grace period.
4. **Failure and stop safety:** inject socket denial, AppleScript timeout, and
   Core Audio translation/property failures. Confirm unknown evidence does not
   start a session, stop an active session, or clear a decline. Restore sensors
   and verify a full new grace period. Make the fake backend return an
   unverified stop: the platform binding, pending action, and synthetic files
   must remain until a verified stop retry.
5. **Partial transcription:** use only generated test clips and local cached
   models, with no downloads. Remove a temporary part and inject an inference
   exception on another part. Inspect the incomplete banner, count, gap order,
   and missing/failed filenames; confirm no outbound request or retention
   eligibility. Restore the part and retry locally; the completed transcript
   must no longer contain partial markers. Inspect MLX/diarization quality
   separately if models/hardware are available.

6. **Settings responsiveness and rollback:** in the isolated test process, inject
   a delayed/failing settings component and change a shortcut plus detector/OBS
   settings. Confirm the menu and window still respond during application. On
   failure, confirm the original file bytes and runtime settings return, with an
   alert on the main thread. During synthetic capture, confirm connection edits
   stay deferred until a verified stop. Check zero values, quoted paths, and
   legacy keys survive setup and Settings round trips.

The mocked suite validates these state transitions without OBS or model inference.
Actual app window naming, Core Audio behavior, transport fallback, permissions,
and AppKit presentation still require this manual validation; synthetic sensor
injection cannot establish the clients' real-device behavior.
