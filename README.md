# Webex OBS Companion

Automated background meeting recorder, Apple Silicon MLX Whisper transcriber, neural speaker diarizer, and Webex Bot transcript delivery daemon for macOS.

---

## Key Features

- **Automated Meeting Detection**: Detects Webex, Zoom, and Microsoft Teams calls from sustained, app-specific media activity and macOS window state before recording.
- **OBS Studio Automation & Audio Refresh**: Automatically starts/stops OBS recordings via WebSocket v5. OBS stays open between calls by default. Guarded idle restarts refresh long-running OBS instances; optional exit after recording is also available.
- **Automatic Recording Recovery**: Verifies OBS recording state throughout a call and restarts/resumes in a new segment if OBS exits or capture stops.
- **Local Apple Silicon Hardware Transcription**: Fast, private speech-to-text powered by `mlx-whisper` (`whisper-large-v3-turbo`).
- **Neural & Acoustic Speaker Diarization**: Assigns anonymous speaker labels to speech turns. Neural diarization falls back to a rough two-group acoustic classification when unavailable; labels do not identify participant names.
- **Direct 1:1 Webex Bot Delivery**: Automatically delivers formatted meeting transcripts directly to your personal 1:1 Webex chat via a permanent Webex Bot token.
- **macOS LaunchAgent Daemon**: Runs silently in the background (`RunAtLoad` / `KeepAlive`) with auto-recovery.
- **Configurable Global Hotkeys**: Customize Start Audio (Cmd+Shift+A), Start / Switch Video (Cmd+Shift+V), Select Video Source (Cmd+Shift+R), and Stop Recording / Transcribe (Cmd+Shift+S) in Settings or the setup wizard. Source selection is available only while recording video; starting audio while already recording and switching to video while already recording video do nothing. Existing `HOTKEY_MENU` values are used as the source shortcut until `HOTKEY_VIDEO_SOURCE` is saved.
- **Live Video Source Selection**: Choose whether a newly opened Webex window is selected automatically or offered in a picker during video recording. The video-only menu command lets you select a Webex window or an entire screen at any time; screen capture pauses automatic window following.
- **macOS Status Menu**: The menu icon shows no badge while idle, a microphone badge for audio recording, and a camera badge for video recording. Provides recording controls, Stop & Transcribe while recording, and Settings. Its tooltip shows the recording mode and current meeting; the automatic recording confirmation retains the 15-second audio default and consent notice.
- Recording controls are directly in the status menu. Idle: Start Audio Recording, Start Video Recording, Settings, and Quit. Recording audio: Switch to Video, Stop & Transcribe, Stop & Discard, and Settings. Recording video: Select Video Source, Stop & Transcribe, Stop & Discard, and Settings. Unavailable actions are hidden, never grayed out. Source selection remains a separate window-or-screen picker; Stop & Discard requires a separate confirmation defaulting to No.
- Manual audio/video starts (including the Video hotkey while idle) show a two-party/all-party consent reminder after recording begins. **OK** is the default; the notice dismisses after 10 seconds and keeps recording. **Cancel & Discard** stops the session and deletes its segments only after OBS confirms recording has stopped, without transcription. The existing automatically detected-call prompt remains unchanged.
- **Descriptive Session Filenames**: Recording segments and transcripts include the call-start date, time, and a filesystem-safe version of the detected call-window title.

Automatic detection currently targets the native macOS desktop clients. It
combines a matching call window with per-process Core Audio input/output state.
This works independently of whether the call uses UDP, dynamic peer-to-peer
ports, or TCP-only media. Known UDP media sockets provide an additional signal
and a fallback on macOS versions without per-process audio state. Dynamic OBS
video-window binding remains Webex-specific.
The `Webex-Video` scene needs a macOS Screen Capture source named
`Webex-Meeting-Window` configured for Window Capture. Switching its window
while recording keeps the same OBS recording active.

