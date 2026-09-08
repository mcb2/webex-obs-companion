import datetime
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

class MediaCleaner:
    @staticmethod
    def prune_old_recordings(recordings_dir: Path, retention_days: int = 14):
        try:
            if not recordings_dir.exists():
                return
        except OSError as e:
            logger.warning(f"Recording directory is temporarily unavailable: {e}")
            return
        now = datetime.datetime.now()
        cutoff = now - datetime.timedelta(days=retention_days)
        media_exts = {".m4a", ".mp4", ".mkv", ".wav", ".mov", ".aac"}

        try:
            items = list(recordings_dir.iterdir())
        except OSError as e:
            logger.warning(f"Could not inspect recording directory: {e}")
            return
        for item in items:
            if item.is_file() and item.suffix.lower() in media_exts:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(item))
                if mtime < cutoff:
                    try:
                        item.unlink()
                        logger.info(f"Pruned old recording ({retention_days}+ days): {item.name}")
                    except Exception as e:
                        logger.warning(f"Could not delete old media file {item}: {e}")
