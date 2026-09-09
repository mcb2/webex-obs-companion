# Webex OBS Companion

Automated background meeting recorder, Apple Silicon MLX Whisper transcriber, neural speaker diarizer, and Webex Bot transcript delivery daemon for macOS.

---

## Key Features

- **Automated Webex Meeting Detection**: Confirms sustained, call-specific Webex media activity before recording, while using window state to validate weaker signals from the general Webex app.
- **OBS Studio Automation & Audio Refresh**: Automatically starts/stops OBS recordings via WebSocket v5. Optional per-call restart prevents macOS CoreAudio buffer stalls during prolonged uptime.
- **Automatic Recording Recovery**: Verifies OBS recording state throughout a call and restarts/resumes in a new segment if OBS exits or capture stops.
- **Local Apple Silicon Hardware Transcription**: Fast, private speech-to-text powered by `mlx-whisper` (`whisper-large-v3-turbo`).
- **Neural & Acoustic Speaker Diarization**: Identifies speaker turns and labels participants chronologically.
- **Direct 1:1 Webex Bot Delivery**: Automatically delivers formatted meeting transcripts directly to your personal 1:1 Webex chat via a permanent Webex Bot token.
- **macOS LaunchAgent Daemon**: Runs silently in the background (`RunAtLoad` / `KeepAlive`) with auto-recovery.
- **Configurable Global Hotkeys**: Customize the Video, Menu, and Stop & Transcribe shortcuts in the setup wizard to avoid conflicts with other apps.
- **Descriptive Session Filenames**: Recording segments and transcripts include the call-start date, time, and a filesystem-safe version of the Webex call-window title.

---

## Quick Start

### 1. Installation
```bash
git clone <repo_url> webex_obs_companion
cd webex_obs_companion
uv venv
source .venv/bin/activate
uv sync --extra neural-diarization
brew install ffmpeg
```

### Hugging Face access for neural diarization

Neural speaker diarization requires a free Hugging Face account and read token:

1. Accept the conditions for
   [`pyannote/segmentation-3.0`](https://huggingface.co/pyannote/segmentation-3.0).
2. Accept the conditions for
   [`pyannote/speaker-diarization-3.1`](https://huggingface.co/pyannote/speaker-diarization-3.1).
3. Create a **Read** token at
   [Hugging Face Access Tokens](https://huggingface.co/settings/tokens).
4. Enter that token when `uv run webex-obs setup` prompts for it. The token is
   stored only in the local `.env` file.

A plain `uv sync` does not install the optional neural-diarization packages;
use `uv sync --extra neural-diarization` after updates.

### 2. Configuration Wizard
Run the interactive setup wizard (intelligently preserves existing `.env` values when re-run):
```bash
uv run webex-obs setup
```

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
| `uv run webex-obs install-service` | Install and start background LaunchAgent (`com.cisco.webex-obs.plist`) |
| `uv run webex-obs status` | Check whether the background LaunchAgent service is active |
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
are replaced, while the original Webex title is retained inside the transcript
and Webex delivery message.
