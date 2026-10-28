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
   your local `.env` file. For the base environment use `uv sync --locked`;
   the optional extra above adds neural diarization. Run commands in this guide
   use `--no-sync` and require dependencies to be installed beforehand.

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
6. Set **Recordings folder** in the companion settings (default `~/Movies/WebexRecordings`). The companion applies and verifies this directory in OBS before each recording start. Use OBS WebSocket 5.3+ to allow directory changes through the API.
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

### macOS permissions

OBS performs capture. Grant it **Screen Recording** (called **Screen & System
Audio Recording** on some macOS versions) and **Microphone** access for the
configured sources. The companion does not set up or verify audio routing.

The companion reads client window names through AppleScript **System Events**;
allow **Accessibility** and the prompted **Automation** access for the process
hosting Python (for example, your terminal during foreground runs). Global
hotkeys use `pynput` and may also require **Input Monitoring**. Permissions for
an interactive terminal do not establish access for a LaunchAgent. Use the
macOS prompt and System Settings to identify the requesting process.

Quartz window details may be restricted without Screen Recording access for the
companion's host. OBS's capturable-window list supplies a fallback for video
selection; this does not bypass capture permissions in OBS. Allow notifications
if you want delivery/error notices. Missing sensor access is reported as unknown,
not evidence that a call ended; stop manually if detection remains unknown.
Permission grants and actual audio/video quality require the manual checks below.

Window probes are skipped when process enumeration confirms the meeting app is
not running. For running apps, window warnings distinguish a two-second
AppleScript timeout, a nonzero exit (including a numeric AppleScript error code
when present), and a launch failure without including window titles or raw error
output. Repeated sensor failures warn at most once per minute per platform and
sensor during a continuous outage; a successful read resets that limit. A timeout
alone does not establish a permissions problem. For access errors, check the
hosting process's Accessibility and Automation access above. Failed reads remain
unknown even when repeated warnings are suppressed.

### OBS Lifecycle & Auto-Reconnection
- **Audio capture restart**: Before each manual or automatic audio/video recording start, including recovery segments and start retries, the companion requests **Restart Capture** for all macOS Audio Capture sources (`sck_audio_capture`) in OBS, regardless of source name or scene. Microphone and screen-capture sources are unaffected. OBS disables this button unless it has flagged capture as failed; rejected requests and enumeration failures are logged without blocking recording. An accepted request does not verify audio health. No new configuration is required. For a manual check, use synthetic audio in both recording modes, confirm both sides are audible, and check that a disabled button does not prevent recording; if a failed source is available, confirm its enabled button is requested before recording starts.
- **Startup check**: The service launches/connects OBS, verifies WebSocket connectivity, and leaves OBS running, including when exit-after-recording is enabled. If startup connectivity cannot be verified, a recording request retries the connection.
- **Recording**: By default, OBS stays open and connected between calls (`EXIT_OBS_ON_RECORDING_STOP=false`). Existing explicit true values and legacy `RELAUNCH_OBS_PER_CALL` values apply only after a recording stops: turn off **Exit OBS on recording stop** in Settings to enable keep-open behavior and idle maintenance between calls. OBS is prepared immediately on a manual recording request or strong audio/network call evidence, before slow window validation. Automatic recordings still require the existing window/attachment checks and two positive polls; meeting-title enrichment happens after recording starts.
- **Idle maintenance**: **Idle restart interval (minutes; 0 = off)** / `OBS_IDLE_RESTART_MINUTES` defaults to 60. This is a minimum OBS uptime, not a fixed wall-clock schedule. Maintenance waits for at least two minutes without potential call evidence and runs only during minutes 05–24 or 35–54 of the local hour, avoiding five minutes either side of :00 and :30. Recording, streaming, replay-buffer activity, unknown output status, or a pending recording request defer the restart. OBS is gracefully restarted and immediately reconnected; failures back off for two minutes. Maintenance applies only to local OBS in keep-open mode, and never runs from the active recording lifecycle. A request arriving during shutdown is handled after OBS reconnects. The timer is preventative, not an audio-health check.
- **Recovery**: During recording, the companion checks capture status and can start a new segment from a confirmed stopped output. After connection loss, an active output cannot safely be identified as its own: stop OBS directly, then retry the pending Stop action in the companion. Unobserved segment paths and the in-memory active-session ledger require manual recovery after a companion restart.
- **After Updating**: Pulling new source code does not reload an already-running LaunchAgent. Finish any active recording before running `uv run --locked --no-sync webex-obs start` from the repository to reload the service.
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
- **Delivery**: After a verified recording stop and complete transcription, the bot sends the transcript to the configured recipient or space. Partial and no-speech-only sessions do not trigger delivery.
- **AI Summary**: You can forward or paste the transcript directly into your 1:1 chat with the My Agent address configured as `MY_AGENT_EMAIL` to request executive summaries and action items. The companion does not automatically forward transcripts to My Agent or generate summaries.

