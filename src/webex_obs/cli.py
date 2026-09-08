import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import requests
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

# Ensure Homebrew and standard binary paths are in PATH
extra_paths = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")]
current_path = os.environ.get("PATH", "")
for p in extra_paths:
    if p not in current_path and os.path.exists(p):
        current_path = f"{p}:{current_path}"
os.environ["PATH"] = current_path

# Disable fast transfer CAS client that fails on corporate CDNs / expired signed URLs
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"

# Ensure root certificates are injected
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

try:
    import certifi
    if "SSL_CERT_FILE" not in os.environ:
        os.environ["SSL_CERT_FILE"] = certifi.where()
    if "REQUESTS_CA_BUNDLE" not in os.environ:
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
except Exception:
    pass

app = typer.Typer(help="Webex OBS Companion CLI management")
console = Console()

PLIST_NAME = "com.cisco.webex-obs.plist"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / PLIST_NAME
MODELS_CACHE_DIR = Path.home() / ".cache" / "webex_obs" / "models"


def _check_ffmpeg() -> bool:
    """Pre-flight check for ffmpeg executable."""
    if not shutil.which("ffmpeg"):
        console.print("[bold red]⚠ ffmpeg is missing on your Mac![/bold red]")
        console.print("MLX Whisper requires ffmpeg to decode audio from recordings.")
        console.print("Install it via Homebrew by running:\n  [bold cyan]brew install ffmpeg[/bold cyan]\n")
        return False
    return True


