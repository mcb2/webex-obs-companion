# Webex OBS Companion

Automated background meeting recorder, Apple Silicon MLX Whisper transcriber, neural speaker diarizer, and Webex Bot transcript delivery daemon for macOS.

---

## Key Features

- **Automated Webex Meeting Detection**: Uses low-overhead RTP UDP socket polling and floating window state detection to trigger recording only during active meetings.
- **OBS Studio Automation & Audio Refresh**: Automatically starts/stops OBS recordings via WebSocket v5. Optional per-call restart prevents macOS CoreAudio buffer stalls during prolonged uptime.
- **Automatic WebSocket Reconnection**: Seamlessly reconnects whenever OBS is restarted manually or automatically.
- **Local Apple Silicon Hardware Transcription**: Fast, private speech-to-text powered by `mlx-whisper` (`whisper-large-v3-turbo`).
- **Neural & Acoustic Speaker Diarization**: Identifies speaker turns and labels participants chronologically.
- **Direct 1:1 Webex Bot Delivery**: Automatically delivers formatted meeting transcripts directly to your personal 1:1 Webex chat via a permanent Webex Bot token.
- **macOS LaunchAgent Daemon**: Runs silently in the background (`RunAtLoad` / `KeepAlive`) with auto-recovery.

---

## Quick Start

### 1. Installation
```bash
git clone <repo_url> webex_obs_companion
cd webex_obs_companion
uv venv
source .venv/bin/activate
uv pip install -e .
brew install ffmpeg
```

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
