# Webex OBS Companion — Setup & Operations Guide

Follow this guide to configure and run the Webex OBS Companion on macOS.

---

## 1. Prerequisites

1. **macOS on Apple Silicon (M1/M2/M3/M4)** or Intel.
2. **OBS Studio** (v28+ with OBS WebSocket enabled).
3. **ffmpeg**: Required for audio extraction & decoding.
   ```bash
   brew install ffmpeg
   ```
4. **Python package manager (`uv`)**:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

---

## 2. OBS Studio WebSocket Setup

1. Open **OBS Studio**.
2. Go to **Tools ➔ WebSocket Server Settings**.
3. Check **Enable WebSocket server**.
4. Set Server Port to `4455`.
5. Check **Enable Authentication** and set a password (or copy the generated password).
6. Set your recording save path in **Settings ➔ Output ➔ Recording ➔ Recording Path** (e.g. `~/Movies` or `~/Movies/WebexRecordings`).

### Automatic OBS Audio Reset & Auto-Reconnection
- **Audio Capture Refresh**: macOS CoreAudio / ScreenCaptureKit can occasionally freeze audio buffers if OBS has been running continuously for days. The companion defaults to `RELAUNCH_OBS_PER_CALL=true` which cleanly restarts OBS when each call starts to guarantee fresh audio capture.
- **Auto-Reconnection**: The daemon constantly monitors WebSocket health. If you stop or restart OBS manually, the companion automatically reconnects without needing a service restart.
- **After Updating**: Pulling new source code does not reload an already-running LaunchAgent. Run `uv run webex-obs start` from the repository after every update so the service uses the new code.

---

## 3. Webex Bot Setup (Option 1: Direct 1:1 Delivery)

Webex Bots provide **permanent access tokens** that never expire (unlike personal user tokens that expire every 12 hours).

1. Go to [developer.webex.com/my-apps](https://developer.webex.com/my-apps) and click **Create a New App ➔ Create a Bot**.
2. Name your bot (e.g. `My Meeting Companion`) and pick a username (e.g. `mark-meeting-bot@webex.bot`).
3. Copy the generated **Bot Access Token**.

### Direct 1:1 Delivery Configuration
In `~/Projects/webex_obs_companion/.env`:
```ini
WEBEX_ACCESS_TOKEN=your_bot_token_here
WEBEX_RECIPIENT_EMAIL=your_webex_email@example.com
MY_AGENT_EMAIL=your_agent@webex.bot
WEBEX_ROOM_ID=
```
- **How it Works**: When a meeting ends, your bot sends the full transcript directly to your personal 1:1 Webex chat.
- **AI Summary**: You can forward or paste the transcript directly into your 1:1 chat with the My Agent address configured as `MY_AGENT_EMAIL` for instant executive summaries and action items.

---

## 4. Configuration Wizard & Testing

### Step A: Run the Setup Wizard
The setup wizard reuses existing `.env` values as defaults, so you can press **Enter** to keep your current configuration:
```bash
uv run webex-obs setup
```

The wizard also configures the three global keyboard shortcuts. They use `pynput`
syntax, such as `<cmd>+<shift>+v`. Restart the background service after changing
them so the new shortcuts are registered.

### Step B: Pre-fetch Local Whisper Models
Download and cache the Apple Silicon MLX Whisper model weights locally:
```bash
uv run webex-obs prefetch-model
```

### Step C: Test 1:1 Webex Delivery
Send a verification message through your Webex Bot into your 1:1 Webex chat:
```bash
uv run webex-obs test-webex
```

---

## 5. Background Service Management

Install the companion daemon as a background macOS LaunchAgent (`com.cisco.webex-obs`):

```bash
# Install and auto-start on boot
uv run webex-obs install-service

# Check service status
uv run webex-obs status

# Stream live background logs
uv run webex-obs logs

# Stop or Start anytime
uv run webex-obs stop
uv run webex-obs start
```

---

## 6. Directory Structure & Files

- **Text Transcripts**: `~/Documents/WebexTranscripts/meeting_YYYY-MM-DD_HH-MM-SS.txt`
- **OBS Media Recordings**: `~/Movies/` or `~/Movies/WebexRecordings/`
- **Background Service Logs**: `~/Library/Logs/WebexOBS/stdout.log`
- **Local Model Cache**: `~/.cache/webex_obs/models/`