### Space Delivery (Option 2)

Add the bot to the intended Webex space, run `uv run --locked --no-sync webex-obs list-rooms`, and
save its ID as `WEBEX_ROOM_ID` in `.env` or Settings. A nonempty room ID takes
precedence over `WEBEX_RECIPIENT_EMAIL`; clear it to return to personal delivery.
Transcripts shorter than 6,000 characters are sent inline; transcripts of 6,000
characters or more are attached as `.txt` files. A successful notification means
Webex returned HTTP 200/201, not that a person read the message or a summary was
generated. Network/server errors leave delivery uncertain; check Webex before
sending manually. Failed, blocked, and uncertain jobs retain media and do not
retry automatically. Changing delivery settings affects future jobs; already
queued jobs retain their settings and clients. Restart recovery holds pending
outbound jobs if delivery was disabled or the saved destination changed.

---

## 4. Configuration Wizard & Testing

### Step A: Run the Setup Wizard
The setup wizard reuses existing `.env` values as defaults, so you can press **Enter** to keep your current configuration:
```bash
uv run --locked --no-sync webex-obs setup
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
saves and applies changes without a service restart. Settings is nonmodal and can
remain open while you use other apps or recording controls. Reopening Settings
brings the existing window forward; **Cancel**, Escape, and the close button
discard unsaved edits. OBS connection changes
take effect after the current recording finishes. **Recordings folder** sets the
OBS save destination and retention folder automatically before recording. Changes
during a session apply to the next session; recovery segments and cleanup retain
the original folder. The folder is created and checked for local write access.
Recording is blocked if OBS rejects the directory or read-back verification fails;
check the service log, folder permissions, and WebSocket 5.3+ support. Existing
files are not moved. LaunchAgent
stdout/stderr log paths are fixed by the installed service definition.

Recording actions now live directly in the status menu. Only actions available
in the current state appear. Video source
selection still opens a separate picker, and **Stop & Discard…** opens a separate
confirmation with **No** as the default. Manual audio/video starts show a consent
notice while recording is already running: **OK** (default) or waiting 10 seconds
continues recording; **Cancel & Discard** deletes session segments only after a
positively verified stop, without transcription. An unresolved stop retains the
session and files. The automatic call-detection prompt retains its existing behavior.
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
uv run --locked --no-sync webex-obs prefetch-model
```

Each full repository name has its own hashed cache directory. Prefetch succeeds
only when `config.json` has the required model dimensions and
`weights.safetensors` passes header, tensor-layout, and file-length checks. It
exits nonzero for missing/corrupt required artifacts, including HTTP 404. The
MLX tokenizer is bundled, so repository tokenizer files are not downloaded.
Structural checks do not prove inference compatibility or detect every change
to weight data. Existing basename-only caches are retained but ignored; rerun
prefetch before offline use.

If logs show Hugging Face revision or `xet-read-token` requests, Whisper is using
the repository download path rather than the validated prefetch cache. These
requests concern model files, not meeting audio. The app disables Xet before
dependency imports to avoid that downloader's failure path. After a download
panic, run prefetch (with the configured repository argument for a nondefault
model) and restart after finishing any active recording. Confirm the next
startup logs `Using local pre-fetched Whisper model`. This prepares future
transcription; failed jobs remain protected and require manual recovery.

