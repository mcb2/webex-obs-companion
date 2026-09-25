"""Validated, atomic updates to the existing dotenv configuration."""

import os
import shutil
import tempfile
from pathlib import Path

from dotenv import set_key

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
    "relaunch_obs_per_call": "RELAUNCH_OBS_PER_CALL",
    "recordings_dir": "RECORDINGS_DIR",
    "transcripts_dir": "TRANSCRIPTS_DIR",
    "retention_days": "RETENTION_DAYS",
    "call_end_grace_seconds": "CALL_END_GRACE_SECONDS",
    "enable_diarization": "ENABLE_DIARIZATION",
    "whisper_model": "WHISPER_MODEL",
    "hf_token": "HF_TOKEN",
    "poll_interval": "POLL_INTERVAL",
    "hotkey_video": "HOTKEY_VIDEO",
    "hotkey_menu": "HOTKEY_MENU",
    "hotkey_stop_transcribe": "HOTKEY_STOP_TRANSCRIBE",
}


def validate_settings(values: dict[str, str | bool], current: Config) -> Config:
    """Validate edited fields against the currently running configuration."""
    unknown = set(values) - EDITABLE.keys()
    if unknown:
        raise ValueError(f"Unsupported settings: {', '.join(sorted(unknown))}")
    proposed = current.model_dump()
    proposed.update(values)
    validated = Config.model_validate(proposed)
    validate_hotkeys(
        validated.hotkey_video, validated.hotkey_menu, validated.hotkey_stop_transcribe
    )
    if validated.enable_diarization and not validated.hf_token and (
        not current.enable_diarization or ("hf_token" in values and not values["hf_token"])
    ):
        raise ValueError("Hugging Face token is required when diarization is enabled")
    return validated


def save_settings(values: dict[str, str | bool | Path | None], path: Path | None = None, current: Config | None = None) -> Config:
    """Validate the complete effective configuration before replacing the file."""
    path = Path(path or DEFAULT_ENV_FILE).expanduser().resolve()
    validated = validate_settings(values, current or Config(_env_file=path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".webex-obs-", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        if path.exists():
            shutil.copyfile(path, temp)
            os.chmod(temp, path.stat().st_mode & 0o700)
        else:
            os.chmod(temp, 0o600)
        for field, value in values.items():
            formatted = "" if value is None else (str(value).lower() if isinstance(value, bool) else str(value))
            set_key(str(temp), EDITABLE[field], formatted, quote_mode="always")
        os.replace(temp, path)
        return validated
    finally:
        temp.unlink(missing_ok=True)
