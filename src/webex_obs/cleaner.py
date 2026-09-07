import datetime
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

class MediaCleaner:
    @staticmethod
    def prune_old_recordings(recordings_dir: Path, retention_days: int = 14):
        if not recordings_dir.exists():
            return
        now = datetime.datetime.now()
        cutoff = now - datetime.timedelta(days=retention_days)
        media_exts = {".m4a", ".mp4", ".mkv", ".wav", ".mov", ".aac"}

        for item in recordings_dir.iterdir():
            if item.is_file() and item.suffix.lower() in media_exts:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(item))
                if mtime < cutoff:
                    try:
                        item.unlink()
                        logger.info(f"Pruned old recording ({retention_days}+ days): {item.name}")
                    except Exception as e:
                        logger.warning(f"Could not delete old media file {item}: {e}")