### Step C: Test 1:1 Webex Delivery
Send a verification message through your Webex Bot into your 1:1 Webex chat:
```bash
uv run --locked --no-sync webex-obs test-webex
```

---

## 5. Background Service Management

Install the companion daemon as a background macOS LaunchAgent (`com.cisco.webex-obs`):

```bash
# Install, start now, and auto-start at login
uv run --locked --no-sync webex-obs install-service

# Check service status
uv run --locked --no-sync webex-obs status

# Stream live background logs
uv run --locked --no-sync webex-obs logs

# Stop or Start anytime
uv run --locked --no-sync webex-obs stop
uv run --locked --no-sync webex-obs start
```

The menu bar's **Quit (stop service)** action is equivalent to stopping the
LaunchAgent for the current login session. Use `uv run --locked --no-sync webex-obs start` to
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

`RETENTION_DAYS=0` deletes that session's recordings immediately after complete
successful transcription, before any enabled Webex delivery; delivery failure
does not preserve these recordings. Transcripts are kept. The setup CLI and
Settings → Files and detection explain this zero-day behavior.
While recording with nonzero retention, select **Delete Recording After
Transcription** in the status menu to apply it to the current session only.
The checked option can be toggled off before stopping; it does not stop capture
or change Settings, and resets for the next session. It is hidden while idle
and whenever global retention is 0.

Positive values retain eligible media after transcription and enabled delivery
until its modification time is that many days old (default 14). Cleanup uses
persisted exact file identities in `.webex-obs-retention/`, never a media-folder
scan or matching filename pattern. Active, untranscribed, unrelated,
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
   own confirmed inactivity reaches the grace period. Also cancel a synthetic
   Webex recording, keep its evidence active for two polls, then inject one fully
   idle poll (supported Core Audio idle, no call window, no known UDP sockets)
   followed by two active polls inside 15 seconds. Confirm the next call gets a
   new recording and consent prompt, including when it has the same title.
   Repeat without cancellation and confirm the original recording stops with
   verification before the next starts. Partial evidence loss or failed reads
   must not trigger this shortcut. In an authorized test call on macOS, repeat
   the cancel/leave/rejoin sequence with Webex and verify actual detection and
   separate recording files; a gap entirely between polls may be missed.
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

### Diagnostic call-transition manual checks

Finish any active recording before relaunching with
`uv run --locked --no-sync webex-obs start --detection-diagnostics`.
Use synthetic test calls and the normal consent process.

- Verify the diagnostic confirmation has Yes / No / Not sure; let one expire
  after 30 seconds and close another. Neither should create a special report.
  Confirm detection/recording continues while the diagnostic window is open and
  normal consent and stop controls still work.
- Mark starts and ends from the status menu while idle and recording. Test Just
  now, a time 45 minutes ago, invalid input, and a time older than 60 minutes.
  Invalid input should keep the dialog open. Cancel should add no annotation.
  Confirm diagnostic panels appear near the upper-right, away from centered
  recording prompts. Check placement with multiple displays and a small screen.
- Leave a marker unreported, then mark a later event. No missing event should be
  inferred. Confirm marker event times differ from submission times for late entries.
  Matching markers and Yes answers should create no reports. Unmatched markers
  allow up to 60 seconds for detection, then save a dated report. No answers save
  suspected false starts; Not sure saves uncertain cases.
- Leave and immediately join another Webex test call, including one with the
  same title. Correlate manual markers with the report sensor context, automatic
  detection IDs, and confirmations. Recording boundaries remain controlled by
  the existing detector, never by diagnostic answers.
- Relaunch without the switch; confirm diagnostic menu items and popups disappear.
  Reports remain local under LOG_DIR/detection-reports. Verify no new continuous
  detection-diagnostics.jsonl is written; older files are left untouched. See README
  for the matching tolerance, context window, buffer limits, and partial coverage.
