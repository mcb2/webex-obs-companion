import datetime
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MEETING_TITLE = "Webex Session"
MAX_FILENAME_TITLE_BYTES = 100
SAFE_PUNCTUATION = {"-", "_", ".", "(", ")", "&", "'", ","}


def normalize_display_title(title: str | None) -> str:
    """Return a single-line meeting title suitable for UI and transcript text."""
    if not title:
        return DEFAULT_MEETING_TITLE
    normalized = unicodedata.normalize("NFKC", title)
    normalized = "".join(
        " " if char.isspace() else char
        for char in normalized
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or DEFAULT_MEETING_TITLE


def sanitize_filename_title(title: str | None) -> str:
    """Create a conservative, cross-platform-safe filename component."""
    display_title = normalize_display_title(title)
    safe_chars: list[str] = []
    for char in display_title:
        category = unicodedata.category(char)
        if category.startswith(("L", "N", "M")) or char in SAFE_PUNCTUATION:
            safe_chars.append(char)
        elif char.isspace():
            safe_chars.append(" ")
        else:
            safe_chars.append("-")

    safe = "".join(safe_chars)
    safe = re.sub(r"\s+", " ", safe)
    safe = re.sub(r"(?:\s*-\s*)+", " - ", safe)
    safe = safe.strip(" .-_")

    # Limit by UTF-8 bytes rather than characters so long non-ASCII titles remain
    # safely below common filesystem and cloud-sync component limits.
    while len(safe.encode("utf-8")) > MAX_FILENAME_TITLE_BYTES:
        safe = safe[:-1]
    safe = safe.rstrip(" .-_")
    return safe or DEFAULT_MEETING_TITLE


def unique_path(path: Path) -> Path:
    """Return path, or a numbered variant, without overwriting an existing file."""
    if not path.exists():
        return path
    for number in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find an available filename for {path}")


@dataclass
class RecordingSession:
    started_at: datetime.datetime
    display_title: str
    filename_title: str

    @classmethod
    def create(
        cls,
        title: str | None,
        started_at: datetime.datetime | None = None,
    ) -> "RecordingSession":
        display_title = normalize_display_title(title)
        return cls(
            started_at=started_at or datetime.datetime.now(),
            display_title=display_title,
            filename_title=sanitize_filename_title(display_title),
        )

    @property
    def filename_stem(self) -> str:
        timestamp = self.started_at.strftime("%Y-%m-%d_%H-%M-%S")
        return f"{timestamp} - {self.filename_title}"

    def use_title_if_missing(self, title: str | None) -> None:
        """Replace the fallback if Webex exposes its title shortly after call start."""
        if self.display_title != DEFAULT_MEETING_TITLE or not title:
            return
        self.display_title = normalize_display_title(title)
        self.filename_title = sanitize_filename_title(self.display_title)

    def rename_recordings(self, media_files: list[str]) -> list[str]:
        """Rename completed OBS segments while preserving order and extensions."""
        paths = list(dict.fromkeys(Path(path) for path in media_files))
        existing_paths = [path for path in paths if path.exists()]
        missing_paths = [path for path in paths if not path.exists()]
        multiple = len(existing_paths) > 1
        renamed: list[str] = []

        for part_number, source in enumerate(existing_paths, start=1):
            part = f" - part-{part_number:02d}" if multiple else ""
            desired = source.with_name(f"{self.filename_stem}{part}{source.suffix}")
            destination = source if source == desired else unique_path(desired)
            if source == destination:
                renamed.append(str(source))
                continue
            try:
                source.rename(destination)
                logger.info("Renamed recording segment to: %s", destination)
                renamed.append(str(destination))
            except OSError as exc:
                logger.warning("Could not rename recording segment %s: %s", source, exc)
                renamed.append(str(source))

        # Preserve missing paths in the result so the transcriber can log/skip them
        # consistently with its prior behavior.
        renamed.extend(str(path) for path in missing_paths)
        return renamed
