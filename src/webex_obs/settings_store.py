"""Validated, atomic updates to the existing dotenv configuration."""

import io
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from dotenv.parser import parse_stream

from .config import Config, DEFAULT_ENV_FILE
from .hotkey_listener import validate_hotkeys


EDITABLE = {
    "webex_delivery_enabled": "WEBEX_DELIVERY_ENABLED",
    "webex_access_token": "WEBEX_ACCESS_TOKEN",
    "webex_recipient_email": "WEBEX_RECIPIENT_EMAIL",
    "my_agent_email": "MY_AGENT_EMAIL",
    "webex_room_id": "WEBEX_ROOM_ID",
    "obs_ws_host": "OBS_WS_HOST",
    "obs_ws_port": "OBS_WS_PORT",
    "obs_ws_password": "OBS_WS_PASSWORD",
    "exit_obs_on_recording_stop": "EXIT_OBS_ON_RECORDING_STOP",
    "obs_idle_restart_minutes": "OBS_IDLE_RESTART_MINUTES",
    "shared_window_behavior": "SHARED_WINDOW_BEHAVIOR",
    "recordings_dir": "RECORDINGS_DIR",
    "transcripts_dir": "TRANSCRIPTS_DIR",
    "retention_days": "RETENTION_DAYS",
    "call_end_grace_seconds": "CALL_END_GRACE_SECONDS",
    "enable_diarization": "ENABLE_DIARIZATION",
    "whisper_model": "WHISPER_MODEL",
    "hf_token": "HF_TOKEN",
    "poll_interval": "POLL_INTERVAL",
    "hotkey_audio": "HOTKEY_AUDIO",
    "hotkey_video": "HOTKEY_VIDEO",
    "hotkey_video_source": "HOTKEY_VIDEO_SOURCE",
    "hotkey_stop_transcribe": "HOTKEY_STOP_TRANSCRIBE",
}


class SettingsRollbackError(RuntimeError):
    """The previous settings could not be fully restored."""


def _update_dotenv(text: str, values: dict) -> str:
    """Replace assignments while keeping comments, aliases and unknown lines."""
    edits = {}
    for field, value in values.items():
        formatted = "" if value is None else (str(value).lower() if isinstance(value, bool) else str(value))
        escaped = formatted.replace("\\", "\\\\").replace("'", "\\'")
        edits[EDITABLE[field]] = f"'{escaped}'"
    output = []
    replaced = set()
    for binding in parse_stream(io.StringIO(text)):
        raw = binding.original.string
        if binding.key in edits and not binding.error:
            prefix = re.match(r"\s*(?:export\s+)?[^\s=]+\s*=[ \t]*", raw).end()
            remainder = raw[prefix:]
            if remainder.startswith(("'", '"')):
                quoted = re.match(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", remainder, re.DOTALL)
                end = quoted.end()
            else:
                comment = re.search(r"\s+#", remainder)
                end = comment.start() if comment else len(remainder.rstrip())
            raw = raw[:prefix] + edits[binding.key] + remainder[end:]
            replaced.add(binding.key)
        output.append(raw)
    result = "".join(output)
    for key in edits:
        if key in replaced:
            continue
        if result and not result.endswith("\n"):
            result += "\n"
        result += f"{key}={edits[key]}\n"
    return result


def validate_settings(values: dict[str, str | bool | int | float | Path | None], current: Config) -> Config:
    """Validate edited fields against the currently running configuration."""
    unknown = set(values) - EDITABLE.keys()
    if unknown:
        raise ValueError(f"Unsupported settings: {', '.join(sorted(unknown))}")
    proposed = current.model_dump()
    proposed.update(values)
    validated = Config.model_validate(proposed)
    validate_hotkeys(
        validated.hotkey_audio, validated.hotkey_video, validated.hotkey_video_source, validated.hotkey_stop_transcribe
    )
    if validated.enable_diarization and not validated.hf_token and (
        not current.enable_diarization or ("hf_token" in values and not values["hf_token"])
    ):
        raise ValueError("Hugging Face token is required when diarization is enabled")
    return validated


def save_settings(
    values: dict[str, str | bool | int | float | Path | None],
    path: Path | None = None,
    current: Config | None = None,
    *,
    apply: Callable[[Config], None] | None = None,
) -> Config:
    """Validate and atomically save edits, restoring exact bytes if application fails.

    The caller serializes transactions. The apply callback must roll back its
    runtime components before raising; no AppKit work belongs in this callback.
    """
    path = Path(path or DEFAULT_ENV_FILE).expanduser().resolve()
    validated = validate_settings(values, current or Config(_env_file=path))
    original = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".webex-obs-", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        text = original.decode("utf-8") if original is not None else ""
        with temp.open("w", encoding="utf-8", newline="") as stream:
            stream.write(_update_dotenv(text, values))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        if apply is not None:
            try:
                apply(validated)
            except Exception:
                try:
                    if original is None:
                        path.unlink()
                    else:
                        # The first replacement moved temp away. Recreate it
                        # privately before writing any original credentials.
                        restore_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(restore_fd, "wb") as stream:
                            stream.write(original)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temp, path)
                except OSError:
                    raise SettingsRollbackError("Could not restore the previous settings file.") from None
                raise
        return validated
    finally:
        temp.unlink(missing_ok=True)