def _stream_download_file(url: str, dest_path: Path, token: str | None = None) -> bool:
    """Download a file with streaming chunks and a live progress bar."""
    headers = {"User-Agent": "webex-obs-companion/0.2.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

    try:
        with requests.get(url, headers=headers, stream=True, timeout=30) as response:
            if response.status_code == 404:
                return False  # Optional file does not exist in repo
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0))

            with open(temp_path, "wb") as f, Progress(
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(f"Downloading {dest_path.name}", total=total_size if total_size > 0 else None)
                for chunk in response.iter_content(chunk_size=1024 * 1024):  # 1 MB chunks
                    if chunk:
                        f.write(chunk)
                        progress.update(task, advance=len(chunk))

        temp_path.replace(dest_path)
        return True
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise e


@app.command()
def test_webex():
    """Send a test message to verify Webex Bot delivery and token permissions."""
    from webex_obs.config import settings
    from webex_obs.webex_client import WebexClient

    console.print("[bold blue]=== Testing Webex Bot Delivery ===[/bold blue]")
    if not settings.webex_token:
        console.print("[bold red]Error:[/bold red] WEBEX_ACCESS_TOKEN is not configured in .env.")
        return

    client = WebexClient(
        settings.webex_token,
        settings.webex_recipient_email,
        room_id=settings.room_id,
        my_agent_email=settings.my_agent_email,
    )
    target_info = (
        f"Space (roomId: {settings.room_id})"
        if settings.room_id
        else f"1:1 Direct Message to {settings.webex_recipient_email}"
    )
    console.print(f"Target: [cyan]{target_info}[/cyan]\n")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(
            "# Webex OBS Companion — Test Delivery\n\n"
            "This is a test transcript delivery to verify Webex Bot 1:1 permissions.\n"
            "If you see this message in your 1:1 Webex chat, your Webex Bot token is valid and active!"
        )
        tf_path = Path(tf.name)

    try:
        success = client.send_transcript(tf_path, meeting_title="Test Connection")
        if success:
            console.print("[bold green]✓ Test message delivered successfully to Webex![/bold green]")
        else:
            console.print("[bold red]✗ Delivery failed.[/bold red] Check error details above.")
    finally:
        if tf_path.exists():
            tf_path.unlink()


@app.command()
def list_rooms():
    """List all Webex Spaces/Rooms your Bot has been added to."""
    from webex_obs.config import settings
    from webex_obs.webex_client import WebexClient

    if not settings.webex_token:
        console.print("[bold red]Error:[/bold red] WEBEX_ACCESS_TOKEN is missing in .env.")
        return

    client = WebexClient(settings.webex_token)
    rooms = client.list_rooms()

    if not rooms:
        console.print("[yellow]No spaces found where this Bot is a member.[/yellow]")
        console.print("To post to a shared space, invite your Bot (e.g. mbahler-bot@webex.bot) to the space in Webex.")
        return

    table = Table(title="Webex Spaces Joined by Bot")
    table.add_column("Space Title", style="cyan")
    table.add_column("Room ID (copy for WEBEX_ROOM_ID in .env)", style="green")
    table.add_column("Type", style="dim")

    for r in rooms:
        table.add_row(r.get("title", "Direct"), r.get("id", ""), r.get("type", ""))

    console.print(table)


@app.command()
def prefetch_model(
    model_name: str = "mlx-community/whisper-large-v3-turbo",
    hf_token: str = typer.Option(None, "--token", "-t", help="Optional Hugging Face access token"),
):
    """Directly download and cache Whisper model weights locally."""
    _check_ffmpeg()

    model_slug = Path(model_name).name
    target_dir = MODELS_CACHE_DIR / model_slug
    target_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold blue]Directly fetching MLX Whisper model '{model_name}'...[/bold blue]")
    console.print(f"Target local directory: [cyan]{target_dir}[/cyan]\n")

    repo_files = [
        "config.json",
        "weights.safetensors",
        "tokenizer.json",
        "preprocessor_config.json",
        "special_tokens_map.json",
        "vocab.json",
        "merges.txt",
        "normalizer.json",
    ]

    base_url = f"https://huggingface.co/{model_name}/resolve/main"

    for idx, fname in enumerate(repo_files, start=1):
        dest = target_dir / fname
        if dest.exists() and dest.stat().st_size > 0:
            console.print(f"[{idx}/{len(repo_files)}] [green]✓ Already cached:[/green] {fname} ({dest.stat().st_size:,} bytes)")
            continue

        url = f"{base_url}/{fname}"
        try:
            downloaded = _stream_download_file(url, dest, token=hf_token)
            if downloaded:
                console.print(f"[{idx}/{len(repo_files)}] [green]✓ Downloaded:[/green] {fname}")
        except Exception as e:
            if fname == "weights.safetensors":
                console.print(f"[bold red]Failed to download {fname}:[/bold red] {e}")
                console.print(f"\n[yellow]Direct curl fallback command:[/yellow]")
                console.print(f"  curl -L -o \"{dest}\" \"{url}\"")
                return

    console.print(f"\n[bold green]✓ Whisper model successfully pre-fetched and cached locally![/bold green]")
    console.print(f"Cached at: [cyan]{target_dir}[/cyan]")


