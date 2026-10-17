"""Retention is opt-in per successfully processed, application-owned file."""

import hashlib
import json
import logging
import stat
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class MediaCleaner:
    _lock = threading.RLock()
    _protected: set[Path] = set()
    _index_name = ".webex-obs-retention"

    @staticmethod
    def _path(path: str | Path) -> Path:
        return Path(path).absolute()

    @classmethod
    def _marker(cls, path: Path) -> Path:
        name = hashlib.sha256(str(path).encode()).hexdigest() + ".json"
        return path.parent / cls._index_name / name

    @staticmethod
    def _identity(path: Path) -> list[int]:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Retention requires a regular file, not a symlink")
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

    @classmethod
    def protect_recordings(cls, media_files: list[str]) -> None:
        """Revoke eligibility for active, queued, failed or recoverable segments."""
        with cls._lock:
            for filename in media_files:
                path = cls._path(filename)
                cls._protected.add(path)
                try:
                    marker = cls._marker(path)
                    if marker.parent.is_symlink():
                        raise ValueError("Retention index must not be a symlink")
                    marker.unlink(missing_ok=True)
                except (OSError, ValueError) as exc:
                    logger.warning("Could not revoke recording retention marker: %s", exc)

    @classmethod
    def recording_identities(cls, media_files: list[str]) -> dict[str, list[int]]:
        """Capture identities before processing so a replacement cannot be enrolled."""
        identities = {}
        for filename in media_files:
            try:
                identities[filename] = cls._identity(cls._path(filename))
            except (OSError, ValueError) as exc:
                logger.warning("Could not identify session media; retaining it: %s", exc)
        return identities

    @classmethod
    def mark_processed(
        cls, media_files: list[str], recordings_dir: Path,
        *, identities: dict[str, list[int]] | None = None,
    ) -> None:
        """Persist exact file identity only after every processing step succeeds."""
        root = cls._path(recordings_dir)
        with cls._lock:
            for filename in media_files:
                path = cls._path(filename)
                # OBS can write elsewhere. Such files remain recoverable but are
                # never enrolled in cleanup for a different configured directory.
                if path.parent != root:
                    continue
                try:
                    identity = cls._identity(path)
                    if identities is not None and identity != identities.get(filename):
                        logger.warning("Media changed during processing; retaining it: %s", path.name)
                        continue
                    marker = cls._marker(path)
                    marker.parent.mkdir(mode=0o700, exist_ok=True)
                    if marker.parent.is_symlink():
                        raise ValueError("Retention index must not be a symlink")
                    pending = marker.with_suffix(".tmp")
                    pending.write_text(json.dumps({"name": path.name, "identity": identity}), encoding="utf-8")
                    pending.replace(marker)
                    cls._protected.discard(path)
                except (OSError, ValueError) as exc:
                    logger.warning("Could not mark processed media eligible for retention: %s", exc)

    @classmethod
    def prune_old_recordings(
        cls, recordings_dir: Path, retention_days: int = 14,
        *, media_files: list[str] | None = None,
    ) -> None:
        """Prune eligible identities only; zero means immediately after success.

        No media directory scan or filename pattern establishes ownership. Files
        without a persisted success marker (including legacy files) are retained.
        media_files restricts cleanup to one job without changing other sessions.
        """
        if retention_days < 0:
            raise ValueError("Retention days must be nonnegative")
        root = cls._path(recordings_dir)
        index = root / cls._index_name
        cutoff = time.time() - retention_days * 86400
        selected = {cls._path(name) for name in media_files} if media_files is not None else None
        with cls._lock:
            try:
                if index.is_symlink() or not index.is_dir():
                    return
                markers = list(index.glob("*.json"))
            except OSError as exc:
                logger.warning("Could not inspect retention index: %s", exc)
                return
            for marker in markers:
                try:
                    if marker.is_symlink():
                        continue
                    record = json.loads(marker.read_text(encoding="utf-8"))
                    name = record["name"]
                    if not isinstance(name, str) or Path(name).name != name:
                        continue
                    path = root / name
                    if selected is not None and path not in selected:
                        continue
                    if marker != cls._marker(path) or path in cls._protected:
                        continue
                    identity = cls._identity(path)
                    if identity != record["identity"]:
                        # A replaced or modified file must be explicitly enrolled
                        # again; never delete it just because a name matches.
                        marker.unlink()
                        continue
                    if retention_days == 0 or path.stat().st_mtime <= cutoff:
                        path.unlink()
                        marker.unlink()
                        logger.info("Pruned successfully processed recording (%s days): %s", retention_days, path.name)
                except FileNotFoundError:
                    try:
                        marker.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("Could not remove stale retention marker: %s", exc)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    logger.warning("Could not prune indexed media: %s", exc)