Recording changes are serialized through the lifecycle worker. Repeated video
switch requests coalesce, and a pending stop cancels queued switches. OBS must
accept a scene or source change before the app reports it as successful.
Source enumeration and OBS calls run off the AppKit main thread; Settings queues
OBS updates without waiting for capture operations.

Window and screen choices apply only to the current session. After OBS confirms
the recording has stopped, the companion resets a selected full-screen source
to window capture. If that reset fails, the next video start or audio-to-video
switch requires an accepted reset or a successful Webex-window binding. Recovery
segments within an unfinished session retain its source selection.

---

## Quick Start

### 1. Installation

Requires macOS on Apple Silicon, Python 3.11+, OBS Studio 28+ with WebSocket
enabled, and `uv`. The current MLX transcription backend requires Apple Silicon.
Configure the OBS scenes and audio sources described in [SETUP.md](SETUP.md).

```bash
git clone https://github.com/mcb2/webex-obs-companion.git webex_obs_companion
cd webex_obs_companion
uv venv
source .venv/bin/activate
uv sync --locked --extra neural-diarization
brew install ffmpeg
```

### Hugging Face access for neural diarization

Neural speaker diarization requires a free Hugging Face account and read token:

1. Accept the conditions for
   [`pyannote/speaker-diarization-community-1`](https://huggingface.co/pyannote/speaker-diarization-community-1).
2. Create a **Read** token at
   [Hugging Face Access Tokens](https://huggingface.co/settings/tokens).
3. Enter that token when `uv run webex-obs setup` prompts for it. The token is
   stored only in the local `.env` file.

A plain `uv sync` does not install the optional neural-diarization packages;
use `uv sync --locked --extra neural-diarization` after updates.

The locked pyannote.audio 4 pipeline uses Community-1; access to the older 3.1
models does not grant access to Community-1. Its weights are downloaded on first
use, then inference runs locally. Hugging Face and pyannote telemetry are
disabled before speech dependencies are imported. Extracted PCM audio is passed
in memory, so diarization does not need TorchCodec file decoding. `ffmpeg` is
still required to extract audio from recordings. If neural initialization or
inference fails, the acoustic speaker-label fallback remains available.

### 2. Configuration Wizard
Run the interactive setup wizard (intelligently preserves existing `.env` values when re-run):
```bash
uv run webex-obs setup
```

Open **Settings…** from the menu-bar icon to edit delivery credentials, OBS
connection, transcription, detection, and shortcuts. Changes are saved and
applied when you click **OK**, without restarting the service. An active or
unresolved recording retains its OBS connection until a stop is positively verified.
Idle connection changes are applied on the next recording preparation/start,
after the old OBS output is confirmed stopped. OBS still performs all
capture: update its recording output path in OBS too if you change the
recordings folder here. Tokens and passwords are masked in Settings.
Turn off **Deliver transcripts to Webex** to retain transcripts locally without
sending them to the bot; recording and transcription continue as usual.
**Quit (stop service)** in the menu bar unloads the LaunchAgent so it stays
closed for the current login session. Start it again with `uv run webex-obs start`;
the installed LaunchAgent also starts at the next login.

OBS idle maintenance defaults to a minimum uptime of 60 minutes
(`OBS_IDLE_RESTART_MINUTES`; 0 disables it). Restarts require at least two quiet
minutes and a safe time window away from :00 and :30, with no active recording,
stream, or replay buffer. `EXIT_OBS_ON_RECORDING_STOP=true` instead exits OBS
after a verified recording stop and disables idle maintenance.

Delivery can also target a Webex space using `WEBEX_ROOM_ID`, which takes
precedence over the recipient email; the bot must belong to that space.
Transcripts shorter than 6,000 characters are included in the message; longer
transcripts are attached as text files. My Agent summaries require manually
forwarding or pasting the transcript; the app does not generate summaries.

### 3. Model Pre-fetching & Webex Testing
```bash
# Pre-cache Apple Silicon MLX model weights locally
uv run webex-obs prefetch-model

# Test Webex Bot 1:1 message delivery
uv run webex-obs test-webex
```

### 4. Background Service Management
```bash
# Install and launch as macOS background daemon
uv run webex-obs install-service

# Check running status
uv run webex-obs status

# Stream live background logs
uv run webex-obs logs
```

---

## CLI Reference

| Command | Description |
|---|---|
| `uv run webex-obs setup` | Interactive `.env` wizard with automatic defaults preservation |
| `uv run webex-obs test-webex` | Send an instant test message to verify Webex Bot delivery |
| `uv run webex-obs list-rooms` | List all Webex spaces your Bot belongs to along with their `Room ID` |
| `uv run webex-obs prefetch-model` | Stream download and cache Whisper model weights to `~/.cache/webex_obs/` |
| `uv run webex-obs diagnose-calls` | Show process, window, Core Audio, and UDP evidence for each supported meeting app |
| `uv run webex-obs install-service` | Install and start background LaunchAgent (`com.cisco.webex-obs.plist`) |
| `uv run webex-obs status` | Report LaunchAgent load state and application lifecycle health |
| `uv run webex-obs logs` | Live stream `stdout.log` and `stderr.log` from `~/Library/Logs/WebexOBS/` |
| `uv run webex-obs start` / `stop` | Start or stop the background service |
| `uv run webex-obs run` | Run daemon in foreground (interactive debug mode) |

---

## Storage Paths

- **Meeting Transcripts**: `~/Documents/WebexTranscripts/`
- **OBS Media Recordings**: `~/Movies/` or `~/Movies/WebexRecordings/`
- **Service Logs**: `~/Library/Logs/WebexOBS/stdout.log`
- **Model Weights**: `~/.cache/webex_obs/models/whisper-large-v3-turbo/`

Completed files use names such as
`2026-09-09_12-03-26 - Cloud and AI Weekly Sync.txt`. If OBS produces
multiple recording segments, the media files receive `part-01`, `part-02`,
and subsequent suffixes. Characters that do not work reliably in filenames
are replaced, while the original meeting title is retained inside the transcript
and Webex delivery message.

If OBS stop cannot be verified, the app keeps the current session, segment
paths, source selection, connection, and pending discard/transcribe action.
It does not rename, delete, or queue that media, or start another session.
Check OBS, then retry **Stop & Transcribe** (or **Stop & Discard**) from the menu;
a retry keeps the original pending discard choice. There is no automatic stop
retry. Unknown status is treated separately from a confirmed stopped output.

The app leaves independently started OBS recordings untouched. A successful
StartRecord response establishes ownership; an already active output or a lost
start response does not. After connection loss, an active output's identity is
uncertain, so the app leaves it running. Stop it directly in OBS, then retry
Stop in the app to finalize only already known segments. OBS does not provide
a stable recording ID, and [WebSocket v5 `GetRecordStatus`](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md#getrecordstatus)
has no media path.
The companion tracks accepted `StopRecord` paths and stopped
`RecordStateChanged` events. While it owns an active recording, it also reads
the exact local file target of OBS Studio's built-in recording output through
`GetOutputList` / `GetOutputSettings`. This preserves known recovery segments
when OBS exits, a stop response is interrupted, or a stop event is missed.
WebSocket 5.5+ file-split events retain subsequent paths too. After a verified
stop, completed paths are returned once, in session order. Reconciliation uses
only observed paths; recent files or similar names never establish ownership.
Third-party outputs may not expose a supported local file target. Files whose
paths were never observed (including missed split events) remain on disk for
manual recovery. Tracking survives OBS restarts within the running companion
session; it does not persist across companion restarts.
An independent stop/restart entirely between observations may be undetectable
when OBS supplies neither a changed path nor a decreased output duration.

`RETENTION_DAYS` defaults to 14 and uses media modification time. Cleanup runs
at startup and after successful processing, using file identity records in
`.webex-obs-retention/` inside the configured recordings directory. Only media
from verified completed app sessions, with successful transcription and any
enabled Webex delivery, becomes eligible. **0 deletes eligible media immediately
after success**. Active, queued, failed, recoverable, unrelated, and legacy media
without these records is retained. Modified/replaced files and symlinks are
excluded. Missing/failed segments retain all session media; a partial transcript
may remain locally for recovery and is not delivered automatically. Recordings
written outside the configured folder are never enrolled in its cleanup.
Failed sessions require manual recovery. Interrupted safe work resumes from durable
checkpoints as described below.

## Shutdown and restart recovery

Quit, SIGINT, SIGTERM, and application termination request the same shutdown.
New starts stop being accepted. The lifecycle worker attempts one verified stop
of an active owned recording and saves a processing job; an already unresolved
stop is left for explicit resolution in OBS. Shutdown does not discard media.
The app waits up to **10 seconds** for lifecycle cleanup, including up to **2
seconds** for current processing. Queued work is deferred to restart. A stuck
backend is never stopped concurrently; timeout exits with an error and leaves
OBS/media available for manual recovery. Menu Quit still requires an idle
recording session and unloads the service after cleanup (the launchctl calls
have a further combined 15-second timeout).

Startup and lifecycle failures are logged, show an error in the menu while
exiting, and produce a nonzero exit status so launchd can restart the process.
`status` reports whether the LaunchAgent is loaded separately from whether its
PID has a fresh running lifecycle heartbeat. A loaded job with no PID, a failed
startup, or a stale/missing heartbeat is not reported as healthy. Long blocked
operations can make the heartbeat stale; this is a responsiveness check, not a
capture/audio-quality guarantee.

Private JSON checkpoints live in `LOG_DIR/jobs/` (default
`~/Library/Logs/WebexOBS/jobs/`), with files mode 0600 and directory mode 0700.
They contain sensitive titles, local paths, destination settings, and exact media
identities, but no tokens or passwords. Transcripts are written to a temporary
file, flushed, and atomically replaced; a partially written file never becomes
the committed transcript. Durable jobs append a job ID to the transcript name
so interrupted work can reuse its own path without overwriting another job.

On restart, `queued` and interrupted `transcribing` jobs resume local processing;
`ready` jobs reuse their complete transcript. `delivered` jobs only finalize
retention and never send again. `complete` checkpoints remain as completion
records. Recovery uses the saved model/destination and current credentials.
Disabled delivery or a changed destination holds pending outbound work as
`blocked`; it does not silently send or declare that work complete.

The `delivering` checkpoint is saved **before** making a network request. If
termination interrupts that request, or Webex returns a network/server error,
the job becomes `uncertain` and is never resent automatically. Check Webex and
the local transcript before deciding whether to send manually. `failed`,
`blocked`, and `uncertain` jobs retain their files and require manual review;
there is no automatic retry loop or retry command. Do not reset an uncertain
checkpoint to a sendable state without checking for an existing message.

Keep `LOG_DIR` stable across restarts. To relocate state, stop the app fully and
move its `jobs/` directory before starting with the new path. A local file lock
prevents two instances using the same state directory from processing jobs.
SIGKILL/power loss cannot run cleanup: already checkpointed jobs recover, while
active OBS capture and its in-memory segment ledger require manual recovery.
Files remain on disk if termination occurs before the verified stop/job checkpoint,
or if storage is unavailable; these files are not automatically adopted.

## Development checks

With the existing development environment, run `uv run --locked --no-sync pytest`.
`--no-sync` prevents test execution from installing or updating dependencies.
The tests replace inference, audio/process/window probes, network requests, and
service commands with scoped doubles; they do not require model weights or a
running OBS instance. AppKit tests require macOS and skip when AppKit is absent.
Real capture, permissions, audio routing, and model inference still require
separate checks on a suitable Mac.