@app.command()
def setup():
    """Run interactive setup wizard to configure .env with intelligent defaults."""
    console.print("[bold blue]=== Webex OBS Companion Setup Wizard ===[/bold blue]\n")

    _check_ffmpeg()

    project_root = Path(__file__).resolve().parent.parent.parent
    env_file = project_root / ".env"

    # Default configuration values
    existing_token = ""
    existing_recipient_email = ""
    existing_my_agent_email = ""
    existing_room_id = ""
    existing_obs_port = "4455"
    existing_obs_password = ""
    existing_relaunch_obs = True
    existing_whisper_model = "mlx-community/whisper-large-v3-turbo"
    existing_enable_diarize = True
    existing_hf_token = ""
    existing_recordings_dir = str(Path.home() / "Movies" / "WebexRecordings")
    existing_transcripts_dir = str(Path.home() / "Documents" / "WebexTranscripts")
    existing_retention_days = "14"

    if env_file.exists():
        console.print(f"[bold green]Found existing configuration at {env_file}.[/bold green]")
        console.print("[dim]Press Enter on any prompt to keep its current value.[/dim]\n")
        try:
            from webex_obs.config import Settings
            current = Settings()
            if current.webex_access_token:
                existing_token = current.webex_access_token
            if current.webex_recipient_email:
                existing_recipient_email = current.webex_recipient_email
            if current.my_agent_email:
                existing_my_agent_email = current.my_agent_email
            if current.webex_room_id:
                existing_room_id = current.webex_room_id
            if current.obs_ws_port:
                existing_obs_port = str(current.obs_ws_port)
            if current.obs_ws_password:
                existing_obs_password = current.obs_ws_password
            existing_relaunch_obs = current.relaunch_obs_per_call
            if current.whisper_model:
                existing_whisper_model = current.whisper_model
            existing_enable_diarize = current.enable_diarization
            if current.hf_token:
                existing_hf_token = current.hf_token
            if current.recordings_dir:
                existing_recordings_dir = str(current.recordings_dir)
            if current.transcripts_dir:
                existing_transcripts_dir = str(current.transcripts_dir)
            if current.retention_days:
                existing_retention_days = str(current.retention_days)
        except Exception:
            pass

    console.print("[bold]1. Webex Bot Configuration[/bold]")
    console.print("Create a permanent bot at: [cyan]https://developer.webex.com/my-apps[/cyan]\n")

    token = Prompt.ask("Enter your Webex Bot Access Token", default=existing_token if existing_token else None)
    recipient_email = Prompt.ask(
        "Enter your Webex email for 1:1 direct delivery (Option 1)",
        default=existing_recipient_email,
    )
    my_agent_email = Prompt.ask(
        "Enter your My Agent bot email (optional)",
        default=existing_my_agent_email,
    )
    room_id = Prompt.ask("Optional Webex Space/Room ID (leave blank for Option 1 direct 1:1 DM)", default=existing_room_id)

    console.print("\n[bold]2. OBS Studio WebSocket Configuration[/bold]")
    obs_port = Prompt.ask("Enter OBS WebSocket port", default=existing_obs_port)
    obs_password = Prompt.ask("Enter OBS WebSocket password", default=existing_obs_password, password=True)
    relaunch_obs = Confirm.ask("Restart OBS on each call start to guarantee clean system audio capture?", default=existing_relaunch_obs)

    console.print("\n[bold]3. Transcription & Speaker Diarization[/bold]")
    whisper_model = Prompt.ask("Enter Whisper model", default=existing_whisper_model)
    enable_diarize = Confirm.ask("Enable Neural Speaker Diarization (identifying speaker turns)?", default=existing_enable_diarize)
    hf_token = ""
    if enable_diarize:
        hf_token = Prompt.ask("Optional Hugging Face token for pyannote models (leave blank for local acoustic engine)", default=existing_hf_token)

    console.print("\n[bold]4. File & Storage Paths[/bold]")
    recordings_dir = Prompt.ask("Recordings Directory (where OBS saves)", default=existing_recordings_dir)
    transcripts_dir = Prompt.ask("Transcripts Directory (where meeting text is stored)", default=existing_transcripts_dir)
    retention_days = Prompt.ask("Retention Days (for auto-pruning raw recordings)", default=existing_retention_days)

    env_content = f"""# Webex OBS Companion Configuration
# Permanent Webex Bot Access Token (developer.webex.com)
WEBEX_ACCESS_TOKEN={token}
WEBEX_RECIPIENT_EMAIL={recipient_email}
MY_AGENT_EMAIL={my_agent_email}
WEBEX_ROOM_ID={room_id}

# OBS Studio WebSocket
OBS_WS_HOST=localhost
OBS_WS_PORT={obs_port}
OBS_WS_PASSWORD={obs_password}
RELAUNCH_OBS_PER_CALL={'true' if relaunch_obs else 'false'}

# Transcription & Neural Diarization
WHISPER_MODEL={whisper_model}
ENABLE_DIARIZATION={'true' if enable_diarize else 'false'}
HF_TOKEN={hf_token}

# File & Storage Paths
RECORDINGS_DIR={recordings_dir}
TRANSCRIPTS_DIR={transcripts_dir}
RETENTION_DAYS={retention_days}
POLL_INTERVAL=3.0
"""
    with open(env_file, "w") as f:
        f.write(env_content)

    console.print(f"\n[bold green]Configuration successfully saved to {env_file}![/bold green]")
    console.print("[cyan]Tip: Test 1:1 delivery anytime with: [bold]uv run webex-obs test-webex[/bold][/cyan]")


