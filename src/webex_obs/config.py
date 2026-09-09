import os
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOTENV_PATH = PROJECT_ROOT / ".env"
HOME_DOTENV = Path.home() / ".webex_obs.env"

if DOTENV_PATH.exists():
    DEFAULT_ENV_FILE = str(DOTENV_PATH)
elif HOME_DOTENV.exists():
    DEFAULT_ENV_FILE = str(HOME_DOTENV)
else:
    DEFAULT_ENV_FILE = ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Webex settings (Permanent Bot Token)
    webex_access_token: str = Field(
        default="",
        validation_alias=AliasChoices("WEBEX_ACCESS_TOKEN", "webex_access_token", "WEBEX_BOT_TOKEN", "webex_bot_token", "WEBEX_TOKEN", "webex_token"),
        description="Permanent Webex Bot Access Token from developer.webex.com"
    )
    webex_recipient_email: str = Field(
        default="",
        validation_alias=AliasChoices(
            "WEBEX_RECIPIENT_EMAIL",
            "webex_recipient_email",
            "WEBEX_BOT_EMAIL",
            "webex_bot_email",
        ),
        description="Recipient email for direct 1:1 message delivery from your bot"
    )
    my_agent_email: str = Field(
        default="",
        validation_alias=AliasChoices("MY_AGENT_EMAIL", "my_agent_email"),
        description="Optional My Agent bot email shown in transcript delivery instructions"
    )
    webex_room_id: str = Field(
        default="",
        validation_alias=AliasChoices("WEBEX_ROOM_ID", "webex_room_id", "ROOM_ID", "room_id"),
        description="Optional Webex Space/Room ID for space-based delivery (leave blank for 1:1 DM)"
    )

    # OBS WebSocket settings
    obs_ws_host: str = Field(
        default="localhost",
        validation_alias=AliasChoices("OBS_WS_HOST", "obs_ws_host", "OBS_WS_ADDRESS", "obs_ws_address", "OBS_HOST", "obs_host"),
        description="OBS WebSocket host"
    )
    obs_ws_port: int = Field(
        default=4455,
        validation_alias=AliasChoices("OBS_WS_PORT", "obs_ws_port", "OBS_PORT", "obs_port"),
        description="OBS WebSocket port"
    )
    obs_ws_password: str = Field(
        default="",
        validation_alias=AliasChoices("OBS_WS_PASSWORD", "obs_ws_password", "OBS_PASSWORD", "obs_password"),
        description="OBS WebSocket password"
    )
    relaunch_obs_per_call: bool = Field(
        default=True,
        validation_alias=AliasChoices("RELAUNCH_OBS_PER_CALL", "relaunch_obs_per_call", "RELAUNCH_OBS", "relaunch_obs"),
        description="Gracefully restart OBS Studio on each call to prevent macOS CoreAudio / ScreenCaptureKit capture stall"
    )

    # Transcription & Diarization settings
    whisper_model: str = Field(
        default="mlx-community/whisper-large-v3-turbo",
        validation_alias=AliasChoices("WHISPER_MODEL", "whisper_model"),
        description="Local MLX Whisper model name"
    )
    enable_diarization: bool = Field(
        default=True,
        validation_alias=AliasChoices("ENABLE_DIARIZATION", "enable_diarization"),
        description="Enable neural speaker diarization (identifying speaker turns)"
    )
    hf_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("HF_TOKEN", "hf_token", "HUGGING_FACE_HUB_TOKEN", "hugging_face_hub_token"),
        description="Optional Hugging Face token for gated pyannote diarization models"
    )

    # Path settings
    recordings_dir: Path = Field(
        default_factory=lambda: Path.home() / "Movies" / "WebexRecordings",
        validation_alias=AliasChoices("RECORDINGS_DIR", "recordings_dir"),
        description="Directory where OBS saves recordings"
    )
    transcripts_dir: Path = Field(
        default_factory=lambda: Path.home() / "Documents" / "WebexTranscripts",
        validation_alias=AliasChoices("TRANSCRIPTS_DIR", "transcripts_dir"),
        description="Directory where text transcripts are stored"
    )
    log_dir: Path = Field(
        default_factory=lambda: Path.home() / "Library" / "Logs" / "WebexOBS",
        validation_alias=AliasChoices("LOG_DIR", "log_dir"),
        description="Directory for application and service logs"
    )

    # Retention settings
    retention_days: int = Field(
        default=14,
        validation_alias=AliasChoices("RETENTION_DAYS", "retention_days"),
        description="Days to keep raw media files before auto-pruning"
    )

    # Polling settings
    poll_interval: float = Field(
        default=3.0,
        validation_alias=AliasChoices("POLL_INTERVAL", "poll_interval"),
        description="Process polling interval in seconds"
    )

    # Global hotkeys (pynput GlobalHotKeys syntax)
    hotkey_video: str = Field(
        default="<cmd>+<shift>+v",
        validation_alias=AliasChoices("HOTKEY_VIDEO", "hotkey_video"),
        description="Switch an active recording to video mode",
    )
    hotkey_menu: str = Field(
        default="<cmd>+<shift>+r",
        validation_alias=AliasChoices("HOTKEY_MENU", "hotkey_menu"),
        description="Show the recording control menu",
    )
    hotkey_stop_transcribe: str = Field(
        default="<cmd>+<shift>+s",
        validation_alias=AliasChoices(
            "HOTKEY_STOP_TRANSCRIBE", "hotkey_stop_transcribe"
        ),
        description="Stop recording and begin transcription",
    )

    @field_validator("hotkey_video", "hotkey_menu", "hotkey_stop_transcribe")
    @classmethod
    def normalize_hotkey(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("hotkey cannot be empty")
        return value

    @model_validator(mode="after")
    def require_distinct_hotkeys(self):
        hotkeys = (self.hotkey_video, self.hotkey_menu, self.hotkey_stop_transcribe)
        if len(set(hotkeys)) != len(hotkeys):
            raise ValueError(
                "HOTKEY_VIDEO, HOTKEY_MENU, and HOTKEY_STOP_TRANSCRIBE must be different"
            )
        return self

    @field_validator("recordings_dir", "transcripts_dir", "log_dir", mode="before")
    @classmethod
    def expand_path(cls, v):
        if isinstance(v, (str, Path)):
            # Do not touch the filesystem while loading configuration. Cloud-backed
            # folders (notably OneDrive on macOS) can temporarily raise EDEADLK and
            # used to crash the LaunchAgent before monitoring even started.
            return Path(os.path.expanduser(str(v))).resolve()
        return v

    # Compatibility properties & aliases
    @property
    def webex_token(self) -> str:
        return self.webex_access_token

    @property
    def webex_bot_email(self) -> str:
        """Backward-compatible attribute for the direct-delivery recipient."""
        return self.webex_recipient_email

    @property
    def room_id(self) -> str:
        return self.webex_room_id

    @property
    def obs_address(self) -> str:
        return self.obs_ws_host

    @property
    def obs_port(self) -> int:
        return self.obs_ws_port

    @property
    def obs_password(self) -> str:
        return self.obs_ws_password

    @classmethod
    def load(cls) -> "Settings":
        return cls()


Config = Settings
settings = Settings()
