import logging
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .job_store import JobStore

from .transcriber import Transcriber
from .webex_client import WebexClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostProcessingJob:
    """Everything needed to process one finalized recording session."""

    media_files: tuple[str, ...]
    meeting_title: str
    output_stem: str
    transcriber: Transcriber
    webex: WebexClient
    delivery_enabled: bool
    recordings_dir: Path
    retention_days: int
    job_id: str = field(default_factory=lambda: uuid4().hex)
    journal: "JobStore | None" = None


class PostProcessingWorker:
    """Process completed meetings in FIFO order on one background thread."""

    def __init__(self, processor: Callable[[PostProcessingJob], None],
                 on_fatal: Callable[[], None] | None = None):
        self._processor = processor
        self._on_fatal = on_fatal
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._queue: queue.Queue[PostProcessingJob | object] = queue.Queue()
        self._sentinel = object()
        self._thread: threading.Thread | None = None
        self._shutdown_requested = False

    def start(self) -> None:
        with self._lock:
            if self._shutdown_requested:
                raise RuntimeError("Post-processing worker is shutting down")
            if self._thread is not None:
                if not self._thread.is_alive():
                    raise RuntimeError("Post-processing worker exited unexpectedly")
                return
            self._thread = threading.Thread(target=self._supervised_run,
                                            name="post-processing", daemon=True)
            self._thread.start()

    def submit(self, job: PostProcessingJob) -> None:
        with self._lock:
            if self._shutdown_requested:
                raise RuntimeError("Post-processing worker is shutting down")
            if job.journal is not None:
                job.journal.create(job)
            self.start()
            self._queue.put(job)

    def shutdown(self, wait: bool = False, timeout: float = 2.0, *, drain: bool = True) -> bool:
        """Stop admission; optionally drain, but never wait indefinitely for inference.

        Application shutdown uses drain=False: queued durable jobs resume on restart.
        A running job may finish before the process exits; its checkpoints remain valid.
        """
        with self._lock:
            if not drain:
                self._halt.set()
            if not self._shutdown_requested:
                self._shutdown_requested = True
                self._queue.put(self._sentinel)
            thread = self._thread
        if wait and thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _supervised_run(self) -> None:
        try:
            self._run()
        except BaseException:
            logger.exception("Post-processing worker exited unexpectedly; restart required.")
            if self._on_fatal is not None:
                self._on_fatal()

    def _run(self) -> None:
        while not self._halt.is_set():
            item = self._queue.get()
            try:
                if item is self._sentinel or self._halt.is_set():
                    return
                try:
                    self._processor(item)
                except Exception:
                    if item.journal is not None:
                        record = item.journal.read(item.job_id)
                        item.journal.update(item.job_id, state=(
                            "uncertain" if record["state"] == "delivering" else
                            "delivered" if record["state"] == "delivered" else "failed"
                        ))
                    logger.exception("Post-processing failed for job %s; files retained.", item.job_id)
            finally:
                self._queue.task_done()