@app.command()
def run():
    """Run the companion daemon in the foreground (interactive / debug mode)."""
    _check_ffmpeg()
    from webex_obs.daemon import main as daemon_main
    daemon_main()


@app.command()
def install_service():
    """Install and load the background daemon as a macOS LaunchAgent."""
    _check_ffmpeg()
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    project_root = Path(__file__).resolve().parent.parent.parent

    venv_python = sys.executable
    log_dir = Path.home() / "Library" / "Logs" / "WebexOBS"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"

    env_path = f"/opt/homebrew/bin:/usr/local/bin:{os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}"

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cisco.webex-obs</string>
    <key>WorkingDirectory</key>
    <string>{project_root}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{venv_python}</string>
        <string>-u</string>
        <string>-m</string>
        <string>webex_obs.daemon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>PATH</key>
        <string>{env_path}</string>
        <key>HF_HUB_ENABLE_HF_TRANSFER</key>
        <string>0</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
</dict>
</plist>
"""
    with open(PLIST_PATH, "w") as f:
        f.write(plist_content)

    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    res = subprocess.run(["launchctl", "load", str(PLIST_PATH)], capture_output=True, text=True)

    if res.returncode == 0:
        console.print(f"[bold green]Successfully installed and started LaunchAgent: {PLIST_PATH}[/bold green]")
        console.print(f"Logs: {stdout_log}")
    else:
        console.print(f"[bold red]Failed to load LaunchAgent:[/bold red] {res.stderr}")


@app.command()
def start():
    """Start the background LaunchAgent service."""
    if not PLIST_PATH.exists():
        console.print("[yellow]Service not installed yet. Running install-service first...[/yellow]")
        install_service()
        return

    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    res = subprocess.run(["launchctl", "load", str(PLIST_PATH)], capture_output=True, text=True)
    if res.returncode == 0:
        console.print("[bold green]Background service started successfully.[/bold green]")
    else:
        console.print(f"[bold red]Failed to start service:[/bold red] {res.stderr}")


@app.command()
def stop():
    """Stop the background LaunchAgent service."""
    if not PLIST_PATH.exists():
        console.print("[yellow]Service is not installed.[/yellow]")
        return

    res = subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    if res.returncode == 0:
        console.print("[bold green]Background service stopped successfully.[/bold green]")
    else:
        console.print(f"[bold red]Failed to stop service:[/bold red] {res.stderr}")


@app.command()
def status():
    """Check the status of the background LaunchAgent service."""
    res = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    for line in res.stdout.splitlines():
        if "com.cisco.webex-obs" in line:
            console.print(f"[bold green]Service is ACTIVE:[/bold green] {line}")
            return
    console.print("[yellow]Service is currently INACTIVE / not loaded.[/yellow]")


@app.command()
def logs():
    """Tail the background daemon logs."""
    stdout_log = Path.home() / "Library" / "Logs" / "WebexOBS" / "stdout.log"
    if not stdout_log.exists():
        console.print(f"[yellow]Log file does not exist yet at {stdout_log}[/yellow]")
        return

    console.print(f"[bold blue]Streaming logs from {stdout_log} (Press Ctrl+C to exit)...[/bold blue]\n")
    try:
        subprocess.run(["tail", "-f", str(stdout_log)])
    except KeyboardInterrupt:
        pass


def main():
    """Entrypoint function for CLI script."""
    app()


if __name__ == "__main__":
    main()
