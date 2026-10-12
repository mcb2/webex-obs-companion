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
        records = [(path, self.read(path.stem)) for path in self.directory.glob("*.json")]
        for path, record in sorted(records, key=lambda item: item[1]["created_ns"]):
            if record["version"] != 1 or record["job_id"] != path.stem:
                raise ValueError("Unsupported processing checkpoint; inspect local jobs directory")
            state = record["state"]
            if state != "complete":
                MediaCleaner.protect_recordings(record["media_files"])
            if state == "delivering":
                self.update(path.stem, state="uncertain")
                state = "uncertain"
            if state in {"uncertain", "failed", "blocked"}:
                logger.error("Processing job %s requires review (%s); retained locally.", path.stem, state)
                continue
            if state == "complete":
                continue
            if state not in {"queued", "transcribing", "ready", "delivered"}:
                raise ValueError("Unknown processing checkpoint state")
            if state == "transcribing":
                self.update(path.stem, state="queued")
            if (record["delivery_enabled"] and state != "delivered"
                    and (not config.webex_delivery_enabled
                         or (record["room_id"], record["recipient_email"])
                         != (config.room_id, config.webex_recipient_email))):
                self.update(path.stem, state="blocked")
                logger.error("Recovered job %s has disabled/changed delivery settings; review required.", path.stem)
                continue
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
