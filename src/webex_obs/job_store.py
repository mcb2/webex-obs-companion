"""Durable processing checkpoints; uncertain outbound requests require review."""

import json
import logging
import threading
import time
from pathlib import Path

from .cleaner import MediaCleaner
from .config import Config
from .durable_state import write_json
from .post_processing import PostProcessingJob
from .transcriber import Transcriber
from .webex_client import WebexClient

logger = logging.getLogger(__name__)


class JobStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = threading.RLock()
        self.retention_safe = True

    def _path(self, job_id: str) -> Path:
        # IDs come from UUIDs or validated checkpoint filenames.
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("Invalid processing job ID")
        return self.directory / f"{job_id}.json"

    def read(self, job_id: str) -> dict:
        return json.loads(self._path(job_id).read_text(encoding="utf-8"))

    def update(self, job_id: str, **changes) -> dict:
        with self._lock:
            record = self.read(job_id)
            record.update(changes)
            write_json(self._path(job_id), record)
            return record

    def create(self, job: PostProcessingJob) -> None:
        with self._lock:
            if self._path(job.job_id).exists():
                return
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.directory.chmod(0o700)
            # Reserve a stable per-job name without creating an empty transcript.
            transcript = job.transcriber.transcripts_dir / f"{job.output_stem} - {job.job_id}.txt"
            write_json(self._path(job.job_id), {
                "version": 1, "job_id": job.job_id, "state": "queued",
                "created_ns": time.time_ns(),
                "media_files": list(job.media_files), "meeting_title": job.meeting_title,
                "output_stem": job.output_stem, "transcript": str(transcript),
                "model_name": job.transcriber.model_name,
                "enable_diarization": job.transcriber.enable_diarization,
                "delivery_enabled": job.delivery_enabled,
                "recipient_email": job.webex.recipient_email, "room_id": job.webex.room_id,
                "my_agent_email": job.webex.my_agent_email,
                "recordings_dir": str(job.recordings_dir), "retention_days": job.retention_days,
                "identities": MediaCleaner.recording_identities(list(job.media_files)),
            })

    def recover(self, config: Config) -> list[PostProcessingJob]:
        jobs = []
        records = []
        try:
            paths = [path for path in self.directory.iterdir() if path.suffix == ".json"]
        except FileNotFoundError:
            return jobs
        except OSError:
            self.retention_safe = False
            logger.error("Could not inspect the jobs directory. Retention paused; check directory access.")
            return jobs
        for path in paths:
            try:
                record = self.read(path.stem)
                self._validate_record(record, path.stem)
            except (OSError, ValueError, TypeError, KeyError):
                self.retention_safe = False
                logger.error("Unreadable or invalid processing checkpoint %s. Retention paused; inspect jobs directory.",
                             path.name)
                continue
            records.append((path, record))
            if record["state"] != "complete":
                MediaCleaner.protect_recordings(record["media_files"])

        for path, record in sorted(records, key=lambda item: item[1]["created_ns"]):
            state = record["state"]
            if state == "delivering":
                self.update(path.stem, state="uncertain")
                state = "uncertain"
            if (state == "blocked" and not record.get("delivery_held")
                    and not Path(record["transcript"]).exists()):
                # Older versions held entire jobs before local transcription.
                self.update(path.stem, state="queued", delivery_held=True)
                state = "queued"
            if state in {"uncertain", "failed", "blocked"}:
                logger.error("Processing job %s requires review (%s); retained locally.", path.stem, state)
                continue
            if state == "complete":
                continue
            if state == "transcribing":
                self.update(path.stem, state="queued")
            jobs.append(PostProcessingJob(
                media_files=tuple(record["media_files"]), meeting_title=record["meeting_title"],
                output_stem=record["output_stem"],
                transcriber=Transcriber(record["model_name"], Path(record["transcript"]).parent,
                                       record["enable_diarization"], config.hf_token),
                webex=WebexClient(config.webex_token, record["recipient_email"],
                                  record["room_id"], record["my_agent_email"]),
                delivery_enabled=record["delivery_enabled"],
                recordings_dir=Path(record["recordings_dir"]), retention_days=record["retention_days"],
                job_id=path.stem, journal=self,
            ))
        return jobs

    @staticmethod
    def _validate_record(record: dict, job_id: str) -> None:
        if (not isinstance(record, dict) or record.get("version") != 1 or record.get("job_id") != job_id
                or record.get("state") not in (
                    "queued", "transcribing", "ready", "delivering",
                    "delivered", "complete", "uncertain", "failed", "blocked",
                )):
            raise ValueError("Invalid processing checkpoint")
        for key in ("meeting_title", "output_stem", "transcript", "model_name",
                    "recipient_email", "room_id", "my_agent_email", "recordings_dir"):
            if not isinstance(record[key], str) or "\0" in record[key]:
                raise ValueError("Invalid processing checkpoint text")
        for key in ("enable_diarization", "delivery_enabled"):
            if type(record[key]) is not bool:
                raise ValueError("Invalid processing checkpoint flag")
        for key in ("created_ns", "retention_days"):
            if type(record[key]) is not int or record[key] < 0:
                raise ValueError("Invalid processing checkpoint number")
        if (not isinstance(record["media_files"], list)
                or not all(isinstance(path, str) and path for path in record["media_files"])
                or not isinstance(record["identities"], dict)):
            raise ValueError("Invalid processing checkpoint media")
