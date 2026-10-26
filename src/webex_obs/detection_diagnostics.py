"""Opt-in, local call-detection evidence and human annotations.

Only existing probe results are observed. UI callbacks enqueue nonmodal prompts;
no annotation changes detector or recording state. File I/O uses a bounded worker.
"""

from datetime import datetime, timedelta
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import threading
import time
import uuid

logger = logging.getLogger(__name__)


def resolve_event_time(text: str, now: datetime) -> datetime:
    """Interpret local HH:MM[:SS] as the most recent occurrence, within one hour."""
    try:
        clock = datetime.strptime(text.strip(), "%H:%M:%S" if text.count(":") == 2 else "%H:%M").time()
    except ValueError:
        raise ValueError("Enter a 24-hour local time: HH:MM or HH:MM:SS.") from None
    event = now.replace(hour=clock.hour, minute=clock.minute, second=clock.second, microsecond=0)
    if event > now:
        event -= timedelta(days=1)
    if not 0 <= now.timestamp() - event.timestamp() <= 3600:
        raise ValueError("The event must have occurred within the past 60 minutes.")
    return event


class _PrivateLogHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


class DetectionDiagnostics:
    def __init__(self, directory: Path):
        self.path = directory / "detection-diagnostics.jsonl"
        self.run_id = uuid.uuid4().hex
        self._salt = os.urandom(16)
        self.prompts = queue.Queue()
        self._records = queue.Queue(maxsize=4096)
        self._stop = threading.Event()
        self._worker = None
        self._announced = set()
        self._overflow_warned = False

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = _PrivateLogHandler(self.path, maxBytes=20_000_000, backupCount=5)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._worker = threading.Thread(target=self._write, args=(handler,), daemon=True,
                                        name="detection-diagnostics")
        self._worker.start()
        self.record("enabled")
        logger.info("Call detection diagnostics enabled: %s", self.path)

    def _write(self, handler) -> None:
        try:
            while not self._stop.is_set() or not self._records.empty():
                try:
                    payload = self._records.get(timeout=0.1)
                except queue.Empty:
                    continue
                handler.handle(logging.LogRecord(__name__, logging.INFO, "", 0,
                                                 json.dumps(payload), (), None))
        finally:
            handler.close()

    def close(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2)

    def record(self, kind: str, **fields) -> None:
        payload = dict(kind=kind, run_id=self.run_id, observed_at=time.time(),
                       monotonic=time.monotonic(), **fields)
        try:
            self._records.put_nowait(payload)
        except queue.Full:
            if not self._overflow_warned:
                self._overflow_warned = True
                logger.warning("Diagnostic log queue full; some evidence was dropped.")

    def sensor(self, platform: str, sensor: str, value, ok: bool, elapsed: float) -> None:
        fields = dict(platform=platform, sensor=sensor, ok=ok, elapsed_seconds=elapsed)
        if ok and sensor == "audio" and value is not None:
            fields.update(available=value.available, input_pids=value.input_pids,
                          output_pids=value.output_pids, failed_pids=value.failed_pids)
        elif ok and sensor == "window":
            fields.update(present=bool(value), title_token=(hashlib.blake2b(
                value.encode(), key=self._salt, digest_size=8).hexdigest() if value else None))
        elif ok and sensor == "sockets":
            fields["ports"] = [port for _, port, _ in value]
        self.record("sensor", **fields)

    def detected(self, platform: str, *, restart: bool = False) -> None:
        # Declining recording causes repeated candidate confirmation. Ask once per
        # detector call, rearmed only by a verified end or the quick-gap shortcut.
        if platform in self._announced and not restart:
            return
        self._announced.add(platform)
        event = dict(event_id=uuid.uuid4().hex, platform=platform,
                     detected_at=time.time(), restart=restart)
        self.record("detected_start", **event)
        self.prompts.put(event)

    def ended(self, platform: str) -> None:
        self._announced.discard(platform)
        self.record("detected_end", platform=platform)

    def answer(self, event: dict, response: str | None) -> None:
        if response in {"yes", "no", "not_sure"}:
            self.record("confirmation", **event, response=response)

    def marker(self, kind: str, occurred_at: datetime) -> None:
        if kind not in {"start", "end"}:
            raise ValueError("Unknown call marker")
        age = time.time() - occurred_at.timestamp()
        if not 0 <= age <= 3600:
            raise ValueError("The event must have occurred within the past 60 minutes.")
        self.record("manual_marker", event=kind, occurred_at=occurred_at.timestamp())
