"""Validated, atomic updates to the existing dotenv configuration."""

import os
import shutil
import tempfile
from pathlib import Path

from dotenv import set_key

from .config import Config, DEFAULT_ENV_FILE
from .hotkey_listener import validate_hotkeys


# Keep credentials in the setup wizard for now; avoid ever displaying them in UI.
EDITABLE = {
    "obs_ws_host": "OBS_WS_HOST",
    "obs_ws_port": "OBS_WS_PORT",
    "relaunch_obs_per_call": "RELAUNCH_OBS_PER_CALL",
    "recordings_dir": "RECORDINGS_DIR",
    "transcripts_dir": "TRANSCRIPTS_DIR",
    "retention_days": "RETENTION_DAYS",
    "call_end_grace_seconds": "CALL_END_GRACE_SECONDS",
    "enable_diarization": "ENABLE_DIARIZATION",
    "hotkey_video": "HOTKEY_VIDEO",
    "hotkey_menu": "HOTKEY_MENU",
    "hotkey_stop_transcribe": "HOTKEY_STOP_TRANSCRIBE",
}


def save_settings(values: dict[str, str | bool], path: Path | None = None) -> None:
    """Validate the complete effective configuration before replacing the file."""
    path = Path(path or DEFAULT_ENV_FILE).expanduser().resolve()
    unknown = set(values) - EDITABLE.keys()
    if unknown:
        raise ValueError(f"Unsupported settings: {', '.join(sorted(unknown))}")
    current = Config(_env_file=path)
    proposed = current.model_dump()
    proposed.update(values)
    # Explicit field-name population prevents aliases from excluding proposed values.
    validated = Config.model_validate(proposed)
    validate_hotkeys(
        validated.hotkey_video, validated.hotkey_menu, validated.hotkey_stop_transcribe
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".webex-obs-", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        if path.exists():
            shutil.copyfile(path, temp)
            os.chmod(temp, path.stat().st_mode & 0o777)
        else:
            os.chmod(temp, 0o600)
        for field, value in values.items():
            set_key(str(temp), EDITABLE[field], str(value).lower() if isinstance(value, bool) else str(value), quote_mode="always")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
